"""
NetBox Custom Script — импорт из YAML (inventory.yml).

Установка:
  Скопировать этот файл в директорию scripts/ вашего NetBox.
  NetBox UI → Customization → Scripts → появится "YAML Inventory Import"

Зависимость: PyYAML (уже включён в NetBox).
"""

import re
import ipaddress as _ipaddress

import yaml

try:
    from netbox.scripts import Script, FileVar, BooleanVar
except ImportError:
    from extras.scripts import Script, FileVar, BooleanVar
from django.contrib.contenttypes.models import ContentType

from dcim.models import (
    Site, Location, Rack,
    Manufacturer, DeviceType, Platform,
    Device, Interface,
)
try:
    from dcim.models import Role as DeviceRole
except ImportError:
    from dcim.models import DeviceRole
from ipam.models import VLAN, VRF, IPAddress, Prefix
from extras.models import Tag


# ─── Утилиты ─────────────────────────────────────────────────────────────────

IFACE_TYPE_MAP = {
    "1000base-t":   "1000base-t",
    "10gbase-t":    "10gbase-t",
    "10gbase-x":    "10gbase-x-sfpp",
    "25gbase-x":    "25gbase-x-sfp28",
    "40gbase-x":    "40gbase-x-qsfpp",
    "100gbase-x":   "100gbase-x-qsfp28",
    "400gbase-x":   "400gbase-x-qsfpdd",
    "virtual":      "virtual",
    "lag":          "lag",
}

NETWORK_PLATFORMS = {"eos", "arista-eos", "arista", "vrp", "comware", "ios", "nxos", "junos"}
SERVER_ROLE_SLUGS = {"server", "servers", "vm", "virtual-machine", "esxi", "hypervisor"}


def slugify(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s[:100]


def normalize_type(t: str) -> str:
    return IFACE_TYPE_MAP.get((t or "").strip().lower(), t or "1000base-t")


# ─── ORM-хелперы ─────────────────────────────────────────────────────────────

def _get_or_create(model, lookup: dict, defaults: dict = None):
    obj, created = model.objects.get_or_create(**lookup, defaults=defaults or {})
    return obj, created


def get_or_create_site(name: str):
    slug = slugify(name)
    obj = Site.objects.filter(slug=slug).first()
    if obj:
        return obj, False
    return Site.objects.get_or_create(
        name=name, defaults={"slug": slug, "status": "active"},
    )


def get_or_create_location(name: str, site: Site):
    return Location.objects.get_or_create(
        name=name, site=site, defaults={"slug": slugify(name)},
    )


def get_or_create_rack(name: str, site: Site, location=None):
    return Rack.objects.get_or_create(
        name=name, site=site, defaults={"status": "active", "location": location},
    )


def get_or_create_manufacturer(name: str):
    slug = slugify(name)
    obj = Manufacturer.objects.filter(slug=slug).first()
    if obj:
        return obj, False
    return Manufacturer.objects.get_or_create(name=name, defaults={"slug": slug})


def get_or_create_device_type(model: str, manufacturer: Manufacturer, height: int = 1):
    return DeviceType.objects.get_or_create(
        manufacturer=manufacturer, model=model,
        defaults={"slug": slugify(model), "u_height": height or 1},
    )


def get_or_create_role(name: str):
    slug = slugify(name)
    obj = DeviceRole.objects.filter(slug=slug).first()
    if obj:
        return obj, False
    return DeviceRole.objects.create(name=name, slug=slug, color="2196f3"), True


def get_or_create_platform(name: str):
    slug = slugify(name)
    obj = Platform.objects.filter(slug=slug).first()
    if obj:
        return obj, False
    return Platform.objects.get_or_create(name=name, defaults={"slug": slug})


def get_or_create_vrf(name: str, description: str = ""):
    return VRF.objects.get_or_create(name=name, defaults={"description": description})


def _get_or_create_tag(slug: str, name: str, color: str = "ff9800") -> Tag:
    tag, _ = Tag.objects.get_or_create(slug=slug, defaults={"name": name, "color": color})
    return tag


# ─── Основной скрипт ─────────────────────────────────────────────────────────

class YAMLInventoryImport(Script):

    class Meta:
        name = "YAML Inventory Import"
        description = "Импорт устройств, VRF, VLAN, интерфейсов и port-channel из YAML-файла"
        field_order = ["yaml_file", "dry_run"]

    yaml_file = FileVar(
        label="YAML файл",
        description="inventory.yml — файл с описанием инфраструктуры",
    )
    dry_run = BooleanVar(
        label="Dry Run",
        description="Показать что будет создано — без записи в базу",
        default=False,
    )

    def run(self, data, commit=True):
        dry = data["dry_run"]
        if dry:
            self.log_warning("DRY RUN — изменений в базе не будет")

        file_obj = data["yaml_file"]
        content = file_obj.read() if hasattr(file_obj, "read") else file_obj
        if isinstance(content, bytes):
            content = content.decode("utf-8")

        inventory = yaml.safe_load(content)
        if not isinstance(inventory, dict):
            self.log_failure("Невалидный YAML — ожидается словарь с ключами: vrfs, vlans, devices")
            return

        self._import_vrfs(inventory.get("vrfs", []), dry)
        self._import_vlans(inventory.get("vlans", []), dry)
        self._import_devices(inventory.get("devices", []), dry)

        if dry:
            raise Exception("DRY RUN завершён — транзакция откатана")

    # ── VRFs ──────────────────────────────────────────────────────────────────

    def _import_vrfs(self, vrfs: list, dry: bool):
        if not vrfs:
            self.log_info("Секция vrfs: данных нет")
            return

        self.log_info(f"VRFs: {len(vrfs)} записей")

        for entry in vrfs:
            name = entry.get("name")
            if not name:
                continue

            description = entry.get("description", "")

            if dry:
                self.log_success(f"[DRY] Создал бы VRF: {name}")
                continue

            vrf, created = get_or_create_vrf(name, description)
            if created:
                self.log_success(f"Создан VRF: {name}")
            else:
                updated = False
                if description and vrf.description != description:
                    vrf.description = description
                    updated = True
                if updated:
                    vrf.save()
                    self.log_info(f"VRF обновлён: {name}")
                else:
                    self.log_info(f"VRF уже существует: {name}")

    # ── VLANs ─────────────────────────────────────────────────────────────────

    def _import_vlans(self, vlans: list, dry: bool):
        if not vlans:
            self.log_info("Секция vlans: данных нет")
            return

        self.log_info(f"VLANs: {len(vlans)} записей")

        for entry in vlans:
            vid = entry.get("vid")
            name = entry.get("name")

            if not vid or not name:
                continue

            if dry:
                self.log_success(f"[DRY] Создал бы VLAN {vid} ({name})")
                continue

            vlan, created = VLAN.objects.get_or_create(
                vid=vid,
                defaults={"name": name, "status": "active"},
            )
            if created:
                self.log_success(f"Создан VLAN {vid}: {name}")
            else:
                self.log_info(f"VLAN уже существует: {vid} ({name})")

    # ── Devices ───────────────────────────────────────────────────────────────

    def _import_devices(self, devices: list, dry: bool):
        if not devices:
            self.log_info("Секция devices: данных нет")
            return

        self.log_info(f"Devices: {len(devices)} записей")

        for entry in devices:
            name = entry.get("name")
            if not name:
                continue

            site_name = entry.get("site", "Default")
            loc_name = entry.get("location")
            role_name = entry.get("role", "Network Device")
            dtype_name = entry.get("device_type", "Unknown")
            mfr_name = entry.get("manufacturer", "Generic")
            platform_name = entry.get("platform", "eos")
            rack_name = entry.get("rack")
            position = entry.get("position")
            face = entry.get("rackface", "front")
            height = entry.get("height", 1)
            status = entry.get("status", "active")

            if dry:
                self.log_success(f"[DRY] Создал бы Device: {name} (site={site_name})")
                self._dry_run_interfaces(name, entry.get("interfaces", []))
                self._dry_run_port_channels(name, entry.get("port_channels", []))
                if entry.get("config_context"):
                    keys = list(entry["config_context"].keys())
                    self.log_success(f"  [DRY] Config context: {keys}")
                continue

            # Проверяем существование
            if Device.objects.filter(name=name).exists():
                self.log_info(f"Устройство уже существует: {name}")
                device = Device.objects.get(name=name)
            else:
                # Создаём зависимости
                site, _ = get_or_create_site(site_name)
                mfr, _ = get_or_create_manufacturer(mfr_name)
                dtype, _ = get_or_create_device_type(dtype_name, mfr, height)
                role, _ = get_or_create_role(role_name)
                platform, _ = get_or_create_platform(platform_name)

                location = None
                if loc_name:
                    location, _ = get_or_create_location(loc_name, site)

                rack = None
                if rack_name:
                    rack, _ = get_or_create_rack(rack_name, site, location)

                try:
                    device = Device.objects.create(
                        name=name,
                        device_type=dtype,
                        role=role,
                        platform=platform,
                        site=site,
                        rack=rack,
                        face=face if rack else "",
                        position=position if rack else None,
                        status=status,
                    )
                    self.log_success(f"Создано устройство: {name}")
                    self._tag_if_network(device, platform_name, role_name)
                except Exception as e:
                    self.log_failure(f"Ошибка создания {name}: {e}")
                    continue

            # Импорт интерфейсов, port-channel и config_context
            self._import_interfaces(device, entry.get("interfaces", []))
            self._import_port_channels(device, entry.get("port_channels", []))
            self._import_config_context(device, entry.get("config_context"))

    def _import_config_context(self, device: Device, context: dict):
        if not context:
            return
        device.local_context_data = context
        device.save()
        self.log_success(f"  Config context загружен → {device.name}")

    def _tag_if_network(self, device: Device, platform_name: str, role_name: str):
        role_slug = slugify(role_name) if role_name else ""
        if role_slug not in SERVER_ROLE_SLUGS and (platform_name or "").lower() in NETWORK_PLATFORMS:
            tag = _get_or_create_tag("config-pending", "config-pending", "ff9800")
            device.tags.add(tag)
            self.log_success(f"  Тег 'config-pending' → {device.name}")

    # ── Interfaces ────────────────────────────────────────────────────────────

    def _import_interfaces(self, device: Device, interfaces: list):
        if not interfaces:
            return

        for entry in interfaces:
            iface_name = entry.get("name")
            if not iface_name:
                continue

            iface_type = normalize_type(entry.get("type"))
            description = entry.get("description", "")
            enabled = entry.get("enabled", True)
            mode = entry.get("mode")
            mtu = entry.get("mtu")
            vrf_name = entry.get("vrf")
            untagged_vid = entry.get("untagged_vlan")
            tagged_vids = entry.get("tagged_vlans", [])
            ip_addr = entry.get("ip")
            is_primary = entry.get("primary", False)

            # Определяем mode для NetBox
            nb_mode = ""
            if mode:
                nb_mode = mode.lower()
            elif untagged_vid and tagged_vids:
                nb_mode = "tagged"
            elif untagged_vid:
                nb_mode = "access"
            elif tagged_vids:
                nb_mode = "tagged"

            vrf = None
            if vrf_name:
                vrf, _ = get_or_create_vrf(vrf_name)

            untagged_vlan = VLAN.objects.filter(vid=untagged_vid).first() if untagged_vid else None
            tagged_vlans = list(VLAN.objects.filter(vid__in=tagged_vids)) if tagged_vids else []

            defaults = {
                "type": iface_type,
                "description": description,
                "enabled": enabled,
                "mtu": mtu,
                "mode": nb_mode,
                "untagged_vlan": untagged_vlan,
                "vrf": vrf,
            }

            iface, created = Interface.objects.get_or_create(
                device=device,
                name=iface_name,
                defaults=defaults,
            )

            if not created:
                for k, v in defaults.items():
                    setattr(iface, k, v)
                iface.save()
                self.log_info(f"  Обновлён интерфейс: {device.name} / {iface_name}")
            else:
                self.log_success(f"  Создан интерфейс: {device.name} / {iface_name}")

            if tagged_vlans:
                iface.tagged_vlans.set(tagged_vlans)

            # Создаём IP-адрес если указан
            if ip_addr:
                self._create_ip(device, iface, ip_addr, is_primary)

    # ── Port-Channels ─────────────────────────────────────────────────────────

    def _import_port_channels(self, device: Device, port_channels: list):
        if not port_channels:
            return

        for entry in port_channels:
            pc_name = entry.get("name")
            if not pc_name:
                continue

            description = entry.get("description", "")
            mtu = entry.get("mtu")
            members = entry.get("members", [])
            untagged_vid = entry.get("untagged_vlan")
            tagged_vids = entry.get("tagged_vlans", [])

            # Определяем switchport mode
            nb_mode = ""
            if untagged_vid and tagged_vids:
                nb_mode = "tagged"
            elif untagged_vid:
                nb_mode = "access"
            elif tagged_vids:
                nb_mode = "tagged"

            untagged_vlan = VLAN.objects.filter(vid=untagged_vid).first() if untagged_vid else None
            tagged_vlans = list(VLAN.objects.filter(vid__in=tagged_vids)) if tagged_vids else []

            # Создаём LAG-интерфейс
            defaults = {
                "type": "lag",
                "description": description,
                "mtu": mtu,
                "enabled": True,
                "mode": nb_mode,
                "untagged_vlan": untagged_vlan,
            }

            lag_iface, created = Interface.objects.get_or_create(
                device=device,
                name=pc_name,
                defaults=defaults,
            )

            if not created:
                for k, v in defaults.items():
                    setattr(lag_iface, k, v)
                lag_iface.save()
                self.log_info(f"  Обновлён port-channel: {device.name} / {pc_name}")
            else:
                self.log_success(f"  Создан port-channel: {device.name} / {pc_name}")

            if tagged_vlans:
                lag_iface.tagged_vlans.set(tagged_vlans)

            # Привязываем member-интерфейсы к LAG
            for member_name in members:
                member = Interface.objects.filter(device=device, name=member_name).first()
                if not member:
                    # Создаём member-интерфейс если его нет
                    member = Interface.objects.create(
                        device=device,
                        name=member_name,
                        type="100gbase-x-qsfp28",
                        enabled=True,
                    )
                    self.log_success(f"    Создан member-интерфейс: {member_name}")

                member.lag = lag_iface
                member.save()
                self.log_success(f"    {member_name} → {pc_name}")

    # ── IP-адреса ─────────────────────────────────────────────────────────────

    def _create_ip(self, device: Device, iface: Interface, address: str, is_primary: bool):
        # Создаём префикс
        try:
            network = str(_ipaddress.ip_interface(address).network)
        except ValueError:
            self.log_failure(f"  Невалидный IP: {address}")
            return

        prefix_obj = Prefix.objects.filter(prefix=network).first()
        if prefix_obj is None:
            prefix_obj = Prefix.objects.create(prefix=network, status="active")
            self.log_success(f"  Создан Prefix: {network}")

        # Создаём IP
        ct = ContentType.objects.get_for_model(Interface)
        ip_obj, created = IPAddress.objects.get_or_create(
            address=address,
            defaults={
                "status": "active",
                "assigned_object_type": ct,
                "assigned_object_id": iface.pk,
            },
        )

        if created:
            self.log_success(f"  Создан IP: {address} → {device.name}/{iface.name}")
        else:
            if ip_obj.assigned_object_id != iface.pk:
                ip_obj.assigned_object_type = ct
                ip_obj.assigned_object_id = iface.pk
                ip_obj.save()
                self.log_info(f"  Обновлена привязка IP: {address} → {device.name}/{iface.name}")
            else:
                self.log_info(f"  IP уже существует: {address}")

        # Primary IP
        if is_primary:
            ip_str = address.split("/")[0]
            if ":" in ip_str:
                device.primary_ip6 = ip_obj
            else:
                device.primary_ip4 = ip_obj
            device.save()
            self.log_success(f"  Primary IP: {address} → {device.name}")

    # ── Dry-run хелперы ───────────────────────────────────────────────────────

    def _dry_run_interfaces(self, device_name: str, interfaces: list):
        for entry in interfaces:
            iface_name = entry.get("name", "?")
            ip = entry.get("ip", "")
            ip_info = f" ip={ip}" if ip else ""
            self.log_success(f"  [DRY] Interface: {device_name} / {iface_name}{ip_info}")

    def _dry_run_port_channels(self, device_name: str, port_channels: list):
        for entry in port_channels:
            pc_name = entry.get("name", "?")
            members = entry.get("members", [])
            self.log_success(f"  [DRY] Port-Channel: {device_name} / {pc_name} members={members}")
