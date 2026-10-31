#!/usr/bin/python3
"""Libvirt-based provisioning for server2."""

import secrets
import uuid
import xml.etree.ElementTree as ET

import libvirt

LIBVIRT_URI = "qemu:///system"
BASE_VM = "server1"
NEW_VM = "server2"

POOL_NAME = "default"
BASE_DISK = "/var/lib/libvirt/images/autoscaler-base.qcow2"
NEW_DISK = "/var/lib/libvirt/images/server2.qcow2"


def generate_mac():
    """Generate a locally administered, unicast MAC address."""
    octets = [
        0x52,
        0x54,
        0x00,
        secrets.randbelow(256),
        secrets.randbelow(256),
        secrets.randbelow(256),
    ]
    return ":".join(f"{value:02x}" for value in octets)


def domain_exists(conn, name):
    try:
        conn.lookupByName(name)
        return True
    except libvirt.libvirtError:
        return False


def clone_base_disk(conn):
    """Clone the prepared base disk through the libvirt storage API."""
    try:
        conn.storageVolLookupByPath(NEW_DISK)
    except libvirt.libvirtError:
        pass
    else:
        raise RuntimeError(
            f"Destination disk already exists: {NEW_DISK}. "
            "Inspect it before deleting or reusing it."
        )

    source = conn.storageVolLookupByPath(BASE_DISK)
    pool = conn.storagePoolLookupByName(POOL_NAME)

    source_xml = ET.fromstring(source.XMLDesc(0))
    capacity = int(source_xml.findtext("capacity"))

    volume_xml = f"""
    <volume>
      <name>server2.qcow2</name>
      <capacity unit="bytes">{capacity}</capacity>
      <target>
        <format type="qcow2"/>
      </target>
    </volume>
    """

    print("[provision] Cloning base disk through libvirt storage API...")
    volume = pool.createXMLFrom(volume_xml, source, 0)

    print(f"[provision] Created disk: {volume.path()}")
    return volume


def generate_xml(conn):
    """Generate server2's domain XML from server1's configuration."""
    if domain_exists(conn, NEW_VM):
        raise RuntimeError(f"{NEW_VM} is already defined")

    base = conn.lookupByName(BASE_VM)
    root = ET.fromstring(base.XMLDesc(0))

    root.find("name").text = NEW_VM

    uuid_node = root.find("uuid")
    if uuid_node is not None:
        uuid_node.text = str(uuid.uuid4())

    disk_found = False

    for disk in root.findall("./devices/disk"):
        if disk.get("device") != "disk":
            continue

        source = disk.find("source")

        if source is not None and source.get("file") == (
            "/var/lib/libvirt/images/server1.qcow2"
        ):
            source.set("file", NEW_DISK)
            disk_found = True

    if not disk_found:
        raise RuntimeError(
            "Could not find server1.qcow2 in server1's domain XML."
        )

    interfaces = root.findall("./devices/interface")

    if not interfaces:
        raise RuntimeError(f"{BASE_VM} has no network interface")

    mac_node = interfaces[0].find("mac")

    if mac_node is None:
        mac_node = ET.SubElement(interfaces[0], "mac")

    mac_node.set("address", generate_mac())

    return ET.tostring(root, encoding="unicode")


def provision_server2(conn):
    """Create and start server2; return its libvirt domain object."""
    if domain_exists(conn, NEW_VM):
        domain = conn.lookupByName(NEW_VM)

        if domain.isActive():
            print("[provision] server2 is already running.")
            return domain

        print("[provision] server2 exists but is stopped; starting it.")
        domain.create()
        return domain

    disk_created = False
    domain_defined = False

    try:
        clone_base_disk(conn)
        disk_created = True

        xml = generate_xml(conn)
        domain = conn.defineXML(xml)

        if domain is None:
            raise RuntimeError("libvirt failed to define server2")

        domain_defined = True

        print("[provision] server2 defined. Starting VM...")
        domain.create()

        print("[provision] server2 started.")
        return domain

    except Exception:
        # Clean up only resources created during this attempt.
        if domain_defined:
            try:
                domain = conn.lookupByName(NEW_VM)

                if domain.isActive():
                    domain.destroy()

                domain.undefine()
            except libvirt.libvirtError:
                pass

        if disk_created:
            try:
                volume = conn.storageVolLookupByPath(NEW_DISK)
                volume.delete(0)
                print("[provision] Removed incomplete server2 disk.")
            except libvirt.libvirtError:
                pass

        raise


if __name__ == "__main__":
    print("Import provision_server2() from autoscaler.py.")