CPU-triggered horizontal autoscaling demo
==========================================

Architecture
------------
- server1 and server2 run server.py on guest TCP port 9000.
- load_balancer.py listens on Debian host port 9100.
- autoscaler.py polls CPU through the libvirt Python API.
- At >80% CPU for 3 consecutive 3-second samples, autoscaler.py
  provisions/starts server2, waits for its DHCP lease and TCP service,
  then adds both backends to /tmp/cloud-autoscaler-backends.json.
- load_balancer.py reads the backend file for each request and distributes
  new connections round-robin.
- client.py sends newline-delimited JSON requests through port 9100.

IMPORTANT
---------
1. Run all host-side scripts on the Debian libvirt host, not inside server1.
2. Run autoscaler.py with permission to access qemu:///system and read the
   VM images. On this setup, use sudo /usr/bin/python3.
3. Start load_balancer.py before autoscaler.py.
4. server1 must be running and cloud-server.service must be active.
5. server2.qcow2 must already exist at /var/lib/libvirt/images/server2.qcow2.
6. Do NOT run provision.py manually; autoscaler.py calls it only on overload.
7. The client defaults to 127.0.0.1, assuming it runs on the Debian host.
   For a client on another machine, use --host <Debian-host-IP> and allow
   TCP port 9100 through the host firewall.

Start
-----
Terminal 1 (Debian host):
    cd ~/cloud-autoscaler/code
    python3 load_balancer.py

Terminal 2 (Debian host):
    cd ~/cloud-autoscaler/code
    sudo /usr/bin/python3 autoscaler.py

Terminal 3 (Debian host):
    cd ~/cloud-autoscaler/code
    python3 client.py --requests 100 --workers 8 --iterations 1000000

For a stronger test, use:
    python3 client.py --requests 300 --workers 16 --iterations 3000000

The client workload is sent to server1 initially through the load balancer.
After the scale-out completes, later requests should be split between both
backend IPs. Observe load_balancer.py's logs for backend IPs.

Important clone caveat
----------------------
The VM disk is a clone of server1, so guest hostname and machine-id may be
duplicated. The provisioner gives server2 a new libvirt UUID and NIC MAC;
DHCP should assign a different IP. TCP routing uses IP addresses, not the
guest hostname. If the default libvirt network has static DHCP rules or the
guest has a statically configured IP, fix that before running both VMs.