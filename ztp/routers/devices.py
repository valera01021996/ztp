from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from netbox_client import get_nb, get_device_by_name, get_platform
from adapters.arista import get_eapi
from adapters.h3c import get_h3c

router = APIRouter(prefix="/devices")


class VlanIn(BaseModel):
    vid: int
    name: str = ""


class TrunkIn(BaseModel):
    interface: str
    vlans: list


class AccessIn(BaseModel):
    interface: str
    vlan: int
    description: str = ""


@router.get("/{name}/vlans")
def list_vlans(name: str):
    nb = get_nb()
    device = get_device_by_name(nb, name)
    site_id  = device.site.id if device.site else None
    platform = get_platform(device)

    nb_vlans = {v.vid: v.name for v in nb.ipam.vlans.filter(site_id=site_id)}

    if platform == "comware":
        switch_vids = {v["vid"] for v in get_h3c(device).get_vlans()}
    else:
        result = get_eapi(device).run(["show vlan"])
        switch_vids = {int(v) for v in result[0].get("vlans", {}) if v != "1"}

    return [
        {"vid": vid, "name": vname, "on_switch": vid in switch_vids}
        for vid, vname in sorted(nb_vlans.items())
    ]


@router.post("/{name}/vlans")
def add_vlan(name: str, body: VlanIn):
    nb = get_nb()
    device = get_device_by_name(nb, name)
    site_id  = device.site.id if device.site else None
    platform = get_platform(device)

    nb_status = "created"
    existing = list(nb.ipam.vlans.filter(vid=body.vid, site_id=site_id))
    if existing:
        nb_status = "already_exists"
    else:
        try:
            nb.ipam.vlans.create({
                "vid": body.vid,
                "name": body.name or f"VLAN{body.vid}",
                "site": site_id,
                "status": "active",
            })
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"NetBox error: {e}")

    if platform == "comware":
        get_h3c(device).create_vlan(body.vid, body.name)
    else:
        cmds = ["enable", "configure", f"vlan {body.vid}"]
        if body.name:
            cmds.append(f"name {body.name}")
        get_eapi(device).run(cmds)

    return {"status": "ok", "vid": body.vid, "name": body.name, "netbox": nb_status}


@router.delete("/{name}/vlans/{vid}")
def delete_vlan(name: str, vid: int):
    nb = get_nb()
    device = get_device_by_name(nb, name)
    site_id  = device.site.id if device.site else None
    platform = get_platform(device)

    if platform == "comware":
        get_h3c(device).delete_vlan(vid)
    else:
        get_eapi(device).run(["enable", "configure", f"no vlan {vid}"])

    nb_status = "not_found"
    for vlan in nb.ipam.vlans.filter(vid=vid, site_id=site_id):
        vlan.delete()
        nb_status = "deleted"
        break

    return {"status": "ok", "vid": vid, "netbox": nb_status}


@router.post("/{name}/trunk")
def add_vlan_to_trunk(name: str, body: TrunkIn):
    nb = get_nb()
    device = get_device_by_name(nb, name)
    site_id  = device.site.id if device.site else None
    platform = get_platform(device)

    nb_iface = nb.dcim.interfaces.get(device_id=device.id, name=body.interface)
    if not nb_iface:
        nb_iface = nb.dcim.interfaces.create(
            device=device.id, name=body.interface, type="1000base-t"
        )

    current_vids = {v.id for v in (nb_iface.tagged_vlans or [])}
    vlan_ids_int = []
    for vid in body.vlans:
        vlans = list(nb.ipam.vlans.filter(vid=vid, site_id=site_id))
        if not vlans:
            raise HTTPException(status_code=400,
                detail=f"VLAN {vid} не найден в NetBox")
        current_vids.add(vlans[0].id)
        vlan_ids_int.append(int(vid))

    nb_iface.update({"mode": "tagged", "tagged_vlans": list(current_vids)})

    if platform == "comware":
        get_h3c(device).set_trunk_vlans(body.interface, vlan_ids_int)
    else:
        vids_str = ",".join(str(v) for v in body.vlans)
        get_eapi(device).run([
            "enable", "configure",
            f"interface {body.interface}",
            "switchport mode trunk",
            f"switchport trunk allowed vlan add {vids_str}",
        ])

    return {"status": "ok", "interface": body.interface, "vlans_added": body.vlans}


@router.post("/{name}/access")
def set_access_port(name: str, body: AccessIn):
    nb = get_nb()
    device = get_device_by_name(nb, name)
    site_id  = device.site.id if device.site else None
    platform = get_platform(device)

    vlans = list(nb.ipam.vlans.filter(vid=body.vlan, site_id=site_id))
    if not vlans:
        raise HTTPException(status_code=400, detail=f"VLAN {body.vlan} не найден в NetBox")
    vlan_obj = vlans[0]

    nb_iface = nb.dcim.interfaces.get(device_id=device.id, name=body.interface)
    if not nb_iface:
        nb_iface = nb.dcim.interfaces.create(
            device=device.id, name=body.interface, type="1000base-t"
        )

    update = {"mode": "access", "untagged_vlan": vlan_obj.id}
    if body.description:
        update["description"] = body.description
    nb_iface.update(update)

    if platform == "comware":
        get_h3c(device).set_access_vlan(body.interface, body.vlan, body.description)
    else:
        cmds = ["enable", "configure", f"interface {body.interface}",
                "switchport mode access", f"switchport access vlan {body.vlan}"]
        if body.description:
            cmds.append(f"description {body.description}")
        get_eapi(device).run(cmds)

    return {"status": "ok", "interface": body.interface, "vlan": body.vlan}


@router.post("/{name}/rollback")
def rollback_to_day0(name: str):
    """Откатить до Day0 конфига (base.j2) — только hostname, mgmt, SSH, eAPI."""
    from builder import build_config
    from pipeline import deploy_config

    nb = get_nb()
    device = get_device_by_name(nb, name)
    platform = get_platform(device)

    if platform != "eos":
        raise HTTPException(status_code=400, detail="Rollback поддерживается только для EOS")

    config = build_config(nb, device, day0_only=True)
    deploy_config(device, config)
    return {"status": "ok", "device": name, "message": "Rolled back to Day0 config"}


@router.post("/{name}/reset")
def reset_config(name: str):
    """Сбросить конфиг до минимального (hostname + mgmt) для тестирования."""
    nb = get_nb()
    device = get_device_by_name(nb, name)
    platform = get_platform(device)

    if platform != "eos":
        raise HTTPException(status_code=400, detail="Reset поддерживается только для EOS")

    if not device.primary_ip4:
        raise HTTPException(status_code=400, detail="primary_ip4 не задан")

    mgmt_ip = str(device.primary_ip4)
    import ipaddress
    gw = str(next(ipaddress.ip_interface(mgmt_ip).network.hosts()))

    clean_config = f"""hostname {device.name}
username admin privilege 15 role network-admin secret 0 admin
aaa authentication login default local
management ssh
   no shutdown
management api http-commands
   protocol http
   no shutdown
interface Management1
   ip address {mgmt_ip}
   no shutdown
ip route 0.0.0.0/0 {gw}
"""

    cmds = ["enable", "configure terminal"]
    for line in clean_config.splitlines():
        stripped = line.strip()
        if stripped:
            cmds.append(stripped)
    cmds.append("end")
    cmds.append("write memory")

    get_eapi(device).run(cmds)
    return {"status": "ok", "device": name, "message": "Config reset to minimal"}
