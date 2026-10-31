#!/usr/bin/python3

import time
from datetime import datetime

import libvirt


# Configuration
LIBVIRT_URI = "qemu:///system"
SERVER_NAMES = ["server1"]

CPU_THRESHOLD = 80.0
POLL_INTERVAL = 3
REQUIRED_OVERLOAD_READINGS = 3


def get_cpu_utilization(domain, previous_cpu_time, previous_time):
    """
    Calculate CPU utilization from two libvirt CPU-time readings.

    Returns:
        utilization: CPU utilization as a percentage
        current_cpu_time: Current cumulative CPU time in nanoseconds
        current_time: Current wall-clock time
    """

    info = domain.info()

    current_cpu_time = info[4]
    current_time = time.monotonic()

    elapsed_time = current_time - previous_time
    cpu_time_delta = current_cpu_time - previous_cpu_time

    if elapsed_time <= 0 or cpu_time_delta < 0:
        return None, current_cpu_time, current_time

    vcpus = info[3]

    if vcpus <= 0:
        return None, current_cpu_time, current_time

    utilization = (
        cpu_time_delta
        / (elapsed_time * vcpus * 1_000_000_000)
    ) * 100.0

    return utilization, current_cpu_time, current_time


def main():
    conn = libvirt.open(LIBVIRT_URI)

    if conn is None:
        raise RuntimeError("Could not connect to libvirt")

    try:
        domains = {}

        # Find the VMs that should be monitored.
        for name in SERVER_NAMES:
            try:
                domain = conn.lookupByName(name)
            except libvirt.libvirtError:
                print(f"ERROR: VM '{name}' is not defined.")
                return

            if domain.isActive() == 0:
                print(f"ERROR: VM '{name}' is not running.")
                return

            info = domain.info()

            domains[name] = {
                "domain": domain,
                "previous_cpu_time": info[4],
                "previous_time": time.monotonic(),
                "overload_count": 0,
            }

        print("Libvirt CPU monitor started.")
        print(f"CPU threshold: {CPU_THRESHOLD:.1f}%")
        print(f"Polling interval: {POLL_INTERVAL} seconds")
        print(
            "Required consecutive overloaded readings: "
            f"{REQUIRED_OVERLOAD_READINGS}"
        )
        print("Press Ctrl+C to stop.\n")

        while True:
            time.sleep(POLL_INTERVAL)

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            for name, state in domains.items():
                domain = state["domain"]

                if domain.isActive() == 0:
                    print(f"[{timestamp}] {name} is no longer running.")
                    return

                utilization, cpu_time, current_time = (
                    get_cpu_utilization(
                        domain,
                        state["previous_cpu_time"],
                        state["previous_time"],
                    )
                )

                state["previous_cpu_time"] = cpu_time
                state["previous_time"] = current_time

                if utilization is None:
                    print(
                        f"[{timestamp}] {name}: "
                        "Unable to calculate CPU utilization."
                    )
                    continue

                print(
                    f"[{timestamp}] {name}: "
                    f"CPU = {utilization:.2f}%"
                )

                if utilization > CPU_THRESHOLD:
                    state["overload_count"] += 1

                    print(
                        f"  Overload reading "
                        f"{state['overload_count']}/"
                        f"{REQUIRED_OVERLOAD_READINGS}"
                    )

                    if (
                        state["overload_count"]
                        >= REQUIRED_OVERLOAD_READINGS
                    ):
                        print(
                            f"\nOVERLOAD DETECTED: {name} exceeded "
                            f"{CPU_THRESHOLD:.1f}% for "
                            f"{REQUIRED_OVERLOAD_READINGS} "
                            "consecutive readings."
                        )
                        print(
                            "Scale-up trigger detected. "
                            "Autoscaler integration is the next step."
                        )
                        return

                else:
                    # A below-threshold reading resets the counter.
                    state["overload_count"] = 0

    except KeyboardInterrupt:
        print("\nCPU monitor stopped.")

    finally:
        conn.close()


if __name__ == "__main__":
    main()