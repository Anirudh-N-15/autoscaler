#!/usr/bin/python3
"""Libvirt-based provisioning for server2. Import provision_server2() from autoscaler.py."""

import os
import secrets
import uuid
import xml.etree.ElementTree as ET

import libvirt

BASE_VM = "server1"
NEW_VM = "server2"
BASE_DISK = "/var/lib/libvirt/images/server1.qcow2"
NEW_DISK = "/var/lib/libvirt/images/server2.qcow2"


def generate_mac():
    # Locally administered, unicast MAC address.
    octets = [0x52, 0x54, 0x00,
              secrets.randbelow(256),
              secrets.randbelow(256),
              secrets.randbelow(256)]
    return ":".join(f"{value:02x}" for value in octets)


def domain_exists(conn, name):
    try:
        conn.lookupByName(name)
        return True
    except libvirt.libvirtError:
        return False


def generate_xml(conn):
    if domain_exists(conn, NEW_VM):
        raise RuntimeError(f"{NEW_VM} is already defined")

    if not os.path.isfile(NEW_DISK):
        raise FileNotFoundError(f"Prepared disk not found: {NEW_DISK}")

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
        if source is not None and source.get("file") == BASE_DISK:
            source.set("file", NEW_DISK)
            disk_found = True

    if not disk_found:
        raise RuntimeError(
            f"Could not find {BASE_DISK} in {BASE_VM}'s XML. "
            "No VM was created."
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
    """Define and start server2; return its libvirt domain object."""
    if domain_exists(conn, NEW_VM):
        domain = conn.lookupByName(NEW_VM)
        if domain.isActive():
            print("[provision] server2 is already running.")
            return domain
        print("[provision] server2 exists but is stopped; starting it.")
        domain.create()
        return domain

    xml = generate_xml(conn)
    domain = conn.defineXML(xml)
    if domain is None:
        raise RuntimeError("libvirt failed to define server2")

    print("[provision] server2 defined. Starting VM...")
    domain.create()
    print("[provision] server2 started.")
    return domain


if __name__ == "__main__":
    # Deliberately do not provision when this module is run directly.
    # Scaling should be triggered by autoscaler.py after sustained overload.
    print("Import provision_server2() from autoscaler.py; do not provision manually.")
