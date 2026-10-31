#!/usr/bin/python3
import libvirt
from provision import LIBVIRT_URI, NEW_VM, NEW_VOLUME, get_pool

conn = libvirt.open(LIBVIRT_URI)
try:
    dom = conn.lookupByName(NEW_VM)
    if dom.isActive():
        dom.destroy()
    dom.undefine()
    print(f"Removed domain {NEW_VM}")
except libvirt.libvirtError:
    print(f"{NEW_VM} not defined")

pool = get_pool(conn)
try:
    pool.storageVolLookupByName(NEW_VOLUME).delete(0)
    print(f"Deleted volume {NEW_VOLUME}")
except libvirt.libvirtError:
    print("No server2 volume to delete")
conn.close()