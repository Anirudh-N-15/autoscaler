#!/usr/bin/python3
"""CPU-triggered horizontal autoscaler using the libvirt Python API."""

import json
import os
import tempfile
import time
from datetime import datetime

import libvirt

from provision import provision_server2

LIBVIRT_URI = "qemu:///system"
BASE_VM = "server1"
NEW_VM = "server2"
NETWORK_NAME = "default"
SERVER_PORT = 9000

CPU_THRESHOLD = 80.0
POLL_INTERVAL = 3
REQUIRED_OVERLOAD_READINGS = 3

BACKENDS_FILE = os.environ.get(
    "BACKENDS_FILE", "/tmp/cloud-autoscaler-backends.json"
)

READINESS_TIMEOUT = 180
READINESS_POLL_INTERVAL = 3


def get_mac(domain):
    """Return the first network interface MAC address."""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(domain.XMLDesc(0))
    nodes = root.findall("./devices/interface/mac")

    if not nodes:
        raise RuntimeError(
            f"{domain.name()} has no interface MAC"
        )

    return nodes[0].get("address", "").lower()


def get_dhcp_ip(conn, mac):
    """Find a VM's IPv4 address from libvirt DHCP leases."""
    network = conn.networkLookupByName(NETWORK_NAME)

    for lease in network.DHCPLeases(None, 0):
        if lease.get("mac", "").lower() == mac.lower():
            return lease.get("ipaddr")

    return None


def wait_for_ip(conn, domain, timeout=READINESS_TIMEOUT):
    """Wait until the VM receives a DHCP address."""
    mac = get_mac(domain)
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        ip = get_dhcp_ip(conn, mac)

        if ip:
            return ip

        time.sleep(READINESS_POLL_INTERVAL)

    raise TimeoutError(
        f"No DHCP lease found for {domain.name()} "
        f"(MAC {mac}) within {timeout} seconds. "
        "Check the guest network configuration."
    )


def wait_for_service(
    ip, port=SERVER_PORT, timeout=READINESS_TIMEOUT
):
    """Wait until the application accepts TCP connections."""
    import socket

    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        try:
            with socket.create_connection(
                (ip, port), timeout=2
            ):
                return True
        except OSError:
            time.sleep(READINESS_POLL_INTERVAL)

    raise TimeoutError(
        f"Server service did not become reachable at "
        f"{ip}:{port} within {timeout} seconds."
    )


def publish_backends(backends):
    """Atomically update the backend list read by load_balancer.py."""
    directory = os.path.dirname(BACKENDS_FILE) or "."
    os.makedirs(directory, exist_ok=True)

    payload = {
        "backends": [
            {
                "name": name,
                "host": ip,
                "port": SERVER_PORT,
            }
            for name, ip in backends
        ]
    }

    fd, temporary = tempfile.mkstemp(
        prefix="cloud-autoscaler-",
        suffix=".json",
        dir=directory,
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(payload, file)
            file.write("\n")

        os.chmod(temporary, 0o644)
        os.replace(temporary, BACKENDS_FILE)

    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def cpu_percent(domain, old_cpu_time, old_wall_time):
    """Calculate CPU usage from two cumulative CPU-time samples."""
    info = domain.info()

    now = time.monotonic()
    cpu_time = info[4]
    elapsed = now - old_wall_time
    delta = cpu_time - old_cpu_time
    vcpus = info[3]

    if elapsed <= 0 or delta < 0 or vcpus <= 0:
        return None, cpu_time, now

    percent = (
        delta / (elapsed * vcpus * 1_000_000_000)
    ) * 100.0

    return min(percent, 100.0), cpu_time, now


def initialize_cpu_state(domain):
    """Initialize CPU monitoring state for a VM."""
    info = domain.info()

    return {
        "domain": domain,
        "cpu_time": info[4],
        "wall_time": time.monotonic(),
        "overload_count": 0,
    }


def ensure_server2(conn):
    """
    Ensure server2 exists and is running.

    If the VM is not defined, ask provision.py to create its
    disk from the base image, define the VM, and start it.

    provision_server2() must implement disk creation from
    autoscaler-base.qcow2 when the target disk is missing.
    """
    try:
        server2 = conn.lookupByName(NEW_VM)

        print(
            "[autoscaler] server2 already exists."
        )

    except libvirt.libvirtError:
        print(
            "[autoscaler] server2 is not defined. "
            "Creating its disk and VM from the base image..."
        )

        server2 = provision_server2(conn)

    if not server2.isActive():
        print(
            "[autoscaler] Starting server2..."
        )

        server2.create()

    return server2


def main():
    """Monitor server1 and automatically scale out when overloaded."""
    conn = libvirt.open(LIBVIRT_URI)

    if conn is None:
        raise RuntimeError(
            f"Could not connect to libvirt at {LIBVIRT_URI}"
        )

    try:
        try:
            server1 = conn.lookupByName(BASE_VM)

        except libvirt.libvirtError as exc:
            raise RuntimeError(
                f"{BASE_VM} is not defined in libvirt"
            ) from exc

        if not server1.isActive():
            raise RuntimeError(
                f"{BASE_VM} is not running. "
                "Start server1 before autoscaler.py."
            )

        print(
            "[autoscaler] Waiting for server1's DHCP address..."
        )

        ip1 = wait_for_ip(conn, server1)

        wait_for_service(ip1)

        publish_backends([(BASE_VM, ip1)])

        print(
            f"[autoscaler] Initial backend: "
            f"{BASE_VM} at {ip1}:{SERVER_PORT}"
        )

        print(
            f"[autoscaler] CPU threshold={CPU_THRESHOLD:.0f}%, "
            f"poll={POLL_INTERVAL}s, "
            f"consecutive readings="
            f"{REQUIRED_OVERLOAD_READINGS}"
        )

        print(
            "[autoscaler] Monitoring started. Ctrl+C to stop."
        )

        states = {
            BASE_VM: initialize_cpu_state(server1)
        }

        scaled = False

        while True:
            time.sleep(POLL_INTERVAL)

            timestamp = datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )

            # Monitor every VM currently tracked by the autoscaler.
            for name in list(states):
                state = states[name]
                domain = state["domain"]

                if not domain.isActive():
                    print(
                        f"[{timestamp}] WARNING: "
                        f"{name} is not active."
                    )
                    continue

                percent, cpu_time, wall_time = cpu_percent(
                    domain,
                    state["cpu_time"],
                    state["wall_time"],
                )

                state["cpu_time"] = cpu_time
                state["wall_time"] = wall_time

                if percent is None:
                    print(
                        f"[{timestamp}] {name}: "
                        "CPU sample unavailable"
                    )
                    continue

                print(
                    f"[{timestamp}] {name}: "
                    f"CPU={percent:.2f}%"
                )

                # Scale out only when server1 is overloaded.
                if name == BASE_VM and not scaled:
                    if percent > CPU_THRESHOLD:
                        state["overload_count"] += 1

                        print(
                            "  Overload sample "
                            f"{state['overload_count']}/"
                            f"{REQUIRED_OVERLOAD_READINGS}"
                        )

                    else:
                        state["overload_count"] = 0

                    if (
                        state["overload_count"]
                        >= REQUIRED_OVERLOAD_READINGS
                    ):
                        print(
                            "[autoscaler] Sustained overload "
                            "detected. Provisioning server2..."
                        )

                        try:
                            # Automatically reuse server2 if it
                            # exists; otherwise provision it.
                            server2 = ensure_server2(conn)

                            # Wait for guest network readiness.
                            ip2 = wait_for_ip(conn, server2)

                            print(
                                f"[autoscaler] server2 has IP "
                                f"{ip2}; checking application "
                                "readiness..."
                            )

                            wait_for_service(ip2)

                            # Publish both backend addresses.
                            publish_backends([
                                (BASE_VM, ip1),
                                (NEW_VM, ip2),
                            ])

                            states[NEW_VM] = (
                                initialize_cpu_state(server2)
                            )

                            scaled = True

                            print(
                                "[autoscaler] SCALE-OUT COMPLETE. "
                                f"Load balancer now has "
                                f"{ip1} and {ip2}."
                            )

                        except Exception as exc:
                            print(
                                f"[autoscaler] Scale-out failed: "
                                f"{exc}. Will keep monitoring; "
                                "check server2's disk, DHCP, "
                                "and service logs before retrying."
                            )

                            # Prevent repeated provisioning attempts
                            # on every poll after a failure.
                            state["overload_count"] = 0

    except KeyboardInterrupt:
        print(
            "\n[autoscaler] Stopped by user."
        )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
    