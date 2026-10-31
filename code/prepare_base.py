#!/usr/bin/env python3

import libvirt
import os
import sys

POOL_NAME = "default"

SOURCE_DISK = "/var/lib/libvirt/images/server1.qcow2"
BASE_DISK_NAME = "autoscaler-base.qcow2"

BASE_DISK_PATH = f"/var/lib/libvirt/images/{BASE_DISK_NAME}"


def main():
    conn = libvirt.open("qemu:///system")

    if conn is None:
        raise RuntimeError("Could not connect to libvirt")

    try:
        # Never copy a disk while its source VM is running.
        source_vm = conn.lookupByName("server1")

        if source_vm.isActive():
            raise RuntimeError(
                "server1 is running. Shut it down before preparing "
                "the base image."
            )

        if os.path.exists(BASE_DISK_PATH):
            raise RuntimeError(
                f"{BASE_DISK_PATH} already exists. "
                "Refusing to overwrite it."
            )

        pool = conn.storagePoolLookupByName(POOL_NAME)

        if not pool.isActive():
            pool.create(0)

        # Look up the existing source volume through libvirt.
        source_volume = conn.storageVolLookupByPath(SOURCE_DISK)

        # Preserve the source volume's capacity.
        capacity = source_volume.info()[1]

        volume_xml = f"""
        <volume>
          <name>{BASE_DISK_NAME}</name>
          <capacity unit="bytes">{capacity}</capacity>
          <target>
            <format type="qcow2"/>
          </target>
        </volume>
        """

        print("Creating base disk through the libvirt storage API...")

        base_volume = pool.createXMLFrom(
            volume_xml,
            source_volume,
            0
        )

        print(f"Base image created: {base_volume.path()}")
        print(f"Capacity: {capacity} bytes")

    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
