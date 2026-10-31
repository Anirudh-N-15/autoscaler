#!/usr/bin/python3
"""Provision server2 from the base image using only libvirt APIs."""

import xml.etree.ElementTree as ET

import libvirt

# Silence libvirt's "libvirt: ... error :" lines on stderr.
# Exceptions are still raised as normal.
libvirt.registerErrorHandler(lambda ctx, err: None, None)

LIBVIRT_URI = "qemu:///system"
TEMPLATE_VM = "server1"          # its XML is used as the template
NEW_VM = "server2"
POOL_NAME = "default"
BASE_VOLUME = "autoscaler-base.qcow2"
NEW_VOLUME = "server2.qcow2"
NEW_MAC = "52:54:00:aa:bb:02"

# False = full copy of the base (slower, independent).
# True  = qcow2 overlay on the base (created in about a second;
#         the base must never be modified or moved).
USE_OVERLAY = False


def get_pool(conn):
    pool = conn.storagePoolLookupByName(POOL_NAME)
    if not pool.isActive():
        pool.create(0)
    # Rescan the directory so libvirt's volume list matches the disk.
    pool.refresh(0)
    return pool


def remove_stale_volume(pool):
    """Delete a leftover server2 disk (only called when server2 is not defined)."""
    try:
        vol = pool.storageVolLookupByName(NEW_VOLUME)
    except libvirt.libvirtError:
        return
    print(f"[provision] Removing stale disk {vol.path()}")
    vol.delete(0)


def create_disk(pool):
    base = pool.storageVolLookupByName(BASE_VOLUME)
    capacity = base.info()[1]

    if USE_OVERLAY:
        xml = f"""
<volume type='file'>
  <name>{NEW_VOLUME}</name>
  <capacity unit='bytes'>{capacity}</capacity>
  <target>
    <format type='qcow2'/>
    <permissions><mode>0644</mode></permissions>
  </target>
  <backingStore>
    <path>{base.path()}</path>
    <format type='qcow2'/>
  </backingStore>
</volume>"""
        print("[provision] Creating overlay disk on the base image...")
        return pool.createXML(xml, 0)

    xml = f"""
<volume type='file'>
  <name>{NEW_VOLUME}</name>
  <capacity unit='bytes'>{capacity}</capacity>
  <target>
    <format type='qcow2'/>
    <permissions><mode>0644</mode></permissions>
  </target>
</volume>"""
    print("[provision] Cloning base disk through libvirt storage API...")
    return pool.createXMLFrom(xml, base, 0)


def build_domain_xml(conn, disk_path):
    """Derive server2's XML from server1's persistent config."""
    template = conn.lookupByName(TEMPLATE_VM)
    root = ET.fromstring(template.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE))

    root.find("name").text = NEW_VM

    uuid = root.find("uuid")
    if uuid is not None:
        root.remove(uuid)          # libvirt generates a new one

    for disk in root.findall("./devices/disk"):
        if disk.get("device") == "disk":
            disk.find("source").set("file", disk_path)
            backing = disk.find("backingStore")
            if backing is not None:
                disk.remove(backing)
            break
    else:
        raise RuntimeError(f"{TEMPLATE_VM} has no disk to replace")

    iface = root.find("./devices/interface")
    if iface is None:
        raise RuntimeError(f"{TEMPLATE_VM} has no network interface")
    mac = iface.find("mac")
    if mac is None:
        mac = ET.SubElement(iface, "mac")
    mac.set("address", NEW_MAC)

    return ET.tostring(root, encoding="unicode")


def provision_server2(conn):
    """Create server2's disk and domain. Returns the defined domain."""
    try:
        return conn.lookupByName(NEW_VM)
    except libvirt.libvirtError:
        pass

    pool = get_pool(conn)
    remove_stale_volume(pool)

    vol = create_disk(pool)
    try:
        domain = conn.defineXML(build_domain_xml(conn, vol.path()))
    except Exception:
        vol.delete(0)              # don't leave an orphan disk behind
        raise

    print(f"[provision] Defined {NEW_VM} using {vol.path()}")
    return domain