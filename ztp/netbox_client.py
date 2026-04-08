import pynetbox
from fastapi import HTTPException
from config import NETBOX_URL, NETBOX_TOKEN


def get_nb():
    return pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)


def get_device_by_name(nb, name: str):
    device = nb.dcim.devices.get(name=name)
    if not device:
        raise HTTPException(status_code=404, detail=f"Устройство '{name}' не найдено в NetBox")
    return device


def get_platform(device) -> str:
    return device.platform.slug if device.platform else "eos"
