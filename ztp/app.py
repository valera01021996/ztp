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
import ssl
import json
import ipaddress
import urllib.request
import urllib.error
from base64 import b64encode

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

ZTP_SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "ztp_script.py")


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
    """Арista скачивает этот скрипт и выполняет его при ZTP."""
    with open(ZTP_SCRIPT_PATH) as f:
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

def build_eos_startup_config(nb, device) -> str:
    lines = []
    lines.append("! Generated by ZTP Server from NetBox")
    lines.append(f"! Device: {device.name}  Serial: {device.serial}")
    lines.append("!")

    lines.append(f"hostname {device.name}")
    lines.append("!")

    lines.append(f"username admin privilege 15 role network-admin secret 0 {ADMIN_PASSWORD}")
    lines.append("!")
    lines.append("aaa authentication login default local")
    lines.append("!")
    lines.append("management ssh")
    lines.append("   no shutdown")
    lines.append("!")
    lines.append("management api http-commands")
    lines.append("   protocol http")
    lines.append("   no shutdown")
    lines.append("!")

    if device.primary_ip4:
        primary = str(device.primary_ip4)
        net = ipaddress.ip_interface(primary).network
        gateway = str(next(net.hosts()))

        ip_obj = nb.ipam.ip_addresses.get(device.primary_ip4.id)
        mgmt_iface = ip_obj.assigned_object.name if (ip_obj and ip_obj.assigned_object) else "Management0"

        lines.append(f"interface {mgmt_iface}")
        lines.append(f"   ip address {primary}")
        lines.append("   no shutdown")
        lines.append("!")
        lines.append(f"ip route 0.0.0.0/0 {gateway}")
        lines.append("!")

    interfaces = list(nb.dcim.interfaces.filter(device_id=device.id))

    needed_vids = set()
    for iface in interfaces:
        if not iface.mode:
            continue
        if iface.untagged_vlan:
            needed_vids.add(iface.untagged_vlan.vid)
        if iface.tagged_vlans:
            for v in iface.tagged_vlans:
                needed_vids.add(v.vid)

    if needed_vids:
        lines.append("! === VLANs ===")
        seen_vids = {}
        for vid in sorted(needed_vids):
            for vlan in nb.ipam.vlans.filter(vid=vid):
                if vlan.vid not in seen_vids:
                    seen_vids[vlan.vid] = vlan.name
                    break
        for vid in sorted(seen_vids):
            lines.append(f"vlan {vid}")
            lines.append(f"   name {seen_vids[vid]}")
        lines.append("!")

    lines.append("! === Interfaces ===")
    for iface in sorted(interfaces, key=lambda i: i.name):
        if not iface.mode:
            continue

        lines.append(f"interface {iface.name}")

        if iface.description:
            lines.append(f"   description {iface.description}")

        mode = iface.mode.value if hasattr(iface.mode, "value") else str(iface.mode)

        if mode == "access":
            lines.append("   switchport mode access")
            if iface.untagged_vlan:
                lines.append(f"   switchport access vlan {iface.untagged_vlan.vid}")
        elif mode == "tagged":
            lines.append("   switchport mode trunk")
            if iface.tagged_vlans:
                vids = ",".join(str(v) for v in sorted(set(v.vid for v in iface.tagged_vlans)))
                lines.append(f"   switchport trunk allowed vlan {vids}")
        elif mode == "tagged-all":
            lines.append("   switchport mode trunk")

        if iface.mtu:
            lines.append(f"   mtu {iface.mtu}")

        lines.append("   no shutdown" if iface.enabled else "   shutdown")
        lines.append("!")

    lines.append("end")
    return "\n".join(lines)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_or_create_tag(nb, slug: str, name: str, color: str = "00bcd4"):
    tag = nb.extras.tags.get(slug=slug)
    if not tag:
        tag = nb.extras.tags.create({"name": name, "slug": slug, "color": color})
    return tag
