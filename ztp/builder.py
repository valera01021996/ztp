import re
import ipaddress
from config import ADMIN_PASSWORD, ROLE_TEMPLATES, DEFAULT_TEMPLATES
from templates import get_jinja_env


def _iface_to_dict(iface) -> dict:
    mode = None
    if iface.mode:
        mode = iface.mode.value if hasattr(iface.mode, "value") else str(iface.mode)
    tagged_vlans = sorted({v.vid for v in (iface.tagged_vlans or [])})
    return {
        "name":          iface.name,
        "description":   iface.description or "",
        "mode":          mode,
        "untagged_vlan": iface.untagged_vlan.vid if iface.untagged_vlan else None,
        "tagged_vlans":  tagged_vlans,
        "ip_address":    None,
        "mtu":           iface.mtu or None,
        "enabled":       iface.enabled,
        "vrf":           iface.vrf.name if iface.vrf else None,
    }


def build_config(nb, device, day0_only: bool = False) -> str:
    platform_slug = device.platform.slug if device.platform else "eos"

    if day0_only:
        base_templates = {"eos": "eos/base.j2", "comware": "comware/base.j2"}
        template_name = base_templates.get(platform_slug, "eos/base.j2")
    else:
        role_slug     = device.role.slug if device.role else ""
        template_name = ROLE_TEMPLATES.get(
            (role_slug, platform_slug),
            DEFAULT_TEMPLATES.get(platform_slug, "eos/default.j2"),
        )

    template = get_jinja_env().get_template(template_name)

    # Management interface / primary IP
    primary_ip = None
    mgmt_iface = None
    gateway    = None
    if device.primary_ip4:
        primary_ip = str(device.primary_ip4)
        net = ipaddress.ip_interface(primary_ip).network
        gateway = str(next(net.hosts()))
        ip_obj = nb.ipam.ip_addresses.get(device.primary_ip4.id)
        mgmt_iface = (
            ip_obj.assigned_object.name
            if (ip_obj and ip_obj.assigned_object)
            else "Management0"
        )

    # Interfaces
    nb_ifaces = sorted(nb.dcim.interfaces.filter(device_id=device.id), key=lambda i: i.name)

    ip_by_iface: dict[int, str] = {}
    for ip in nb.ipam.ip_addresses.filter(device_id=device.id):
        if ip.assigned_object_id and ip.assigned_object_type == "dcim.interface":
            ip_by_iface[ip.assigned_object_id] = str(ip)

    # Маппинг member → LAG: {iface_name: {"lag_name": "Port-Channel1", "lag_id": 1}}
    lag_member_map: dict[str, dict] = {}
    for iface in nb_ifaces:
        if iface.lag:
            lag_name = iface.lag.name
            # Извлекаем номер из Port-Channel1 → 1
            m = re.search(r'(\d+)$', lag_name)
            lag_id = int(m.group(1)) if m else None
            lag_member_map[iface.name] = {"lag_name": lag_name, "lag_id": lag_id}

    interfaces = []
    for iface in nb_ifaces:
        d = _iface_to_dict(iface)
        if iface.id in ip_by_iface:
            d["ip_address"] = ip_by_iface[iface.id]
        if iface.name in lag_member_map:
            d["lag"] = lag_member_map[iface.name]
        interfaces.append(d)

    # VLANs
    needed_vids: set[int] = set()
    for iface in nb_ifaces:
        if iface.untagged_vlan:
            needed_vids.add(iface.untagged_vlan.vid)
        for v in (iface.tagged_vlans or []):
            needed_vids.add(v.vid)

    if mgmt_iface:
        m = re.match(r'^[Vv][Ll][Aa][Nn]\s*(\d+)$', mgmt_iface.strip())
        if m:
            needed_vids.add(int(m.group(1)))

    vlan_map: dict[int, str] = {}
    for vid in needed_vids:
        for vlan in nb.ipam.vlans.filter(vid=vid):
            vlan_map[vid] = vlan.name
            break

    # Добавляем VLANы из vxlan.vlan_vnis — они должны быть созданы на коммутаторе
    vxlan_vnis = (ctx.get("vxlan") or {}).get("vlan_vnis", [])
    warnings = []
    for entry in vxlan_vnis:
        vid = entry.get("vlan")
        if not vid:
            continue
        needed_vids.add(vid)
        if vid not in vlan_map:
            nb_vlan = next(iter(nb.ipam.vlans.filter(vid=vid)), None)
            if nb_vlan:
                vlan_map[vid] = nb_vlan.name
            else:
                vlan_map[vid] = f"VLAN{vid}"
                warnings.append(f"VLAN {vid} используется в VXLAN но не найден в NetBox — создан с именем VLAN{vid}")

    if warnings:
        import logging
        for w in warnings:
            logging.warning("[builder] %s", w)

    vlans = [{"vid": vid, "name": vlan_map[vid]} for vid in sorted(vlan_map)]

    # Router ID — Loopback0 IP
    loopback0_ip = None
    for iface in nb_ifaces:
        if iface.name.lower() == "loopback0" and iface.id in ip_by_iface:
            loopback0_ip = ip_by_iface[iface.id].split("/")[0]
            break

    # ASN from NetBox site
    nb_asn = None
    if device.site:
        site_asns = list(nb.ipam.asns.filter(site_id=device.site.id))
        if site_asns:
            nb_asn = site_asns[0].asn

    # Config context (NetBox мержит role-level fabric.yml + device-level local_context)
    ctx                   = dict(device.config_context) if device.config_context else {}
    ospf_ctx              = ctx.get("ospf", {})
    bgp_ctx               = ctx.get("bgp", {})
    ospf_ifaces           = ctx.get("ospf_interfaces", {})
    hardware              = ctx.get("hardware")
    interface_defaults    = ctx.get("interface_defaults")
    ip_virtual_router_mac = ctx.get("ip_virtual_router_mac")
    vrf_extra             = ctx.get("vrf_extra", {})
    mlag_ctx              = ctx.get("mlag", {})
    vxlan_ctx             = ctx.get("vxlan", {})

    ospf = None
    if ospf_ctx:
        ospf = {**ospf_ctx}
        if loopback0_ip and not ospf.get("router_id"):
            ospf["router_id"] = loopback0_ip

    bgp = None
    if bgp_ctx or nb_asn:
        bgp = {**bgp_ctx}
        if nb_asn and not bgp.get("asn"):
            bgp["asn"] = nb_asn
        if loopback0_ip and not bgp.get("router_id"):
            bgp["router_id"] = loopback0_ip

    mlag = mlag_ctx if mlag_ctx else None

    # VXLAN — мержим vlan_vnis из fabric (vxlan_ctx) с flood_vteps из device context
    vxlan = None
    if vxlan_ctx:
        vxlan = {**vxlan_ctx}

    # VRFs from NetBox — обогащаем данными из vrf_extra
    vrf_ids = {iface.vrf.id for iface in nb_ifaces if iface.vrf}
    vrfs = []
    for vrf_id in vrf_ids:
        vrf_obj = nb.ipam.vrfs.get(vrf_id)
        if not vrf_obj:
            continue
        extra      = vrf_extra.get(vrf_obj.name, {})
        vni        = extra.get("vni")
        rd_suffix  = extra.get("rd_suffix", vni)
        vrfs.append({
            "name":             vrf_obj.name,
            "vni":              vni,
            "rd_suffix":        rd_suffix,
            "route_target":     extra.get("route_target", f"{vni}:{vni}" if vni else ""),
            "route_map_export": extra.get("route_map_export"),
            "import_targets":   [rt.name for rt in (vrf_obj.import_targets or [])],
            "export_targets":   [rt.name for rt in (vrf_obj.export_targets or [])],
        })

    return template.render(
        device=device,
        admin_password=ADMIN_PASSWORD,
        primary_ip=primary_ip,
        mgmt_iface=mgmt_iface,
        gateway=gateway,
        interfaces=interfaces,
        vlans=vlans,
        ospf=ospf,
        ospf_ifaces=ospf_ifaces,
        bgp=bgp,
        mlag=mlag,
        vxlan=vxlan,
        hardware=hardware,
        interface_defaults=interface_defaults,
        vrfs=vrfs,
        ip_virtual_router_mac=ip_virtual_router_mac,
        router_id=loopback0_ip,
    )
