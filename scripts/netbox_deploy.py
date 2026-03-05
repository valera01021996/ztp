"""
NetBox Custom Script — Deploy Config to Arista (eAPI) and H3C (SSH/Netmiko).

Установка:
  Скопировать в директорию scripts/ NetBox.
  NetBox UI → Customization → Scripts → "Deploy Config"

Зависимости:
  - netmiko  (pip install netmiko)  — для H3C
  - stdlib ssl / urllib              — для Arista eAPI

Поддерживаемые платформы (slug в NetBox):
  Arista : eos, arista-eos, arista
  H3C    : comware, h3c, hp-comware

Workflow:
  1. Excel импорт создаёт устройства с тегом "config-pending"
  2. Инженер монтирует коммутатор, делает bootstrap (mgmt IP + доступ)
  3. Запускаем этот скрипт → выбираем устройство → Deploy
  4. Скрипт определяет платформу → генерирует конфиг → заливает
  5. Тег "config-pending" снимается, статус → active
"""

import json
import ssl
import urllib.request
import urllib.error
from typing import List, Optional

from extras.scripts import Script, MultiObjectVar, StringVar, BooleanVar, IntegerVar
from dcim.models import Device, Interface
from ipam.models import VLAN
from extras.models import Tag

# ─── Константы ───────────────────────────────────────────────────────────────

TAG_PENDING  = "config-pending"
TAG_DEPLOYED = "config-deployed"

SERVER_ROLE_SLUGS = {
    "server", "servers", "vm", "virtual-machine",
    "esxi", "esxi-host", "hypervisor", "bare-metal",
}

ARISTA_PLATFORM_SLUGS = {"eos", "arista-eos", "arista"}
H3C_PLATFORM_SLUGS    = {"comware", "h3c", "hp-comware", "hp_comware"}

SUPPORTED_SLUGS = ARISTA_PLATFORM_SLUGS | H3C_PLATFORM_SLUGS


# ─── Arista eAPI клиент ───────────────────────────────────────────────────────

class EAPIError(Exception):
    pass


class AristaEAPI:
    def __init__(self, host: str, username: str, password: str,
                 port: int = 443, verify_ssl: bool = False):
        scheme = "https" if port == 443 else "http"
        self.url      = f"{scheme}://{host}:{port}/command-api"
        self.username = username
        self.password = password
        self.verify   = verify_ssl

    def run(self, commands: List[str]) -> List:
        import base64
        payload = json.dumps({
            "jsonrpc": "2.0",
            "method":  "runCmds",
            "params":  {"version": 1, "cmds": commands, "format": "json"},
            "id":      "netbox-deploy",
        }).encode("utf-8")

        token = base64.b64encode(
            f"{self.username}:{self.password}".encode()
        ).decode()

        ctx = ssl.create_default_context()
        if not self.verify:
            ctx.check_hostname = False
            ctx.verify_mode    = ssl.CERT_NONE

        req = urllib.request.Request(
            self.url, data=payload,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Basic {token}"},
        )
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise EAPIError(f"Не удалось подключиться: {e}")
        except Exception as e:
            raise EAPIError(f"Ошибка запроса: {e}")

        if "error" in result:
            code = result["error"].get("code", "?")
            msg  = result["error"].get("message", "")
            raise EAPIError(f"eAPI error {code}: {msg}")

        return result.get("result", [])

    def check(self) -> str:
        result = self.run(["show version"])
        return result[0].get("version", "unknown")


# ─── Генерация конфига: Arista EOS ───────────────────────────────────────────

def build_eos_commands(device: Device) -> List[str]:
    cmds = ["enable", "configure terminal"]

    interfaces = list(
        Interface.objects.filter(device=device)
        .select_related("untagged_vlan")
        .prefetch_related("tagged_vlans")
        .order_by("name")
    )

    needed_vids = set()
    for iface in interfaces:
        if iface.untagged_vlan:
            needed_vids.add(iface.untagged_vlan.vid)
        for v in iface.tagged_vlans.all():
            needed_vids.add(v.vid)

    if needed_vids:
        for vlan in VLAN.objects.filter(vid__in=needed_vids).order_by("vid"):
            cmds.append(f"vlan {vlan.vid}")
            cmds.append(f"   name {vlan.name}")

    for iface in interfaces:
        if not iface.mode:
            continue

        cmds.append(f"interface {iface.name}")

        if iface.description:
            cmds.append(f"   description {iface.description}")

        if iface.mode == "access":
            cmds.append("   switchport mode access")
            if iface.untagged_vlan:
                cmds.append(f"   switchport access vlan {iface.untagged_vlan.vid}")

        elif iface.mode == "tagged":
            cmds.append("   switchport mode trunk")
            tagged_vids = ",".join(
                str(v.vid) for v in iface.tagged_vlans.all().order_by("vid")
            )
            if tagged_vids:
                cmds.append(f"   switchport trunk allowed vlan {tagged_vids}")

        elif iface.mode == "tagged-all":
            cmds.append("   switchport mode trunk")

        if iface.mtu:
            cmds.append(f"   mtu {iface.mtu}")

        cmds.append("   no shutdown" if iface.enabled else "   shutdown")

    cmds.extend(["end", "write memory"])
    return cmds


# ─── Генерация конфига: H3C Comware ──────────────────────────────────────────

def build_comware_commands(device: Device) -> List[str]:
    cmds = ["system-view"]

    interfaces = list(
        Interface.objects.filter(device=device)
        .select_related("untagged_vlan")
        .prefetch_related("tagged_vlans")
        .order_by("name")
    )

    needed_vids = set()
    for iface in interfaces:
        if iface.untagged_vlan:
            needed_vids.add(iface.untagged_vlan.vid)
        for v in iface.tagged_vlans.all():
            needed_vids.add(v.vid)

    if needed_vids:
        for vlan in VLAN.objects.filter(vid__in=needed_vids).order_by("vid"):
            cmds.append(f"vlan {vlan.vid}")
            cmds.append(f" name {vlan.name}")
            cmds.append("quit")

    for iface in interfaces:
        if not iface.mode:
            continue

        cmds.append(f"interface {iface.name}")

        if iface.description:
            cmds.append(f" description {iface.description}")

        if iface.mode == "access":
            cmds.append(" port link-type access")
            if iface.untagged_vlan:
                cmds.append(f" port access vlan {iface.untagged_vlan.vid}")

        elif iface.mode == "tagged":
            cmds.append(" port link-type trunk")
            tagged_vids = " ".join(
                str(v.vid) for v in iface.tagged_vlans.all().order_by("vid")
            )
            if tagged_vids:
                cmds.append(f" port trunk permit vlan {tagged_vids}")

        elif iface.mode == "tagged-all":
            cmds.append(" port link-type trunk")
            cmds.append(" port trunk permit vlan all")

        if iface.mtu:
            cmds.append(f" jumboframe enable {iface.mtu}")

        cmds.append(" undo shutdown" if iface.enabled else " shutdown")
        cmds.append("quit")

    cmds.append("return")
    return cmds


# ─── Деплой: H3C через Netmiko ───────────────────────────────────────────────

class NetmikoError(Exception):
    pass


def deploy_h3c(host: str, username: str, password: str,
               commands: List[str], ssh_port: int = 22) -> str:
    """Заливает команды на H3C через SSH. Возвращает версию устройства."""
    try:
        from netmiko import ConnectHandler
        from netmiko.exceptions import (
            NetmikoTimeoutException,
            NetmikoAuthenticationException,
        )
    except ImportError:
        raise NetmikoError(
            "netmiko не установлен. Запустите: pip install netmiko"
        )

    try:
        conn = ConnectHandler(
            device_type="hp_comware",
            host=host,
            username=username,
            password=password,
            port=ssh_port,
            timeout=30,
        )
    except NetmikoAuthenticationException:
        raise NetmikoError("Неверный логин или пароль")
    except NetmikoTimeoutException:
        raise NetmikoError(f"Таймаут подключения к {host}:{ssh_port}")
    except Exception as e:
        raise NetmikoError(f"Ошибка SSH подключения: {e}")

    try:
        version_out = conn.send_command("display version")
        # Извлекаем строку с версией
        version = "unknown"
        for line in version_out.splitlines():
            if "Comware Software" in line or "Version" in line:
                version = line.strip()
                break

        conn.send_config_set(commands)
        conn.save_config()   # save force
    except Exception as e:
        raise NetmikoError(f"Ошибка выполнения команд: {e}")
    finally:
        conn.disconnect()

    return version


# ─── Helpers ──────────────────────────────────────────────────────────────────

def get_or_create_tag(slug: str, name: str, color: str = "00bcd4") -> Tag:
    tag, _ = Tag.objects.get_or_create(
        slug=slug,
        defaults={"name": name, "color": color},
    )
    return tag


def get_device_ip(device: Device) -> Optional[str]:
    if not device.primary_ip4:
        return None
    return str(device.primary_ip4.address).split("/")[0]


def get_platform_slug(device: Device) -> Optional[str]:
    if device.platform:
        return device.platform.slug.lower()
    return None



# ─── Custom Script ────────────────────────────────────────────────────────────

class DeployConfig(Script):

    class Meta:
        name        = "Deploy Config"
        description = (
            "Генерирует конфиг из NetBox и заливает на коммутатор. "
            "Arista → eAPI, H3C → SSH (Netmiko). "
            "Работает только с устройствами у которых есть тег 'config-pending'."
        )
        field_order = [
            "devices", "username", "password",
            "eapi_port", "ssh_port", "verify_ssl", "dry_run",
        ]

    devices = MultiObjectVar(
        model=Device,
        label="Устройства",
        description="Коммутаторы для деплоя (только с тегом config-pending)",
        query_params={"tag": TAG_PENDING},
        required=True,
    )

    username = StringVar(
        label="Username",
        description="Логин (для eAPI и SSH)",
        default="admin",
    )

    password = StringVar(
        label="Password",
        description="Пароль (для eAPI и SSH)",
    )

    eapi_port = IntegerVar(
        label="Arista eAPI Port",
        description="443 = HTTPS, 80 = HTTP",
        default=443,
        min_value=1,
        max_value=65535,
    )

    ssh_port = IntegerVar(
        label="H3C SSH Port",
        description="Обычно 22",
        default=22,
        min_value=1,
        max_value=65535,
    )

    verify_ssl = BooleanVar(
        label="Проверять SSL (Arista)",
        description="Отключите если используется self-signed сертификат",
        default=False,
    )

    dry_run = BooleanVar(
        label="Dry Run",
        description="Показать конфиг без заливки на устройство",
        default=True,
    )

    def run(self, data, commit):
        dry      = data["dry_run"]
        username = data["username"]
        password = data["password"]
        devices  = data["devices"]
        eapi_port = data["eapi_port"]
        ssh_port  = data["ssh_port"]
        verify    = data["verify_ssl"]

        if dry:
            self.log_warning("DRY RUN — конфиг будет показан, но НЕ залит на устройство")

        tag_pending  = get_or_create_tag(TAG_PENDING,  "config-pending",  "ff9800")
        tag_deployed = get_or_create_tag(TAG_DEPLOYED, "config-deployed", "4caf50")

        success_count = 0
        fail_count    = 0

        for device in devices:
            self.log_info(f"{'─' * 60}")
            self.log_info(f"Устройство: {device.name}")

            # ── Пропускаем серверы ───────────────────────────────────────
            if device.role and device.role.slug in SERVER_ROLE_SLUGS:
                self.log_failure(
                    f"  Пропускаем {device.name} — роль '{device.role}' относится к серверам"
                )
                fail_count += 1
                continue

            # ── Определяем платформу ─────────────────────────────────────
            platform_slug = get_platform_slug(device)
            if platform_slug not in SUPPORTED_SLUGS:
                self.log_failure(
                    f"  Пропускаем {device.name} — платформа '{platform_slug}' не поддерживается. "
                    f"Поддерживаются: {', '.join(sorted(SUPPORTED_SLUGS))}"
                )
                fail_count += 1
                continue

            is_arista = platform_slug in ARISTA_PLATFORM_SLUGS
            is_h3c    = platform_slug in H3C_PLATFORM_SLUGS
            vendor    = "Arista" if is_arista else "H3C"
            self.log_info(f"  Платформа: {platform_slug} ({vendor})")

            # ── Получаем IP ──────────────────────────────────────────────
            mgmt_ip = get_device_ip(device)
            if not mgmt_ip:
                self.log_failure(f"  У устройства {device.name} нет primary IP — пропускаем")
                fail_count += 1
                continue
            self.log_info(f"  Management IP: {mgmt_ip}")

            # ── Генерируем конфиг ────────────────────────────────────────
            if is_arista:
                commands = [c for c in build_eos_commands(device) if not c.startswith("!")]
            else:
                commands = build_comware_commands(device)

            self.log_info("  Сгенерированный конфиг:")
            for line in commands:
                self.log_info(f"    {line}")

            if dry:
                self.log_success(f"  [DRY] Конфиг для {device.name} готов, деплой пропущен")
                continue

            # ── Деплой ──────────────────────────────────────────────────
            if is_arista:
                eapi = AristaEAPI(mgmt_ip, username, password, eapi_port, verify)
                try:
                    eos_version = eapi.check()
                    self.log_success(f"  Подключение успешно — EOS {eos_version}")
                except EAPIError as e:
                    self.log_failure(f"  Не удалось подключиться к {mgmt_ip}: {e}")
                    fail_count += 1
                    continue
                try:
                    eapi.run(commands)
                    self.log_success(f"  Конфиг залит на {device.name}")
                except EAPIError as e:
                    self.log_failure(f"  Ошибка деплоя: {e}")
                    fail_count += 1
                    continue

            else:  # H3C
                try:
                    version = deploy_h3c(mgmt_ip, username, password, commands, ssh_port)
                    self.log_success(f"  Подключение и деплой успешны — {version}")
                except NetmikoError as e:
                    self.log_failure(f"  Ошибка деплоя на {device.name}: {e}")
                    fail_count += 1
                    continue

            # ── Обновляем NetBox ─────────────────────────────────────────
            device.tags.remove(tag_pending)
            device.tags.add(tag_deployed)
            device.status = "active"
            device.save()
            self.log_success(
                f"  NetBox обновлён: тег '{TAG_PENDING}' снят, "
                f"добавлен '{TAG_DEPLOYED}', статус → active"
            )
            success_count += 1

        # ── Итог ─────────────────────────────────────────────────────────
        self.log_info(f"{'─' * 60}")
        if dry:
            self.log_success(f"DRY RUN завершён. Конфиг показан для {len(devices)} устройств.")
        else:
            self.log_success(f"Успешно задеплоено: {success_count}")
            if fail_count:
                self.log_failure(f"Ошибок: {fail_count}")
