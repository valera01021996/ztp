"""
ZTP Server — FastAPI.

Endpoints:
  GET  /ztp.py                                → отдаёт ZTP скрипт коммутатору
  GET  /config/{serial}                       → генерирует EOS конфиг из NetBox по серийнику
  GET  /ztp-done/{serial}                     → помечает устройство в NetBox как задеплоенное

  GET  /devices/{name}/vlans                  → список VLAN с коммутатора
  POST /devices/{name}/vlans                  → добавить VLAN
  DELETE /devices/{name}/vlans/{vid}          → удалить VLAN
  POST /devices/{name}/trunk                  → добавить VLAN в транк
  POST /devices/{name}/access                 → настроить access-порт

Запуск:
  pip install -r requirements.txt
  cp .env.example .env  # заполни NETBOX_URL, NETBOX_TOKEN
  uvicorn app:app --host 0.0.0.0 --port 80
"""

import os
import re
import ssl
import json
import ipaddress
import urllib.request
import urllib.error
from base64 import b64encode

from jinja2 import Environment, FileSystemLoader

import pynetbox
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="ZTP Server")

NETBOX_URL      = os.environ["NETBOX_URL"]
NETBOX_TOKEN    = os.environ["NETBOX_TOKEN"]
ADMIN_PASSWORD  = os.environ.get("ADMIN_PASSWORD", "123456")
SWITCH_USER     = os.environ.get("SWITCH_USER", "admin")
SWITCH_PASSWORD = os.environ.get("SWITCH_PASSWORD", ADMIN_PASSWORD)

ZTP_SCRIPT_PATH     = os.path.join(os.path.dirname(__file__), "ztp_script.py")
H3C_ZTP_SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "h3c_ztp_script.py")
TEMPLATES_DIR       = os.path.join(os.path.dirname(__file__), "templates")

# (role_slug, platform_slug) → template file (relative to TEMPLATES_DIR)
ROLE_TEMPLATES = {
    ("data-sw", "eos"):     "eos/data-sw.j2",
    ("oam",     "eos"):     "eos/oam.j2",
    ("leaf",    "eos"):     "eos/leaf.j2",
    ("data-sw", "comware"): "comware/data-sw.j2",
    ("oam",     "comware"): "comware/oam.j2",
    ("leaf",    "comware"): "comware/leaf.j2",
}
DEFAULT_TEMPLATES = {
    "eos":     "eos/default.j2",
    "comware": "comware/default.j2",
}

def _cidr_to_mask(cidr: str) -> str:
    """'192.168.1.1/24' → '192.168.1.1 255.255.255.0'  (H3C/Comware format)"""
    iface = ipaddress.ip_interface(cidr)
    return f"{iface.ip} {iface.netmask}"

_jinja_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)
_jinja_env.filters["cidr_to_mask"] = _cidr_to_mask
_jinja_env.tests["match"] = lambda value, pattern: bool(re.match(pattern, str(value)))


def get_nb():
    return pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)


# ─── Arista eAPI client ───────────────────────────────────────────────────────

class AristaEAPI:
    def __init__(self, ip: str, username: str, password: str):
        self.url = f"http://{ip}/command-api"
        creds = b64encode(f"{username}:{password}".encode()).decode()
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Basic {creds}",
        }

    def run(self, commands: list) -> dict:
        payload = json.dumps({
            "jsonrpc": "2.0",
            "method": "runCmds",
            "params": {"version": 1, "cmds": commands, "format": "json"},
            "id": 1,
        }).encode()

        req = urllib.request.Request(self.url, data=payload, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            raise HTTPException(status_code=502, detail=f"eAPI HTTP error {e.code}: {body}")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"eAPI connection error: {e}")

        if "error" in result:
            err = result["error"]
            raise HTTPException(status_code=502, detail=f"eAPI error {err.get('code')}: {err.get('message')}")

        return result.get("result", [])


def get_device_by_name(nb, name: str):
    """Ищет устройство в NetBox по имени."""
    device = nb.dcim.devices.get(name=name)
    if not device:
        raise HTTPException(status_code=404, detail=f"Устройство '{name}' не найдено в NetBox")
    return device


def get_eapi(device) -> AristaEAPI:
    """Принимает pynetbox device, возвращает готовый eAPI клиент."""
    if not device.primary_ip4:
        raise HTTPException(status_code=400, detail=f"У устройства {device.name} не задан primary_ip4")
    ip = str(device.primary_ip4).split("/")[0]
    return AristaEAPI(ip, SWITCH_USER, SWITCH_PASSWORD)


# ─── ZTP endpoints ────────────────────────────────────────────────────────────

@app.get("/ztp.py", response_class=PlainTextResponse)
def serve_ztp_script():
    """Arista EOS скачивает этот скрипт и выполняет его при ZTP."""
    with open(ZTP_SCRIPT_PATH) as f:
        return f.read()


@app.get("/h3c-ztp.py", response_class=PlainTextResponse)
def serve_h3c_ztp_script():
    """H3C Comware скачивает этот скрипт и выполняет его при ZTP."""
    with open(H3C_ZTP_SCRIPT_PATH) as f:
        return f.read()


@app.get("/config/{serial}", response_class=PlainTextResponse)
def get_config(serial: str):
    """Возвращает EOS startup-config для устройства с указанным серийником."""
    nb = get_nb()
    device = nb.dcim.devices.get(serial=serial)
    if not device:
        raise HTTPException(status_code=404, detail=f"serial={serial} не найден в NetBox")
    return build_eos_startup_config(nb, device)


@app.get("/ztp-done/{serial}")
def ztp_done(serial: str):
    """Вызывается коммутатором после успешного применения конфига."""
    nb = get_nb()
    device = nb.dcim.devices.get(serial=serial)
    if not device:
        raise HTTPException(status_code=404, detail=f"serial={serial} не найден")

    tags = [t for t in (device.tags or []) if t.slug != "config-pending"]
    deployed_tag = _get_or_create_tag(nb, "config-deployed", "config-deployed", "4caf50")
    tags.append(deployed_tag)
    device.update({"tags": [{"id": t.id} for t in tags], "status": "active"})

    return {"status": "ok", "device": device.name}


# ─── Device management endpoints ─────────────────────────────────────────────

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


@app.get("/devices/{name}/vlans")
def list_vlans(name: str):
    """
    Возвращает VLANы из NetBox (source of truth).
    Дополнительно проверяет что они реально есть на коммутаторе.
    """
    nb = get_nb()
    device = get_device_by_name(nb, name)

    # Берём VLANы из NetBox привязанные к сайту устройства
    site_id = device.site.id if device.site else None
    nb_vlans = {
        v.vid: v.name
        for v in nb.ipam.vlans.filter(site_id=site_id)
    }

    # Проверяем фактическое состояние на коммутаторе
    eapi = get_eapi(device)
    result = eapi.run(["show vlan"])
    switch_vids = set(
        int(vid) for vid in result[0].get("vlans", {})
        if vid != "1"
    )

    return [
        {
            "vid": vid,
            "name": name,
            "on_switch": vid in switch_vids,
        }
        for vid, name in sorted(nb_vlans.items())
    ]


@app.post("/devices/{name}/vlans")
def add_vlan(name: str, body: VlanIn):
    """
    Добавить VLAN:
    1. Создать в NetBox (если не существует)
    2. Применить на коммутаторе
    """
    nb = get_nb()
    device = get_device_by_name(nb, name)
    site_id = device.site.id if device.site else None

    # 1. NetBox
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

    # 2. Коммутатор
    eapi = get_eapi(device)
    commands = ["enable", "configure", f"vlan {body.vid}"]
    if body.name:
        commands.append(f"name {body.name}")
    eapi.run(commands)

    return {"status": "ok", "vid": body.vid, "name": body.name, "netbox": nb_status}


@app.delete("/devices/{name}/vlans/{vid}")
def delete_vlan(name: str, vid: int):
    """
    Удалить VLAN:
    1. Удалить с коммутатора
    2. Удалить из NetBox
    """
    nb = get_nb()
    device = get_device_by_name(nb, name)
    site_id = device.site.id if device.site else None

    # 1. Коммутатор
    eapi = get_eapi(device)
    eapi.run(["enable", "configure", f"no vlan {vid}"])

    # 2. NetBox
    nb_status = "not_found"
    for vlan in nb.ipam.vlans.filter(vid=vid, site_id=site_id):
        vlan.delete()
        nb_status = "deleted"
        break

    return {"status": "ok", "vid": vid, "netbox": nb_status}


@app.post("/devices/{name}/trunk")
def add_vlan_to_trunk(name: str, body: TrunkIn):
    """
    Добавить VLAN(ы) в транк:
    1. Обновить tagged_vlans интерфейса в NetBox
    2. Применить на коммутаторе
    """
    nb = get_nb()
    device = get_device_by_name(nb, name)

    # 1. NetBox — обновляем интерфейс
    nb_iface = nb.dcim.interfaces.get(device_id=device.id, name=body.interface)
    if not nb_iface:
        raise HTTPException(status_code=404, detail=f"Интерфейс {body.interface} не найден в NetBox")

    # Собираем текущие tagged_vlans + новые
    current_vids = {v.id for v in (nb_iface.tagged_vlans or [])}
    site_id = device.site.id if device.site else None

    for vid in body.vlans:
        vlans = list(nb.ipam.vlans.filter(vid=vid, site_id=site_id))
        if not vlans:
            raise HTTPException(
                status_code=400,
                detail=f"VLAN {vid} не найден в NetBox — сначала создайте его через POST /devices/{name}/vlans"
            )
        current_vids.add(vlans[0].id)

    nb_iface.update({
        "mode": "tagged",
        "tagged_vlans": list(current_vids),
    })

    # 2. Коммутатор
    eapi = get_eapi(device)
    vids_str = ",".join(str(v) for v in body.vlans)
    eapi.run([
        "enable",
        "configure",
        f"interface {body.interface}",
        "switchport mode trunk",
        f"switchport trunk allowed vlan add {vids_str}",
    ])

    return {"status": "ok", "interface": body.interface, "vlans_added": body.vlans}


@app.post("/devices/{name}/access")
def set_access_port(name: str, body: AccessIn):
    """
    Настроить access-порт:
    1. Проверить что VLAN существует в NetBox
    2. Обновить интерфейс в NetBox (mode=access, untagged_vlan, description)
    3. Применить на коммутаторе
    """
    nb = get_nb()
    device = get_device_by_name(nb, name)
    site_id = device.site.id if device.site else None

    # Проверяем что VLAN есть в NetBox
    vlans = list(nb.ipam.vlans.filter(vid=body.vlan, site_id=site_id))
    if not vlans:
        raise HTTPException(
            status_code=400,
            detail=f"VLAN {body.vlan} не найден в NetBox — сначала создайте через POST /devices/{name}/vlans"
        )
    vlan_obj = vlans[0]

    # Ищем интерфейс в NetBox
    nb_iface = nb.dcim.interfaces.get(device_id=device.id, name=body.interface)
    if not nb_iface:
        raise HTTPException(status_code=404, detail=f"Интерфейс {body.interface} не найден в NetBox")

    # Обновляем интерфейс в NetBox
    update = {"mode": "access", "untagged_vlan": vlan_obj.id}
    if body.description:
        update["description"] = body.description
    nb_iface.update(update)

    # Применяем на коммутаторе
    eapi = get_eapi(device)
    commands = [
        "enable", "configure",
        f"interface {body.interface}",
        "switchport mode access",
        f"switchport access vlan {body.vlan}",
    ]
    if body.description:
        commands.append(f"description {body.description}")
    eapi.run(commands)

    return {"status": "ok", "interface": body.interface, "vlan": body.vlan, "description": body.description}


# ─── Config builder ───────────────────────────────────────────────────────────

def _iface_to_dict(iface) -> dict:
    """Convert a pynetbox interface record to a plain dict for Jinja2."""
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
        "ip_address":    None,  # filled below for leaf routed ports
        "mtu":           iface.mtu or None,
        "enabled":       iface.enabled,
        "vrf":           iface.vrf.name if iface.vrf else None,
    }


def build_eos_startup_config(nb, device) -> str:
    role_slug     = device.role.slug     if device.role     else ""
    platform_slug = device.platform.slug if device.platform else "eos"
    template_name = ROLE_TEMPLATES.get(
        (role_slug, platform_slug),
        DEFAULT_TEMPLATES.get(platform_slug, "eos/default.j2"),
    )
    template = _jinja_env.get_template(template_name)

    # ── Management interface / primary IP ────────────────────────────────────
    primary_ip = None
    mgmt_iface = None
    gateway = None
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

    # ── Interfaces ────────────────────────────────────────────────────────────
    nb_ifaces = sorted(
        nb.dcim.interfaces.filter(device_id=device.id),
        key=lambda i: i.name,
    )

    # For leaf: attach IP addresses to routed interfaces
    ip_by_iface: dict[int, str] = {}
    if role_slug == "leaf":
        for ip in nb.ipam.ip_addresses.filter(device_id=device.id):
            if ip.assigned_object_id and ip.assigned_object_type == "dcim.interface":
                ip_by_iface[ip.assigned_object_id] = str(ip)

    interfaces = []
    for iface in nb_ifaces:
        d = _iface_to_dict(iface)
        if iface.id in ip_by_iface:
            d["ip_address"] = ip_by_iface[iface.id]
        interfaces.append(d)

    # ── VLANs (for L2 roles) ──────────────────────────────────────────────────
    needed_vids: set[int] = set()
    for iface in nb_ifaces:
        if iface.untagged_vlan:
            needed_vids.add(iface.untagged_vlan.vid)
        for v in (iface.tagged_vlans or []):
            needed_vids.add(v.vid)

    vlan_map: dict[int, str] = {}
    for vid in needed_vids:
        for vlan in nb.ipam.vlans.filter(vid=vid):
            vlan_map[vid] = vlan.name
            break
    vlans = [{"vid": vid, "name": vlan_map[vid]} for vid in sorted(vlan_map)]

    # ── Router ID — IP Loopback0 без маски ───────────────────────────────────
    loopback0_ip = None
    for iface in nb_ifaces:
        if iface.name.lower() == "loopback0" and iface.id in ip_by_iface:
            loopback0_ip = ip_by_iface[iface.id].split("/")[0]
            break

    # ── ASN — из NetBox (сайт устройства) ────────────────────────────────────
    nb_asn = None
    if device.site:
        site_asns = list(nb.ipam.asns.filter(site_id=device.site.id))
        if site_asns:
            nb_asn = site_asns[0].asn

    # ── Config context ────────────────────────────────────────────────────────
    ctx = dict(device.config_context) if device.config_context else {}
    ospf_ctx           = ctx.get("ospf", {})
    bgp_ctx            = ctx.get("bgp", {})
    ospf_ifaces        = ctx.get("ospf_interfaces", {})
    hardware           = ctx.get("hardware")
    interface_defaults = ctx.get("interface_defaults")
    ip_virtual_router_mac = ctx.get("ip_virtual_router_mac")
    vrf_extra          = ctx.get("vrf_extra", {})

    # OSPF: router_id берём из Loopback0, остальное из config_context
    ospf = None
    if ospf_ctx:
        ospf = {**ospf_ctx}
        if loopback0_ip and not ospf.get("router_id"):
            ospf["router_id"] = loopback0_ip

    # BGP: asn из NetBox (приоритет), router_id из Loopback0
    bgp = None
    if bgp_ctx or nb_asn:
        bgp = {**bgp_ctx}
        if nb_asn:
            bgp["asn"] = nb_asn
        if loopback0_ip and not bgp.get("router_id"):
            bgp["router_id"] = loopback0_ip

    # ── VRFs from NetBox IPAM (RD + Route Targets) ────────────────────────────
    vrf_ids = {iface.vrf.id for iface in nb_ifaces if iface.vrf}
    vrfs = []
    for vrf_id in vrf_ids:
        vrf_obj = nb.ipam.vrfs.get(vrf_id)
        if not vrf_obj:
            continue
        extra = vrf_extra.get(vrf_obj.name, {})

        # RD: генерируем из loopback0_ip + vni если не задан явно в extra
        # Формат: loopback_ip:vni  (например 1.1.3.1:50000)
        vni = extra.get("vni")
        if loopback0_ip and vni:
            rd = f"{loopback0_ip}:{vni}"
        else:
            # Fallback: RD из NetBox VRF (статический, одинаковый для всех)
            rd = vrf_obj.rd or ""

        vrfs.append({
            "name":           vrf_obj.name,
            "rd":             rd,
            "import_targets": [rt.name for rt in (vrf_obj.import_targets or [])],
            "export_targets": [rt.name for rt in (vrf_obj.export_targets or [])],
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
        hardware=hardware,
        interface_defaults=interface_defaults,
        vrfs=vrfs,
        ip_virtual_router_mac=ip_virtual_router_mac,
        router_id=loopback0_ip,
    )


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_or_create_tag(nb, slug: str, name: str, color: str = "00bcd4"):
    tag = nb.extras.tags.get(slug=slug)
    if not tag:
        tag = nb.extras.tags.create({"name": name, "slug": slug, "color": color})
    return tag
