"""
REST API for network device management.

All endpoints:
  - Apply changes on the device (via eAPI / SSH)
  - Sync the change to NetBox (source of truth)
  - Return JSON

Prefix: /api
"""

from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from netbox_client import get_nb, get_device_by_name, get_platform
from adapters.arista import get_eapi

router = APIRouter(prefix="/api", tags=["api"])


# ─── Schemas ────────────────────────────────────────────────────────────────

class InterfaceUpdate(BaseModel):
    """
    Patch an interface. Only supplied fields are applied.

    L2:
      mode: "access" | "trunk"
      access_vlan: 20
      trunk_vlans_add: [10, 20]
      trunk_vlans_remove: [30]

    L3:
      (use /ip endpoints)

    General:
      description: "Link_To_Spine1"
      mtu: 9214
    """
    mode: Optional[str] = None           # "access" | "trunk"
    access_vlan: Optional[int] = None
    trunk_vlans_add: Optional[list[int]] = None
    trunk_vlans_remove: Optional[list[int]] = None
    description: Optional[str] = None
    mtu: Optional[int] = None


class IpBody(BaseModel):
    address: str                          # CIDR, e.g. "10.0.0.1/30"


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _get_or_create_iface(nb, device, iface_name: str):
    nb_iface = nb.dcim.interfaces.get(device_id=device.id, name=iface_name)
    if not nb_iface:
        nb_iface = nb.dcim.interfaces.create(
            device=device.id, name=iface_name, type="1000base-t"
        )
    return nb_iface


def _get_or_create_vlan(nb, vid: int, name: str = "") -> object:
    vlan = next(iter(nb.ipam.vlans.filter(vid=vid)), None)
    if not vlan:
        vlan = nb.ipam.vlans.create({"vid": vid, "name": name or f"VLAN{vid}"})
    return vlan


def _eapi(device):
    if get_platform(device) != "eos":
        raise HTTPException(status_code=400, detail="Only EOS devices are supported")
    return get_eapi(device)


# ─── Devices ─────────────────────────────────────────────────────────────────

@router.get("/devices")
def list_devices():
    """List all devices from NetBox."""
    nb = get_nb()
    devices = []
    for d in nb.dcim.devices.all():
        role = getattr(d, 'role', None) or getattr(d, 'device_role', None)
        devices.append({
            "name":       d.name,
            "platform":   get_platform(d),
            "role":       role.name if role else None,
            "site":       d.site.name if d.site else None,
            "primary_ip": str(d.primary_ip4).split("/")[0] if d.primary_ip4 else None,
            "status":     d.status.value if d.status else None,
        })
    return devices


@router.get("/devices/{name}")
def get_device(name: str):
    """Device details including interfaces and VLANs."""
    nb = get_nb()
    device = get_device_by_name(nb, name)
    role = getattr(device, 'role', None) or getattr(device, 'device_role', None)

    # IPs indexed by interface id
    ip_by_iface: dict[int, list] = {}
    for ip in nb.ipam.ip_addresses.filter(device_id=device.id):
        iface_obj = getattr(ip, 'assigned_object', None)
        if iface_obj:
            role_val = ip.role.value if getattr(ip, 'role', None) else None
            ip_by_iface.setdefault(iface_obj.id, []).append({
                "address": str(ip.address),
                "anycast": role_val == "anycast",
            })

    interfaces = []
    vid_map: dict[int, str] = {}
    for iface in nb.dcim.interfaces.filter(device_id=device.id):
        mode_obj = getattr(iface, 'mode', None)
        untagged = None
        tagged = []
        if iface.untagged_vlan and getattr(iface.untagged_vlan, 'vid', None):
            v = iface.untagged_vlan
            vid_map[v.vid] = v.name
            untagged = {"vid": v.vid, "name": v.name}
        for v in (iface.tagged_vlans or []):
            if getattr(v, 'vid', None):
                vid_map[v.vid] = v.name
                tagged.append({"vid": v.vid, "name": v.name})

        interfaces.append({
            "name":        iface.name,
            "description": iface.description or "",
            "enabled":     iface.enabled,
            "mode":        mode_obj.value if mode_obj else None,
            "lag":         iface.lag.name if getattr(iface, 'lag', None) else None,
            "mtu":         iface.mtu,
            "access_vlan": untagged,
            "trunk_vlans": tagged,
            "ips":         ip_by_iface.get(iface.id, []),
            "tags":        [t.slug for t in (iface.tags or [])],
        })

    return {
        "name":       device.name,
        "platform":   get_platform(device),
        "role":       role.name if role else None,
        "site":       device.site.name if device.site else None,
        "primary_ip": str(device.primary_ip4).split("/")[0] if device.primary_ip4 else None,
        "interfaces": interfaces,
        "vlans":      [{"vid": vid, "name": name} for vid, name in sorted(vid_map.items())],
    }


# ─── Interfaces ──────────────────────────────────────────────────────────────

@router.patch("/devices/{name}/interfaces/{iface}")
def update_interface(name: str, iface: str, body: InterfaceUpdate):
    """
    Configure an interface. Applies to device and updates NetBox.

    Examples:

    Set access VLAN:
      {"mode": "access", "access_vlan": 20}

    Add VLANs to trunk:
      {"mode": "trunk", "trunk_vlans_add": [10, 20, 30]}

    Remove VLANs from trunk:
      {"trunk_vlans_remove": [30]}

    Update description and MTU:
      {"description": "Link_To_Spine1", "mtu": 9214}
    """
    nb = get_nb()
    device = get_device_by_name(nb, name)
    eapi = _eapi(device)
    nb_iface = _get_or_create_iface(nb, device, iface)

    cmds = ["enable", "configure", f"interface {iface}"]
    nb_update = {}
    applied = []

    # Description
    if body.description is not None:
        nb_update["description"] = body.description
        cmds.append(f"description {body.description}" if body.description else "no description")
        applied.append(f"description → '{body.description}'")

    # MTU
    if body.mtu is not None:
        nb_update["mtu"] = body.mtu
        cmds.append(f"mtu {body.mtu}")
        applied.append(f"mtu → {body.mtu}")

    # Access VLAN
    if body.mode == "access" and body.access_vlan is not None:
        vlan_obj = _get_or_create_vlan(nb, body.access_vlan)
        nb_update["mode"] = "access"
        nb_update["untagged_vlan"] = vlan_obj.id
        cmds += ["switchport mode access", f"switchport access vlan {body.access_vlan}"]
        applied.append(f"access vlan → {body.access_vlan}")

    # Trunk — add VLANs
    if body.trunk_vlans_add:
        current_ids = {v.id for v in (nb_iface.tagged_vlans or [])}
        for vid in body.trunk_vlans_add:
            vlan_obj = _get_or_create_vlan(nb, vid)
            current_ids.add(vlan_obj.id)
        nb_update["mode"] = "tagged"
        nb_update["tagged_vlans"] = list(current_ids)
        vids_str = ",".join(str(v) for v in body.trunk_vlans_add)
        cmds += ["switchport mode trunk", f"switchport trunk allowed vlan add {vids_str}"]
        applied.append(f"trunk add → {body.trunk_vlans_add}")

    # Trunk — remove VLANs
    if body.trunk_vlans_remove:
        current_ids = {v.id for v in (nb_iface.tagged_vlans or [])}
        for vid in body.trunk_vlans_remove:
            vlan_obj = next(iter(nb.ipam.vlans.filter(vid=vid)), None)
            if vlan_obj:
                current_ids.discard(vlan_obj.id)
        nb_update["tagged_vlans"] = list(current_ids)
        vids_str = ",".join(str(v) for v in body.trunk_vlans_remove)
        cmds.append(f"switchport trunk allowed vlan remove {vids_str}")
        applied.append(f"trunk remove → {body.trunk_vlans_remove}")

    if not applied:
        raise HTTPException(status_code=400, detail="Nothing to apply — no fields provided")

    # Apply to switch
    eapi.run(cmds)

    # Sync to NetBox
    if nb_update:
        nb_iface.update(nb_update)

    return {
        "status":    "ok",
        "device":    name,
        "interface": iface,
        "applied":   applied,
    }


# ─── IP Management ───────────────────────────────────────────────────────────

@router.post("/devices/{name}/interfaces/{iface}/ip")
def add_ip(name: str, iface: str, body: IpBody):
    """Add IP address to interface. Creates in NetBox and applies on device."""
    nb = get_nb()
    device = get_device_by_name(nb, name)
    eapi = _eapi(device)
    nb_iface = _get_or_create_iface(nb, device, iface)

    # Create or find IP in NetBox
    ip_obj = next(iter(nb.ipam.ip_addresses.filter(address=body.address)), None)
    if not ip_obj:
        ip_obj = nb.ipam.ip_addresses.create({"address": body.address})
    ip_obj.update({
        "assigned_object_type": "dcim.interface",
        "assigned_object_id":   nb_iface.id,
    })

    # Apply on device
    eapi.run([
        "enable", "configure",
        f"interface {iface}",
        "no switchport",
        f"ip address {body.address}",
    ])

    return {"status": "ok", "device": name, "interface": iface, "address": body.address}


@router.delete("/devices/{name}/interfaces/{iface}/ip/{address:path}")
def remove_ip(name: str, iface: str, address: str):
    """Remove IP address from interface. Unassigns in NetBox and removes on device."""
    nb = get_nb()
    device = get_device_by_name(nb, name)
    eapi = _eapi(device)

    # Unassign in NetBox
    ip_obj = next(iter(nb.ipam.ip_addresses.filter(address=address)), None)
    if ip_obj:
        ip_obj.update({"assigned_object_type": None, "assigned_object_id": None})

    # Remove on device
    eapi.run([
        "enable", "configure",
        f"interface {iface}",
        f"no ip address {address}",
    ])

    return {"status": "ok", "device": name, "interface": iface, "address": address}


# ─── VLANs ───────────────────────────────────────────────────────────────────

@router.get("/devices/{name}/vlans")
def list_vlans(name: str):
    """VLANs assigned to interfaces on this device (from NetBox)."""
    nb = get_nb()
    device = get_device_by_name(nb, name)

    vid_map: dict[int, str] = {}
    for iface in nb.dcim.interfaces.filter(device_id=device.id):
        if iface.untagged_vlan and getattr(iface.untagged_vlan, 'vid', None):
            vid_map[iface.untagged_vlan.vid] = iface.untagged_vlan.name
        for v in (iface.tagged_vlans or []):
            if getattr(v, 'vid', None):
                vid_map[v.vid] = v.name

    return [{"vid": vid, "name": name} for vid, name in sorted(vid_map.items())]


@router.post("/devices/{name}/vlans")
def add_vlan_global(name: str, vid: int, vlan_name: str = ""):
    """Create VLAN globally on device (vlan database). Does not assign to any interface."""
    nb = get_nb()
    device = get_device_by_name(nb, name)
    eapi = _eapi(device)

    vlan_obj = _get_or_create_vlan(nb, vid, vlan_name)

    eapi.run([
        "enable", "configure",
        f"vlan {vid}",
        f"name {vlan_name or vlan_obj.name}",
    ])

    return {"status": "ok", "device": name, "vid": vid, "name": vlan_obj.name}


@router.delete("/devices/{name}/vlans/{vid}")
def remove_vlan_global(name: str, vid: int):
    """Remove VLAN from device globally. Does not touch NetBox IPAM."""
    nb = get_nb()
    device = get_device_by_name(nb, name)
    eapi = _eapi(device)

    eapi.run(["enable", "configure", f"no vlan {vid}"])

    return {"status": "ok", "device": name, "vid": vid}
