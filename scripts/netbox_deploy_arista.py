"""
NetBox Custom Script — Deploy Config to Arista switches via eAPI.

Установка:
  Скопировать в директорию scripts/ NetBox.
  NetBox UI → Customization → Scripts → "Deploy Arista Config"

Зависимости (уже есть в NetBox окружении): requests

Workflow:
  1. Excel импорт создаёт устройства с тегом "config-pending"
  2. Инженер монтирует коммутатор, делает bootstrap (mgmt IP + доступ)
  3. Запускаем этот скрипт → выбираем устройство → Deploy
  4. Скрипт генерирует конфиг из NetBox, заливает через eAPI
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

# Роли устройств которые считаются серверами — их НЕ показываем
SERVER_ROLE_SLUGS = {
    "server", "servers", "vm", "virtual-machine",
    "esxi", "esxi-host", "hypervisor", "bare-metal",
}

# Платформы Arista
ARISTA_PLATFORMS = {"eos", "arista-eos", "arista"}


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
        """Отправляет список команд, возвращает список результатов."""
        payload = json.dumps({
            "jsonrpc": "2.0",
            "method":  "runCmds",
            "params": {
                "version": 1,
                "cmds":    commands,
                "format":  "json",
            },
            "id": "netbox-deploy",
        }).encode("utf-8")

        # Basic auth header
        import base64
        token = base64.b64encode(
            f"{self.username}:{self.password}".encode()
        ).decode()

        # SSL контекст — отключаем проверку если нужно
        ctx = ssl.create_default_context()
        if not self.verify:
            ctx.check_hostname = False
            ctx.verify_mode    = ssl.CERT_NONE

        req = urllib.request.Request(
            self.url,
            data=payload,
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Basic {token}",
            },
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
        """Проверяет связь. Возвращает версию EOS."""
        result = self.run(["show version"])
        return result[0].get("version", "unknown")


# ─── Генерация конфига из NetBox ──────────────────────────────────────────────

def build_eos_commands(device: Device) -> List[str]:
    """
    Строит список команд EOS из данных NetBox:
      - VLANs (все что используются на интерфейсах)
      - Interfaces (access / trunk / description)
    """
    cmds = ["enable", "configure terminal"]

    interfaces = list(
        Interface.objects
        .filter(device=device)
        .select_related("untagged_vlan")
        .prefetch_related("tagged_vlans")
        .order_by("name")
    )

    # ── Собираем все VID которые нужны на этом устройстве ──────────────────
    needed_vids = set()
    for iface in interfaces:
        if iface.untagged_vlan:
            needed_vids.add(iface.untagged_vlan.vid)
        for v in iface.tagged_vlans.all():
            needed_vids.add(v.vid)

    # ── VLAN база ──────────────────────────────────────────────────────────
    if needed_vids:
        cmds.append("!")
        cmds.append("! === VLANs ===")
        for vlan in VLAN.objects.filter(vid__in=needed_vids).order_by("vid"):
            cmds.append(f"vlan {vlan.vid}")
            cmds.append(f"   name {vlan.name}")

    # ── Интерфейсы ────────────────────────────────────────────────────────
    cmds.append("!")
    cmds.append("! === Interfaces ===")

    for iface in interfaces:
        # Пропускаем интерфейсы без mode (routed / management оставляем как есть)
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

    cmds.append("!")
    cmds.append("end")
    cmds.append("write memory")

    return cmds


# ─── Helpers ──────────────────────────────────────────────────────────────────

def get_or_create_tag(slug: str, name: str, color: str = "00bcd4") -> Tag:
    tag, _ = Tag.objects.get_or_create(
        slug=slug,
        defaults={"name": name, "color": color},
    )
    return tag


def get_device_ip(device: Device) -> Optional[str]:
    """Возвращает строку IP-адреса из primary_ip4."""
    if not device.primary_ip4:
        return None
    return str(device.primary_ip4.address).split("/")[0]


# ─── Custom Script ────────────────────────────────────────────────────────────

class DeployAristaConfig(Script):

    class Meta:
        name        = "Deploy Arista Config"
        description = (
            "Генерирует конфиг из NetBox и заливает на Arista коммутатор через eAPI. "
            "Работает только с устройствами у которых есть тег 'config-pending'."
        )
        field_order = [
            "devices", "username", "password",
            "eapi_port", "verify_ssl", "dry_run",
        ]

    devices = MultiObjectVar(
        model=Device,
        label="Устройства",
        description="Выберите Arista коммутаторы для деплоя (только с тегом config-pending)",
        query_params={
            "tag": TAG_PENDING,
        },
        required=True,
    )

    username = StringVar(
        label="Username",
        description="Логин для eAPI (обычно admin)",
        default="admin",
    )

    password = StringVar(
        label="Password",
        description="Пароль для eAPI",
    )

    eapi_port = IntegerVar(
        label="eAPI Port",
        description="443 = HTTPS (рекомендуется), 80 = HTTP",
        default=443,
        min_value=1,
        max_value=65535,
    )

    verify_ssl = BooleanVar(
        label="Проверять SSL сертификат",
        description="Отключите если используется self-signed сертификат",
        default=False,
    )

    dry_run = BooleanVar(
        label="Dry Run",
        description="Показать конфиг без заливки на устройство",
        default=True,
    )

    def run(self, data, commit):
        dry       = data["dry_run"]
        username  = data["username"]
        password  = data["password"]
        port      = data["eapi_port"]
        verify    = data["verify_ssl"]
        devices   = data["devices"]

        if dry:
            self.log_warning("DRY RUN — конфиг будет показан, но НЕ залит на устройство")

        # Убеждаемся что теги существуют
        tag_pending  = get_or_create_tag(TAG_PENDING,  "config-pending",  "ff9800")
        tag_deployed = get_or_create_tag(TAG_DEPLOYED, "config-deployed", "4caf50")

        success_count = 0
        fail_count    = 0

        for device in devices:
            self.log_info(f"{'─' * 60}")
            self.log_info(f"Устройство: {device.name}  |  Platform: {device.platform}")

            # ── Проверяем что это не сервер ──────────────────────────────
            if device.role and device.role.slug in SERVER_ROLE_SLUGS:
                self.log_failure(f"  Пропускаем {device.name} — роль '{device.role}' относится к серверам")
                fail_count += 1
                continue

            # ── Получаем IP для подключения ──────────────────────────────
            mgmt_ip = get_device_ip(device)
            if not mgmt_ip:
                self.log_failure(f"  У устройства {device.name} нет primary IP — пропускаем")
                fail_count += 1
                continue

            self.log_info(f"  Management IP: {mgmt_ip}")

            # ── Генерируем конфиг из NetBox ──────────────────────────────
            commands = [c for c in build_eos_commands(device) if not c.startswith("!")]

            # Показываем конфиг
            self.log_info("  Сгенерированный конфиг:")
            for line in commands:
                if line.startswith("!"):
                    continue
                self.log_info(f"    {line}")

            if dry:
                self.log_success(f"  [DRY] Конфиг для {device.name} готов, деплой пропущен")
                continue

            # ── Подключаемся и заливаем ──────────────────────────────────
            eapi = AristaEAPI(mgmt_ip, username, password, port, verify)

            # Проверка связи
            try:
                eos_version = eapi.check()
                self.log_success(f"  Подключение успешно — EOS {eos_version}")
            except EAPIError as e:
                self.log_failure(f"  Не удалось подключиться к {mgmt_ip}: {e}")
                fail_count += 1
                continue

            # Деплой
            try:
                eapi.run(commands)
                self.log_success(f"  Конфиг залит успешно на {device.name}")
            except EAPIError as e:
                self.log_failure(f"  Ошибка деплоя на {device.name}: {e}")
                fail_count += 1
                continue

            # ── Обновляем статус в NetBox ────────────────────────────────
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
