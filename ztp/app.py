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
from fastapi import FastAPI, HTTPException, Request
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
def _comware_iface_name(name: str) -> str:
    """Vlan10 / VLAN10 / Vlan 10  →  Vlan-interface10 (H3C Comware naming)."""
    m = re.match(r'^[Vv][Ll][Aa][Nn]\s*(\d+)$', name.strip())
    if m:
        return "Vlan-interface" + m.group(1)
    return name

_jinja_env.filters["cidr_to_mask"]  = _cidr_to_mask
_jinja_env.filters["comware_iface"] = _comware_iface_name
_jinja_env.tests["match"]      = lambda value, pattern: bool(re.match(pattern, str(value)))
_jinja_env.tests["vlan_iface"] = lambda value: bool(
    re.match(r'^[Vv][Ll][Aa][Nn]\s*\d+$', str(value).strip())
)


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


# ─── H3C NETCONF client ───────────────────────────────────────────────────────

H3C_NS_CFG  = "http://www.h3c.com/netconf/config:1.0"
H3C_NS_DATA = "http://www.h3c.com/netconf/data:1.0"

class H3CNetconf:
    def __init__(self, ip: str, username: str, password: str, port: int = 830):
        self.ip       = ip
        self.username = username
        self.password = password
        self.port     = port

    def _connect(self):
        from ncclient import manager
        return manager.connect(
            host=self.ip,
            port=self.port,
            username=self.username,
            password=self.password,
            hostkey_verify=False,
            device_params={"name": "h3c"},
            timeout=15,
        )

    def get_vlans(self) -> list[dict]:
        filter_xml = f"""
        <filter type="subtree">
          <top xmlns="{H3C_NS_DATA}">
            <VLAN><VLANs/></VLAN>
          </top>
        </filter>"""
        try:
            with self._connect() as m:
                result = m.get(filter_xml)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"NETCONF error: {e}")

        import xml.etree.ElementTree as ET
        root = ET.fromstring(str(result))
        vlans = []
        ns = {"h3c": H3C_NS_DATA}
        for vlan in root.findall(".//h3c:VLANID", ns):
            vid_el  = vlan.find("h3c:ID", ns)
            name_el = vlan.find("h3c:Name", ns)
            if vid_el is not None:
                vlans.append({
                    "vid":  int(vid_el.text),
                    "name": name_el.text if name_el is not None else "",
                })
        return vlans

    def create_vlan(self, vid: int, name: str = ""):
        vlan_name = name or f"VLAN{vid}"
        config_xml = f"""
        <config>
          <top xmlns="{H3C_NS_CFG}">
            <VLAN>
              <VLANs>
                <VLANID>
                  <ID>{vid}</ID>
                  <Name>{vlan_name}</Name>
                </VLANID>
              </VLANs>
            </VLAN>
          </top>
        </config>"""
        try:
            with self._connect() as m:
                m.edit_config(target="running", config=config_xml)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"NETCONF error: {e}")

    def delete_vlan(self, vid: int):
        config_xml = f"""
        <config>
          <top xmlns="{H3C_NS_CFG}">
            <VLAN>
              <VLANs>
                <VLANID nc:operation="delete" xmlns:nc="urn:ietf:params:xml:ns:netconf:base:1.0">
                  <ID>{vid}</ID>
                </VLANID>
              </VLANs>
            </VLAN>
          </top>
        </config>"""
        try:
            with self._connect() as m:
                m.edit_config(target="running", config=config_xml)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"NETCONF error: {e}")

    def _cli(self, commands: list[str]):
        """Send CLI config commands via paramiko SSH (handles H3C password-change prompt)."""
        import paramiko, time

        def _recv(shell, timeout=3) -> str:
            time.sleep(timeout)
            out = b""
            while shell.recv_ready():
                out += shell.recv(4096)
            return out.decode("utf-8", errors="ignore")

        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(self.ip, username=self.username, password=self.password, timeout=15)
            shell = client.invoke_shell()
            output = _recv(shell, 2)
            # Handle "Do you want to change the password?" prompt
            if "change" in output.lower() or "[y/n]" in output.lower():
                shell.send("n\n")
                _recv(shell, 1)
            # Enter system-view
            shell.send("system-view\n")
            _recv(shell, 1)
            for cmd in commands:
                shell.send(cmd + "\n")
                _recv(shell, 0.5)
            shell.send("return\n")
            _recv(shell, 0.5)
            shell.send("save force\n")
            _recv(shell, 3)
            client.close()
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"SSH error: {e}")

    def set_trunk_vlans(self, interface: str, vlans: list[int]):
        vids = " ".join(str(v) for v in vlans)
        self._cli([
            f"interface {interface}",
            "port link-type trunk",
            f"port trunk permit vlan {vids}",
        ])

    def set_access_vlan(self, interface: str, vlan: int, description: str = ""):
        cmds = [
            f"interface {interface}",
            "port link-type access",
            f"port access vlan {vlan}",
        ]
        if description:
            cmds.append(f"description {description}")
        self._cli(cmds)


def get_platform(device) -> str:
    """Возвращает slug платформы устройства."""
    return device.platform.slug if device.platform else "eos"


def get_h3c(device) -> H3CNetconf:
    if not device.primary_ip4:
        raise HTTPException(status_code=400, detail=f"У устройства {device.name} не задан primary_ip4")
    ip = str(device.primary_ip4).split("/")[0]
    return H3CNetconf(ip, SWITCH_USER, SWITCH_PASSWORD)


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


@app.get("/debug/{msg}", response_class=PlainTextResponse)
def debug_log(msg: str, request: Request):
    import logging
    logging.warning("ZTP DEBUG [%s]: %s", request.client.host, msg)
    return "ok"


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
    nb = get_nb()
    device = get_device_by_name(nb, name)
    site_id = device.site.id if device.site else None
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


@app.post("/devices/{name}/vlans")
def add_vlan(name: str, body: VlanIn):
    nb = get_nb()
    device = get_device_by_name(nb, name)
    site_id = device.site.id if device.site else None
    platform = get_platform(device)

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
    if platform == "comware":
        get_h3c(device).create_vlan(body.vid, body.name)
    else:
        cmds = ["enable", "configure", f"vlan {body.vid}"]
        if body.name:
            cmds.append(f"name {body.name}")
        get_eapi(device).run(cmds)

    return {"status": "ok", "vid": body.vid, "name": body.name, "netbox": nb_status}


@app.delete("/devices/{name}/vlans/{vid}")
def delete_vlan(name: str, vid: int):
    nb = get_nb()
    device = get_device_by_name(nb, name)
    site_id = device.site.id if device.site else None
    platform = get_platform(device)

    # 1. Коммутатор
    if platform == "comware":
        get_h3c(device).delete_vlan(vid)
    else:
        get_eapi(device).run(["enable", "configure", f"no vlan {vid}"])

    # 2. NetBox
    nb_status = "not_found"
    for vlan in nb.ipam.vlans.filter(vid=vid, site_id=site_id):
        vlan.delete()
        nb_status = "deleted"
        break

    return {"status": "ok", "vid": vid, "netbox": nb_status}


@app.post("/devices/{name}/trunk")
def add_vlan_to_trunk(name: str, body: TrunkIn):
    nb = get_nb()
    device = get_device_by_name(nb, name)
    site_id = device.site.id if device.site else None
    platform = get_platform(device)

    # 1. NetBox — обновляем интерфейс (создаём если нет)
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
                detail=f"VLAN {vid} не найден в NetBox — сначала создайте через POST /devices/{name}/vlans")
        current_vids.add(vlans[0].id)
        vlan_ids_int.append(int(vid))

    nb_iface.update({"mode": "tagged", "tagged_vlans": list(current_vids)})

    # 2. Коммутатор
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


@app.post("/devices/{name}/access")
def set_access_port(name: str, body: AccessIn):
    nb = get_nb()
    device = get_device_by_name(nb, name)
    site_id = device.site.id if device.site else None
    platform = get_platform(device)

    vlans = list(nb.ipam.vlans.filter(vid=body.vlan, site_id=site_id))
    if not vlans:
        raise HTTPException(status_code=400,
            detail=f"VLAN {body.vlan} не найден в NetBox — сначала создайте через POST /devices/{name}/vlans")
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

    # 2. Коммутатор
    if platform == "comware":
        get_h3c(device).set_access_vlan(body.interface, body.vlan, body.description)
    else:
        cmds = ["enable", "configure", f"interface {body.interface}",
                "switchport mode access", f"switchport access vlan {body.vlan}"]
        if body.description:
            cmds.append(f"description {body.description}")
        get_eapi(device).run(cmds)

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

    # Attach IP addresses to interfaces (all roles)
    ip_by_iface: dict[int, str] = {}
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

    # If mgmt interface is a VLAN interface (e.g. Vlan3900), include that VLAN too
    if mgmt_iface:
        m = re.match(r'^[Vv][Ll][Aa][Nn]\s*(\d+)$', mgmt_iface.strip())
        if m:
            needed_vids.add(int(m.group(1)))

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
