#!/usr/bin/env python3
# setup_server1.py
import libvirt

BASE_IMAGE = "/var/lib/libvirt/images/base.qcow2"
SERVER1_DISK = "/var/lib/libvirt/images/server1.qcow2"
SERVER1_MAC = "52:54:00:aa:bb:01"

VOLUME_XML = f"""
<volume>
  <name>server1.qcow2</name>
  <capacity unit="G">10</capacity>
  <target>
    <path>{SERVER1_DISK}</path>
    <format type='qcow2'/>
  </target>
  <backingStore>
    <path>{BASE_IMAGE}</path>
    <format type='qcow2'/>
  </backingStore>
</volume>
"""

with open("domain_template.xml", "r") as f:
    TEMPLATE_XML = f.read()


def main():
    conn = libvirt.open("qemu:///system")
    pool = conn.storagePoolLookupByName("default")

    # Clean up existing server1 if present
    try:
        dom = conn.lookupByName("server1")
        if dom.isActive():
            dom.destroy()
        dom.undefine()
    except libvirt.libvirtError:
        pass

    try:
        vol = pool.storageVolLookupByName("server1.qcow2")
        vol.delete(0)
    except libvirt.libvirtError:
        pass

    pool.createXML(VOLUME_XML, 0)
    dom_xml = (
        TEMPLATE_XML.replace("{{VM_NAME}}", "server1")
        .replace("{{DISK_PATH}}", SERVER1_DISK)
        .replace("{{MAC_ADDR}}", SERVER1_MAC)
    )
    dom = conn.defineXML(dom_xml)
    dom.create()
    print("server1 created and started successfully.")
    conn.close()


if __name__ == "__main__":
    main()