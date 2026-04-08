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
import sqlite3
import difflib
import logging
import ipaddress
import urllib.request
import urllib.error
from base64 import b64encode
from datetime import datetime

from jinja2 import Environment, FileSystemLoader

import pynetbox
from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.responses import PlainTextResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="ZTP Server")

UI_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates", "ui")
ui_templates = Jinja2Templates(directory=UI_TEMPLATES_DIR)

DB_PATH = os.path.join(os.path.dirname(__file__), "pipeline.db")

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _init_db():
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pipeline_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_name TEXT NOT NULL,
                device_serial TEXT,
                platform TEXT,
                status TEXT DEFAULT 'pending',
                generated_config TEXT,
                current_config TEXT,
                diff TEXT,
                error TEXT,
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)
        conn.commit()

_init_db()

NETBOX_URL      = os.environ["NETBOX_URL"]
NETBOX_TOKEN    = os.environ["NETBOX_TOKEN"]
ADMIN_PASSWORD  = os.environ.get("ADMIN_PASSWORD", "123456")
SWITCH_USER     = os.environ.get("SWITCH_USER", "admin")
SWITCH_PASSWORD = os.environ.get("SWITCH_PASSWORD", ADMIN_PASSWORD)

GITLAB_TEMPLATES_URL = os.environ.get("GITLAB_TEMPLATES_URL", "")

ZTP_SCRIPT_PATH     = os.path.join(os.path.dirname(__file__), "ztp_script.py")
H3C_ZTP_SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "h3c_ztp_script.py")
TEMPLATES_DIR       = os.path.join(os.path.dirname(__file__), "templates")
TEMPLATES_REPO_DIR  = os.path.join(os.path.dirname(__file__), "templates_repo")


def _sync_templates() -> str:
    """Clone or pull templates from GitLab. Returns status message."""
    if not GITLAB_TEMPLATES_URL:
        return "GITLAB_TEMPLATES_URL not set, using local templates"
    try:
        from git import Repo, InvalidGitRepositoryError
        if os.path.exists(os.path.join(TEMPLATES_REPO_DIR, ".git")):
            repo = Repo(TEMPLATES_REPO_DIR)
            repo.remotes.origin.pull()
            msg = "Templates pulled from GitLab"
        else:
            os.makedirs(TEMPLATES_REPO_DIR, exist_ok=True)
            Repo.clone_from(GITLAB_TEMPLATES_URL, TEMPLATES_REPO_DIR)
            msg = "Templates cloned from GitLab"
        # Reload jinja env from new templates
        global _jinja_env
        _jinja_env = _make_jinja_env(TEMPLATES_REPO_DIR)
        logging.info(msg)
        return msg
    except Exception as e:
        logging.warning("Template sync failed: %s — using local templates", e)
        return f"sync failed: {e}"

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

def _make_jinja_env(templates_dir: str) -> Environment:
    env = Environment(
        loader=FileSystemLoader(templates_dir),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["cidr_to_mask"]  = _cidr_to_mask
    env.filters["comware_iface"] = _comware_iface_name
    env.tests["match"]      = lambda value, pattern: bool(re.match(pattern, str(value)))
    env.tests["vlan_iface"] = lambda value: bool(
        re.match(r'^[Vv][Ll][Aa][Nn]\s*\d+$', str(value).strip())
    )
    return env

def _comware_iface_name(name: str) -> str:
    """Vlan10 / VLAN10 / Vlan 10  →  Vlan-interface10 (H3C Comware naming)."""
    m = re.match(r'^[Vv][Ll][Aa][Nn]\s*(\d+)$', name.strip())
    if m:
        return "Vlan-interface" + m.group(1)
    return name

_jinja_env = _make_jinja_env(TEMPLATES_DIR)

# Sync templates from GitLab on startup (if configured)
_sync_templates()


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
    return build_eos_startup_config(nb, device, day0_only=True)


@app.post("/webhooks/netbox")
async def netbox_webhook(request: Request):
    """NetBox webhook — создаёт Pipeline Run при изменении устройства/интерфейса/VLAN."""
    body = await request.json()
    event = body.get("event", "")
    model = body.get("model", "")
    data  = body.get("data", {})
    logging.info("NetBox webhook payload: event=%s model=%s data_keys=%s device_field=%s",
                 event, model, list(data.keys()), data.get("device"))

    # Определяем имя устройства из payload
    device_name = None
    if model == "device":
        device_name = data.get("name")
    elif model in ("interface", "ip-address"):
        device_name = (data.get("device") or {}).get("name")
    elif model == "vlan":
        # VLAN изменился — пересчитываем все устройства на этом сайте
        logging.info("NetBox webhook: VLAN change, skipping auto-run")
        return {"status": "skipped", "reason": "vlan change — trigger manually"}

    if not device_name:
        return {"status": "skipped", "reason": "cannot determine device"}

    # Генерируем конфиг и создаём Pipeline Run
    nb = get_nb()
    device = get_device_by_name(nb, device_name)
    platform = get_platform(device)
    generated = None
    error = None
    try:
        generated = build_eos_startup_config(nb, device)
    except Exception as e:
        error = str(e)

    current = _get_current_config(device)
    diff = _make_diff(current, generated) if generated else ""

    with _db() as conn:
        cur = conn.execute(
            """INSERT INTO pipeline_runs
               (device_name, device_serial, platform, status, generated_config, current_config, diff, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (device_name, device.serial or "", platform,
             "pending" if not error else "failed",
             generated, current, diff, error)
        )
        run_id = cur.lastrowid
        conn.commit()

    logging.info("NetBox webhook: event=%s model=%s device=%s run_id=%s", event, model, device_name, run_id)
    return {"status": "ok", "device": device_name, "run_id": run_id}


@app.post("/webhooks/gitlab")
async def gitlab_webhook(request: Request):
    """GitLab webhook — pull latest templates on push."""
    body = await request.json()
    ref = body.get("ref", "")
    project = body.get("project", {}).get("path_with_namespace", "")
    msg = _sync_templates()
    logging.info("GitLab webhook: ref=%s project=%s sync=%s", ref, project, msg)
    return {"status": "ok", "sync": msg}


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


def build_eos_startup_config(nb, device, day0_only: bool = False) -> str:
    platform_slug = device.platform.slug if device.platform else "eos"

    if day0_only:
        # ZTP Day0 — только base шаблон: hostname, mgmt IP, user, SSH/Telnet
        base_templates = {"eos": "eos/base.j2", "comware": "comware/base.j2"}
        template_name = base_templates.get(platform_slug, "eos/base.j2")
    else:
        role_slug     = device.role.slug if device.role else ""
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


# ─── UI ───────────────────────────────────────────────────────────────────────

def _device_creds(device) -> dict:
    if not device.primary_ip4:
        raise HTTPException(status_code=400, detail=f"У устройства {device.name} не задан primary_ip4")
    return {
        "ip": str(device.primary_ip4).split("/")[0],
        "username": SWITCH_USER,
        "password": SWITCH_PASSWORD,
    }


def _get_current_config(device) -> str:
    """Получить текущий running-config с устройства."""
    platform = get_platform(device)
    try:
        if platform == "eos":
            result = get_eapi(device).run(["enable", "show running-config"])
            return result[-1].get("output", "")
        elif platform == "comware":
            import paramiko, time
            creds = _device_creds(device)
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(creds["ip"], username=creds["username"],
                           password=creds["password"], timeout=10)
            shell = client.invoke_shell()
            time.sleep(1)
            out = shell.recv(4096).decode("utf-8", errors="ignore")
            if "change" in out.lower():
                shell.send("n\n"); time.sleep(0.5); shell.recv(1024)
            shell.send("screen-length disable\n"); time.sleep(0.5); shell.recv(1024)
            shell.send("display current-configuration\n"); time.sleep(3)
            output = ""
            while shell.recv_ready():
                output += shell.recv(65536).decode("utf-8", errors="ignore")
                time.sleep(0.3)
            client.close()
            return output
    except Exception as e:
        return f"# Could not connect to device: {e}"
    return ""


def _make_diff(current: str, generated: str) -> str:
    a = (current or "").splitlines(keepends=True)
    b = (generated or "").splitlines(keepends=True)
    return "".join(difflib.unified_diff(a, b, fromfile="current", tofile="generated", lineterm="\n"))


@app.get("/ui", response_class=HTMLResponse)
def ui_dashboard(request: Request):
    with _db() as conn:
        runs = conn.execute(
            "SELECT * FROM pipeline_runs ORDER BY id DESC LIMIT 100"
        ).fetchall()
    return ui_templates.TemplateResponse("dashboard.html", {
        "request": request, "runs": runs, "active": "dashboard"
    })


NETWORK_ROLE_SLUGS = {"leaf", "spine", "data-sw", "oam", "access", "distribution", "core", "router"}

@app.get("/ui/devices", response_class=HTMLResponse)
def ui_devices(request: Request):
    nb = get_nb()
    all_roles = list(nb.dcim.device_roles.all())
    # По умолчанию показываем только сетевые роли
    selected_roles = request.query_params.getlist("role")
    if not selected_roles:
        selected_roles = [r.slug for r in all_roles if r.slug in NETWORK_ROLE_SLUGS]

    if selected_roles:
        devices = list(nb.dcim.devices.filter(role=selected_roles, status="active"))
    else:
        devices = list(nb.dcim.devices.filter(status="active"))

    return ui_templates.TemplateResponse("devices.html", {
        "request": request,
        "devices": devices,
        "roles": all_roles,
        "selected_roles": selected_roles,
        "active": "devices",
    })


@app.get("/ui/devices/{device_name}", response_class=HTMLResponse)
def ui_device_manage(request: Request, device_name: str,
                     error: str = None, success: str = None):
    nb = get_nb()
    device = get_device_by_name(nb, device_name)
    site_id = device.site.id if device.site else None

    vlans = []
    try:
        vlans = sorted(
            list(nb.ipam.vlans.filter(site_id=site_id)),
            key=lambda v: v.vid
        )
    except Exception:
        pass

    return ui_templates.TemplateResponse("device_manage.html", {
        "request": request,
        "device_name": device_name,
        "platform": get_platform(device),
        "role": (getattr(device, 'role', None) or getattr(device, 'device_role', None) or type('', (), {'name': '—'})()).name,
        "primary_ip": str(device.primary_ip4).split("/")[0] if device.primary_ip4 else "—",
        "vlans": vlans,
        "error": error,
        "success": success,
        "active": "devices",
    })


@app.post("/ui/devices/{device_name}/vlans")
def ui_add_vlan(device_name: str, vid: int = Form(...), name: str = Form("")):
    try:
        nb = get_nb()
        device = get_device_by_name(nb, device_name)
        site_id = device.site.id if device.site else None
        platform = get_platform(device)

        existing = nb.ipam.vlans.get(vid=vid, site_id=site_id)
        if not existing:
            vlan_data = {"vid": vid, "name": name or f"VLAN{vid}"}
            if site_id:
                vlan_data["site"] = site_id
            nb.ipam.vlans.create(vlan_data)

        if platform == "comware":
            get_h3c(device).add_vlan(vid, name or f"VLAN{vid}")
        else:
            get_eapi(device).run([
                "enable", "configure",
                f"vlan {vid}",
                f"name {name or f'VLAN{vid}'}",
            ])

        return RedirectResponse(f"/ui/devices/{device_name}?success=VLAN+{vid}+added", status_code=303)
    except Exception as e:
        return RedirectResponse(f"/ui/devices/{device_name}?error={str(e)[:120]}", status_code=303)


@app.post("/ui/devices/{device_name}/vlans/{vid}/delete")
def ui_delete_vlan(device_name: str, vid: int):
    try:
        nb = get_nb()
        device = get_device_by_name(nb, device_name)
        site_id = device.site.id if device.site else None
        platform = get_platform(device)

        vlan_obj = nb.ipam.vlans.get(vid=vid, site_id=site_id)
        if vlan_obj:
            vlan_obj.delete()

        if platform == "comware":
            get_h3c(device)._cli([f"undo vlan {vid}"])
        else:
            get_eapi(device).run(["enable", "configure", f"no vlan {vid}"])

        return RedirectResponse(f"/ui/devices/{device_name}?success=VLAN+{vid}+deleted", status_code=303)
    except Exception as e:
        return RedirectResponse(f"/ui/devices/{device_name}?error={str(e)[:120]}", status_code=303)


@app.post("/ui/devices/{device_name}/trunk")
def ui_trunk(device_name: str, interface: str = Form(...), vlans: str = Form(...)):
    try:
        vlan_list = [int(v.strip()) for v in vlans.split(",") if v.strip()]
        nb = get_nb()
        device = get_device_by_name(nb, device_name)
        site_id = device.site.id if device.site else None
        platform = get_platform(device)

        nb_iface = nb.dcim.interfaces.get(device_id=device.id, name=interface)
        if not nb_iface:
            nb_iface = nb.dcim.interfaces.create(device=device.id, name=interface, type="1000base-t")

        current_vids = {v.id for v in (nb_iface.tagged_vlans or [])}
        for vid in vlan_list:
            vlan_obj = nb.ipam.vlans.get(vid=vid, site_id=site_id)
            if vlan_obj:
                current_vids.add(vlan_obj.id)
        nb_iface.update({"mode": "tagged", "tagged_vlans": list(current_vids)})

        if platform == "comware":
            get_h3c(device).set_trunk_vlans(interface, vlan_list)
        else:
            vids_str = ",".join(str(v) for v in vlan_list)
            get_eapi(device).run([
                "enable", "configure",
                f"interface {interface}",
                "switchport mode trunk",
                f"switchport trunk allowed vlan add {vids_str}",
            ])

        return RedirectResponse(f"/ui/devices/{device_name}?success=Trunk+configured+on+{interface}", status_code=303)
    except Exception as e:
        return RedirectResponse(f"/ui/devices/{device_name}?error={str(e)[:120]}", status_code=303)


@app.post("/ui/devices/{device_name}/access")
def ui_access(device_name: str, interface: str = Form(...),
              vlan: int = Form(...), description: str = Form("")):
    try:
        nb = get_nb()
        device = get_device_by_name(nb, device_name)
        site_id = device.site.id if device.site else None
        platform = get_platform(device)

        vlan_obj = nb.ipam.vlans.get(vid=vlan, site_id=site_id)
        nb_iface = nb.dcim.interfaces.get(device_id=device.id, name=interface)
        if not nb_iface:
            nb_iface = nb.dcim.interfaces.create(device=device.id, name=interface, type="1000base-t")

        update = {"mode": "access"}
        if vlan_obj:
            update["untagged_vlan"] = vlan_obj.id
        if description:
            update["description"] = description
        nb_iface.update(update)

        if platform == "comware":
            get_h3c(device).set_access_vlan(interface, vlan, description)
        else:
            cmds = ["enable", "configure", f"interface {interface}",
                    "switchport mode access", f"switchport access vlan {vlan}"]
            if description:
                cmds.append(f"description {description}")
            get_eapi(device).run(cmds)

        return RedirectResponse(f"/ui/devices/{device_name}?success=Access+port+{interface}+set+to+VLAN+{vlan}", status_code=303)
    except Exception as e:
        return RedirectResponse(f"/ui/devices/{device_name}?error={str(e)[:120]}", status_code=303)


@app.post("/ui/generate/{device_name}")
def ui_generate(device_name: str):
    nb = get_nb()
    device = get_device_by_name(nb, device_name)
    platform = get_platform(device)

    generated = ""
    error = None
    try:
        generated = build_eos_startup_config(nb, device)
    except Exception as e:
        error = str(e)

    current = _get_current_config(device)
    diff = _make_diff(current, generated) if generated else ""

    with _db() as conn:
        cur = conn.execute(
            """INSERT INTO pipeline_runs
               (device_name, device_serial, platform, status, generated_config, current_config, diff, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (device_name, device.serial or "", platform,
             "pending" if not error else "failed",
             generated, current, diff, error)
        )
        run_id = cur.lastrowid
        conn.commit()

    return RedirectResponse(f"/ui/runs/{run_id}", status_code=303)


@app.get("/ui/runs/{run_id}", response_class=HTMLResponse)
def ui_run_detail(request: Request, run_id: int):
    with _db() as conn:
        run = conn.execute(
            "SELECT * FROM pipeline_runs WHERE id = ?", (run_id,)
        ).fetchone()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return ui_templates.TemplateResponse("run_detail.html", {
        "request": request, "run": run, "active": "dashboard"
    })


@app.post("/ui/runs/{run_id}/approve")
def ui_approve(run_id: int):
    with _db() as conn:
        run = conn.execute("SELECT * FROM pipeline_runs WHERE id = ?", (run_id,)).fetchone()
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")

    nb = get_nb()
    device = get_device_by_name(nb, run["device_name"])
    platform = get_platform(device)
    config = run["generated_config"]
    error = None

    try:
        if platform == "eos":
            get_eapi(device).run([
                "enable", "configure",
                "copy terminal: startup-config",
            ])
            # Применить через replace конфига
            cmds = ["enable"]
            for line in config.splitlines():
                if line.strip() and not line.startswith("!"):
                    cmds.append(line)
            get_eapi(device).run(cmds)
        elif platform == "comware":
            import paramiko, time
            creds = _device_creds(device)
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(creds["ip"], username=creds["username"],
                           password=creds["password"], timeout=15)
            shell = client.invoke_shell()
            time.sleep(1)
            out = shell.recv(4096).decode("utf-8", errors="ignore")
            if "change" in out.lower():
                shell.send("n\n"); time.sleep(0.5); shell.recv(1024)
            shell.send("system-view\n"); time.sleep(0.5)
            for line in config.splitlines():
                if line.strip() and not line.startswith("!"):
                    shell.send(line + "\n"); time.sleep(0.1)
            shell.send("return\n"); time.sleep(0.5)
            shell.send("save force\n"); time.sleep(2)
            client.close()
        status = "deployed"
    except Exception as e:
        error = str(e)
        status = "failed"

    with _db() as conn:
        conn.execute(
            "UPDATE pipeline_runs SET status=?, error=?, updated_at=datetime('now','localtime') WHERE id=?",
            (status, error, run_id)
        )
        conn.commit()

    return RedirectResponse(f"/ui/runs/{run_id}", status_code=303)


@app.post("/ui/runs/{run_id}/reject")
def ui_reject(run_id: int):
    with _db() as conn:
        conn.execute(
            "UPDATE pipeline_runs SET status='rejected', updated_at=datetime('now','localtime') WHERE id=?",
            (run_id,)
        )
        conn.commit()
    return RedirectResponse(f"/ui/runs/{run_id}", status_code=303)
