# CPU-Triggered Horizontal Autoscaling Demo (libvirt + KVM on Debian)

A small cloud-management framework that automatically scales a client-server
application from **1 to 2 server VMs** when CPU load stays high. All VM
monitoring and provisioning is done through the **libvirt Python API**. The
code never shells out to `virsh`, `qemu-img` or `virt-clone`.

Tested layout: Debian 11 / 12 / 13 host, KVM, libvirt `default` NAT network,
Debian cloud-image guests.

---

## Table of contents

1. [How it works](#1-how-it-works)
2. [Project files](#2-project-files)
3. [Prerequisites](#3-prerequisites)
4. [Host setup](#4-host-setup)
5. [Build server1 and the golden base image](#5-build-server1-and-the-golden-base-image)
6. [Install the project scripts](#6-install-the-project-scripts)
7. [Running the demo](#7-running-the-demo)
8. [Repeating the demo (reset)](#8-repeating-the-demo-reset)
9. [Tuning](#9-tuning)
10. [Troubleshooting](#10-troubleshooting)
11. [Design notes](#11-design-notes)

---

## 1. How it works

```
                          Debian host
 +-----------+      +------------------+      +---------------------------+
 | client.py | ---> | load_balancer.py | ---> | server1  (VM, port 9000)  |
 +-----------+      |   port 9100      |      +---------------------------+
                    |   round-robin    |      | server2  (VM, port 9000)  |
                    +--------^---------+      |  created ONLY on overload |
                             | reads          +-------------^-------------+
                      backends.json                         | libvirt API
                             ^                              | (define + start)
                    +--------+---------+                    |
                    |  autoscaler.py   |--------------------+
                    |  polls CPU every |  provision.py:
                    |  3 s via libvirt |  clone base disk, define VM from
                    +------------------+  server1's XML, new MAC + UUID
```

1. `server1` runs `server.py` on guest TCP port **9000** (CPU-intensive work per request).
2. `load_balancer.py` listens on host port **9100** and spreads new connections round-robin over the backends listed in `backends.json`.
3. `autoscaler.py` reads server1's CPU time with `virDomainGetInfo` every **3 s**.
4. If CPU is **above 80 % for 3 consecutive samples**, it calls `provision.py`, which creates `server2`'s disk from `autoscaler-base.qcow2`, defines the VM and starts it.
5. The autoscaler waits for server2's DHCP lease and for TCP port 9000 to accept connections, then **publishes both backends** to `backends.json`. This file is the notification to the client path: the load balancer re-reads it per request, so new requests are spread over both servers.
6. `client.py` always talks to the load balancer, so it needs no change when the backend set grows.

Scaling is one-shot (N=1 to N=2), as the assignment requires.

---

## 2. Project files

| File | Runs on | Purpose |
|---|---|---|
| `server.py` | guest VMs | Multi-threaded TCP server, CPU-heavy requests, port 9000 |
| `load_balancer.py` | host | Round-robin proxy on port 9100, reads `backends.json` |
| `client.py` | host | Load generator (`--requests`, `--workers`, `--iterations`) |
| `autoscaler.py` | host | Monitors server1 CPU, triggers scale-out |
| `provision.py` | host | Creates server2's disk and domain (libvirt API only) |
| `reset_server2.py` | host | Deletes server2 and its disk so the demo can be repeated |
| `backends.json` | host | Generated at runtime, list of active backends |

Expected layout on the host: `~/cloud-autoscaler/code/` containing the host scripts.

---

## 3. Prerequisites

- Debian 11, 12 or 13 (amd64) with a normal user that has `sudo`.
- CPU virtualization (Intel VT-x / AMD-V) enabled in BIOS/UEFI.
- Recommended resources: **4 CPU cores, 6 GB free RAM, 15 GB free disk**
  (each VM uses 1 vCPU and 1 GB RAM).
- If the host itself is a VM, nested virtualization must be enabled on its hypervisor.
- Internet access (packages and the Debian cloud image).

Check hardware virtualization:

```bash
grep -E -c '(vmx|svm)' /proc/cpuinfo      # must print a number greater than 0
```

> On an ARM host replace `amd64` with `arm64` everywhere (image name,
> `qemu-system-arm`), and expect different virt-install options.

---

## 4. Host setup

### 4.1 Install packages

```bash
sudo apt update
sudo apt install -y \
    qemu-system-x86 qemu-utils \
    libvirt-daemon-system libvirt-clients \
    virtinst python3 python3-libvirt \
    dnsmasq-base iptables nftables \
    cloud-image-utils wget openssh-client cpu-checker
```

Optional (only if you plot results): `sudo apt install -y python3-matplotlib`

### 4.2 Add your user to the libvirt groups

```bash
sudo usermod -aG libvirt,kvm "$USER"
```

Log out and back in (or run `newgrp libvirt`) so the groups apply. Verify:

```bash
id | grep -E 'libvirt|kvm'
```

> From here on, **do not run the project scripts with `sudo`**. Your user
> reaches `qemu:///system` through the `libvirt` group. Running with `sudo`
> creates root-owned files (for example `backends.json`) that later block
> normal runs.

### 4.3 Make `virsh` use the system connection

Without this, `virsh` as a normal user shows an empty session-level list.

```bash
echo 'export LIBVIRT_DEFAULT_URI=qemu:///system' >> ~/.bashrc
source ~/.bashrc
```

### 4.4 Start libvirt and verify KVM

```bash
sudo systemctl enable --now libvirtd
systemctl is-active libvirtd            # active
ls -l /dev/kvm                          # must exist
sudo kvm-ok                             # "KVM acceleration can be used"
virsh version
```

### 4.5 Enable the `default` NAT network (virbr0)

```bash
virsh net-list --all
virsh net-start default           # ignore "already active"
virsh net-autostart default
virsh net-list                    # default: active, autostart yes
ip addr show virbr0               # normally 192.168.122.1/24
```

If `default` does not exist:

```bash
virsh net-define /usr/share/libvirt/networks/default.xml
virsh net-start default
virsh net-autostart default
```

### 4.6 Make sure the `default` storage pool exists

The scripts use the pool named `default` (directory `/var/lib/libvirt/images`).

```bash
virsh pool-list --all
```

If it is missing:

```bash
virsh pool-define-as default dir --target /var/lib/libvirt/images
virsh pool-build default
virsh pool-start default
virsh pool-autostart default
virsh pool-list --all             # default: active, autostart yes
```

---

## 5. Build server1 and the golden base image

Goal: one `server1` VM, plus a **separate, never-booted copy** of its disk
called `autoscaler-base.qcow2`. The autoscaler clones that base to create
server2. Never clone a running VM's disk.

### 5.1 Download the Debian cloud image

```bash
cd /var/lib/libvirt/images
sudo wget -O server1.qcow2 \
  https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2
sudo qemu-img resize server1.qcow2 8G
```

### 5.2 Create the cloud-init seed (user + password)

```bash
mkdir -p ~/cloud-autoscaler/seed && cd ~/cloud-autoscaler/seed

cat > user-data <<'EOF'
#cloud-config
hostname: server1
ssh_pwauth: true
users:
  - name: student
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    lock_passwd: false
    plain_text_passwd: student123
EOF

printf 'instance-id: server1\nlocal-hostname: server1\n' > meta-data

cloud-localds server1-seed.iso user-data meta-data
sudo mv server1-seed.iso /var/lib/libvirt/images/
```

Change the password (and optionally add `ssh_authorized_keys:`) if the host
is shared.

### 5.3 Create server1 (1 vCPU, 1 GB RAM, fixed MAC)

```bash
virt-install --connect qemu:///system \
  --name server1 \
  --memory 1024 --vcpus 1 \
  --disk path=/var/lib/libvirt/images/server1.qcow2,format=qcow2 \
  --disk path=/var/lib/libvirt/images/server1-seed.iso,device=cdrom \
  --import --os-variant debian12 \
  --network network=default,mac=52:54:00:aa:bb:01 \
  --graphics none --noautoconsole
```

If `--os-variant debian12` is not recognised, run `osinfo-query os | grep -i debian`
and use the newest listed, or `--os-variant generic`.

server2 is later given MAC `52:54:00:aa:bb:02`, so the two never clash.

### 5.4 Find server1's IP and log in

```bash
sleep 40
virsh domifaddr server1
ssh student@<SERVER1_IP>            # password: student123
```

### 5.5 Install the server application in server1

On the host, copy the server file:

```bash
scp ~/cloud-autoscaler/code/server.py student@<SERVER1_IP>:/tmp/server.py
```

Inside server1:

```bash
sudo mkdir -p /opt/cloud-server
sudo mv /tmp/server.py /opt/cloud-server/server.py

sudo tee /etc/systemd/system/cloud-server.service >/dev/null <<'EOF'
[Unit]
Description=Cloud demo server (port 9000)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /opt/cloud-server/server.py
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now cloud-server.service
systemctl is-active cloud-server.service      # active
ss -ltn | grep 9000                           # must listen on 0.0.0.0:9000 (or *:9000)
```

If your `server.py` needs arguments, add them to `ExecStart`. It must listen
on all interfaces, not only `127.0.0.1`.

### 5.6 Make the image safe to clone

Still inside server1. First install a one-shot service that regenerates SSH
host keys on boot (Debian does not do this by itself once cloud-init is disabled):

```bash
sudo tee /etc/systemd/system/regen-ssh-hostkeys.service >/dev/null <<'EOF'
[Unit]
Description=Regenerate SSH host keys if missing
Before=ssh.service
ConditionPathExists=!/etc/ssh/ssh_host_ed25519_key

[Service]
Type=oneshot
ExecStart=/usr/bin/ssh-keygen -A

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable regen-ssh-hostkeys.service
```

Test that the server survives a reboot first:

```bash
sudo reboot
# wait about 30 s, log in again, then:
systemctl is-active cloud-server.service
```

Now generalize the image (this is the last thing you do inside the guest):

```bash
sudo touch /etc/cloud/cloud-init.disabled     # stops cloud-init waiting for a seed disk
sudo rm -f /etc/ssh/ssh_host_*                # regenerated on next boot
sudo truncate -s 0 /etc/machine-id            # regenerated on next boot; keeps DHCP identities unique
sudo rm -f /var/lib/dbus/machine-id
sudo ln -sf /etc/machine-id /var/lib/dbus/machine-id
sudo cloud-init clean --logs 2>/dev/null || true
sudo apt clean
history -c
sudo poweroff
```

Why this matters: clones sharing a machine-id or SSH host keys behave
oddly (DHCP client identifiers, SSH warnings). The guest uses **DHCP**, never
a static IP, so server2 gets its own address from libvirt automatically.

### 5.7 Remove the seed CD-ROM from server1's definition

server2's XML is derived from server1's, so remove the CD-ROM now.

```bash
virsh domblklist server1                       # note the cdrom target, usually sda
virsh detach-disk server1 sda --config         # use the target name shown above
virsh domblklist server1                       # only the qcow2 disk remains
sudo rm -f /var/lib/libvirt/images/server1-seed.iso
```

### 5.8 Create the golden base image (server1 must be shut off)

```bash
virsh domstate server1                         # must say: shut off
virsh pool-refresh default
virsh vol-clone server1.qcow2 autoscaler-base.qcow2 --pool default
virsh vol-list default                         # both volumes must be listed
```

The name **`autoscaler-base.qcow2`** must match `BASE_VOLUME` in `provision.py`.
**Never boot, edit or delete the base**; it stays powered off forever.

Make sure the hypervisor can read it:

```bash
sudo chmod 644 /var/lib/libvirt/images/autoscaler-base.qcow2
ls -l /var/lib/libvirt/images/
```

### 5.9 Start server1 and verify

```bash
virsh start server1
sleep 40
virsh domifaddr server1
nc -vz <SERVER1_IP> 9000      # connection succeeded  (apt install netcat-openbsd if missing)
```

server1's disk was cleaned in step 5.6, so it regenerated its machine-id and
SSH keys on this boot. That is expected.

Do **not** create server2 by hand. The autoscaler creates it.

---

## 6. Install the project scripts

```bash
mkdir -p ~/cloud-autoscaler/code
cd ~/cloud-autoscaler/code
# copy here: server.py (also in the guest), load_balancer.py, client.py,
# autoscaler.py, provision.py, reset_server2.py
```

The host scripts need only `python3` and `python3-libvirt` (installed in 4.1),
plus any extra modules your own scripts import.

Check that Python can import libvirt and connect:

```bash
python3 -c "import libvirt; c=libvirt.open('qemu:///system'); print(c.getHostname(), [d.name() for d in c.listAllDomains()])"
```

### Backend file location

`autoscaler.py` and `load_balancer.py` share one JSON file. Both must use the
same path. The recommended path is inside the project directory, never `/tmp`
(a stale root-owned file in `/tmp` blocks `os.replace`). Set it once:

```bash
echo 'export BACKENDS_FILE=$HOME/cloud-autoscaler/code/backends.json' >> ~/.bashrc
source ~/.bashrc
echo "$BACKENDS_FILE"
```

If you instead rely on the defaults in the code, check that both scripts
default to the same path.

---

## 7. Running the demo

### Pre-flight checklist

```bash
virsh list --all                  # server1: running. server2: NOT listed
virsh vol-list default            # server1.qcow2 and autoscaler-base.qcow2 listed,
                                  # server2.qcow2 NOT listed
rm -f "$BACKENDS_FILE"
```

If server2 or `server2.qcow2` exists from a previous run:

```bash
python3 reset_server2.py
```

### Start order

Open **three terminals on the Debian host** (not inside any VM). Use your
normal user, **no sudo**.

**Terminal 1: load balancer**

```bash
cd ~/cloud-autoscaler/code
python3 load_balancer.py
```

**Terminal 2: autoscaler** (start after the load balancer)

```bash
cd ~/cloud-autoscaler/code
python3 autoscaler.py
```

Expected:

```
[autoscaler] Waiting for server1's DHCP address...
[autoscaler] Initial backend: server1 at 192.168.122.x:9000
[autoscaler] Monitoring started. Ctrl+C to stop.
[...] server1: CPU=1.20%
```

**Terminal 3: client, low load first**

```bash
cd ~/cloud-autoscaler/code
python3 client.py --requests 20 --workers 1 --iterations 100000
```

CPU in Terminal 2 should stay below 80 %. Adjust the numbers to your hardware (see section 9).

**Terminal 3: client, high load**

```bash
python3 client.py --requests 100 --workers 8 --iterations 1000000
# stronger test:
python3 client.py --requests 300 --workers 16 --iterations 3000000
```

### What you should see in Terminal 2

```
[...] server1: CPU=99.97%
  Overload sample 1/3
  Overload sample 2/3
  Overload sample 3/3
[autoscaler] Sustained overload detected. Provisioning server2...
[autoscaler] server2 is not defined. Creating its disk and VM from the base image...
[provision] Cloning base disk through libvirt storage API...
[provision] Defined server2 using /var/lib/libvirt/images/server2.qcow2
[autoscaler] Starting server2...
[autoscaler] server2 has IP 192.168.122.y; checking application readiness...
[autoscaler] SCALE-OUT COMPLETE. Load balancer now has 192.168.122.x and 192.168.122.y.
```

Then watch Terminal 1: new requests alternate between both backend IPs, and
both VMs' CPU values are printed by the autoscaler.

### Useful verification commands (from another terminal)

```bash
virsh list --all                       # server1 and server2 running
virsh domifaddr server2
cat "$BACKENDS_FILE"                   # two backends
virsh vol-list default
virsh domstats --cpu-total server1 server2
```

---

## 8. Repeating the demo (reset)

```bash
# stop client, autoscaler and load balancer with Ctrl+C, then:
python3 reset_server2.py               # destroys/undefines server2, deletes its volume
rm -f "$BACKENDS_FILE"
virsh list --all                       # confirm only server1 remains
```

Then repeat section 7. Because server2 is created from scratch each time,
deleting it and re-running proves that autoscaling is fully automatic.

---

## 9. Tuning

Edit the constants at the top of `autoscaler.py`:

| Setting | Default | Meaning |
|---|---|---|
| `CPU_THRESHOLD` | 80.0 | Percent CPU that counts as overload |
| `POLL_INTERVAL` | 3 | Seconds between samples |
| `REQUIRED_OVERLOAD_READINGS` | 3 | Consecutive overload samples before scaling (filters transient spikes) |
| `READINESS_TIMEOUT` | 180 | Seconds to wait for server2's DHCP lease and service |

Edit the constants at the top of `provision.py`:

| Setting | Default | Meaning |
|---|---|---|
| `BASE_VOLUME` | `autoscaler-base.qcow2` | Golden image name in the pool |
| `NEW_MAC` | `52:54:00:aa:bb:02` | Must differ from server1's MAC |
| `USE_OVERLAY` | `False` | `True` makes a fast qcow2 overlay instead of a full copy (the base must then never move or change) |

Choose the load levels so that low load stays under 80 % on one VM and high load
exceeds it, but is not so large that two servers still cannot cope.

---

## 10. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `virsh list` shows nothing | You are on the user session. Run `export LIBVIRT_DEFAULT_URI=qemu:///system` or use `virsh -c qemu:///system`. |
| `Failed to connect socket ... Permission denied` | User not in the `libvirt` group, or the shell has not picked it up. Run `sudo usermod -aG libvirt $USER`, then log out and in. |
| `/dev/kvm` missing | Enable virtualization in BIOS; for a VM host enable nested virtualization; `sudo modprobe kvm_intel` or `kvm_amd`. |
| `network 'default' is not active` | `virsh net-start default && virsh net-autostart default` |
| `Storage volume not found ... server2.qcow2` followed by `target path ... already exists` | The pool cache is stale and a leftover disk exists. `provision.py` refreshes the pool and removes stale disks; otherwise run `python3 reset_server2.py` or `virsh pool-refresh default`. |
| `Storage volume not found: autoscaler-base.qcow2` | The base is missing or not in the pool. Redo step 5.8, then `virsh pool-refresh default`. |
| `PermissionError ... /tmp/cloud-autoscaler-backends.json` | A root-owned file exists. `sudo rm` it and use `BACKENDS_FILE` in the project directory. Never run the scripts with sudo. |
| QEMU error `Permission denied` opening a disk at VM start | The `libvirt-qemu` user cannot read the image. Use `ls -l /var/lib/libvirt/images/`, keep images there, mode 644. |
| `No DHCP lease found for server2` | The guest did not bring up networking. Check the console: `virsh console server2` (exit with `Ctrl+]`). Make sure the guest uses DHCP, not a static IP, and that `autoscaler-base.qcow2` was created after step 5.6. |
| `Server service did not become reachable` | `cloud-server.service` is not enabled in the base image. Redo section 5.5 and 5.8. Increase `READINESS_TIMEOUT` on slow hosts. |
| Two VMs got the same IP | The image contains a static IP or a hard-coded MAC. Use DHCP and a unique MAC. |
| `virt-install: --os-variant not found` | `osinfo-query os \| grep -i debian` and use an available value, or `--os-variant generic`. |
| Scale-out never triggers | CPU never exceeded the threshold for 3 samples. Increase client `--workers` / `--iterations`, or lower `CPU_THRESHOLD`. |
| Load balancer cannot connect | Confirm `backends.json` content, `ss -ltn \| grep 9100`, and that the VM answers on 9000 (`nc -vz <ip> 9000`). |
| Remote client cannot connect | Run it with `--host <Debian-host-IP>` and allow TCP 9100 in the firewall (`sudo ufw allow 9100/tcp` if ufw is used). |

Handy debugging commands (debugging only; the autoscaler itself uses the API):

```bash
virsh dumpxml server1
virsh console server2
journalctl -u libvirtd --since "10 min ago"
sudo tail -n 50 /var/log/libvirt/qemu/server2.log
```

---

## 11. Design notes

- **API-only provisioning:** server2's volume is created with
  `virStoragePoolCreateXMLFrom` (or `virStorageVolCreateXML` with a backing store),
  the domain is defined with `virDomainDefineXML` and started with `virDomainCreate`.
  Server2's XML is derived from server1's persistent XML with a new name,
  disk path, MAC and no UUID, so libvirt generates one.
- **CPU measurement:** CPU % = Δ(cumulative CPU ns from `virDomainGetInfo`) /
  (Δ wall-clock ns × vCPUs) × 100. This is measured from the host, so it is
  usually higher than `top` inside the guest, because it also counts the
  hypervisor's emulation and I/O threads for that VM.
- **False-positive protection:** scaling needs 3 consecutive samples above the
  threshold, and a single sample below the threshold resets the counter.
- **Notification to the client path:** the autoscaler atomically rewrites
  `backends.json` (temp file plus `os.replace`); the load balancer re-reads it,
  so no restart is needed.
- **Idempotence:** if server2 is already defined, it is reused and started; a
  leftover disk without a domain is removed before cloning.
- **Why a separate base image:** a running VM's disk cannot be safely copied,
  and two VMs must never share one writable disk.

---

## Quick command summary

```bash
# One-time host setup
sudo apt update && sudo apt install -y qemu-system-x86 qemu-utils libvirt-daemon-system \
  libvirt-clients virtinst python3 python3-libvirt dnsmasq-base iptables nftables \
  cloud-image-utils wget openssh-client cpu-checker
sudo usermod -aG libvirt,kvm "$USER"            # then re-login
echo 'export LIBVIRT_DEFAULT_URI=qemu:///system' >> ~/.bashrc

# Each demo run
cd ~/cloud-autoscaler/code
python3 reset_server2.py && rm -f "$BACKENDS_FILE"
python3 load_balancer.py                        # terminal 1
python3 autoscaler.py                           # terminal 2
python3 client.py --requests 100 --workers 8 --iterations 1000000   # terminal 3
```