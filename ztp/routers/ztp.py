import logging
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from config import ZTP_SCRIPT_PATH, H3C_ZTP_SCRIPT_PATH
from netbox_client import get_nb, get_device_by_name, get_platform
from builder import build_config
from templates import sync_templates
from pipeline import get_current_config, make_diff, create_pipeline_run, get_or_create_tag

router = APIRouter()


@router.get("/ztp.py", response_class=PlainTextResponse)
def serve_ztp_script():
    with open(ZTP_SCRIPT_PATH) as f:
        return f.read()


@router.get("/h3c-ztp.py", response_class=PlainTextResponse)
def serve_h3c_ztp_script():
    with open(H3C_ZTP_SCRIPT_PATH) as f:
        return f.read()


@router.get("/debug/{msg}", response_class=PlainTextResponse)
def debug_log(msg: str, request: Request):
    logging.warning("ZTP DEBUG [%s]: %s", request.client.host, msg)
    return "ok"


@router.get("/config/{serial}", response_class=PlainTextResponse)
def get_config(serial: str):
    nb = get_nb()
    device = nb.dcim.devices.get(serial=serial)
    if not device:
        raise HTTPException(status_code=404, detail=f"serial={serial} не найден в NetBox")
    return build_config(nb, device, day0_only=True)


@router.get("/ztp-done/{serial}")
def ztp_done(serial: str):
    nb = get_nb()
    device = nb.dcim.devices.get(serial=serial)
    if not device:
        raise HTTPException(status_code=404, detail=f"serial={serial} не найден")
    tags = [t for t in (device.tags or []) if t.slug != "config-pending"]
    deployed_tag = get_or_create_tag(nb, "config-deployed", "config-deployed", "4caf50")
    tags.append(deployed_tag)
    device.update({"tags": [{"id": t.id} for t in tags], "status": "active"})
    return {"status": "ok", "device": device.name}


@router.post("/webhooks/netbox")
async def netbox_webhook(request: Request):
    body = await request.json()
    event = body.get("event", "")
    model = body.get("model", "")
    data  = body.get("data", {})
    logging.info("NetBox webhook: event=%s model=%s data_keys=%s device_field=%s",
                 event, model, list(data.keys()), data.get("device"))

    device_name = None
    if model == "device":
        device_name = data.get("name")
    elif model in ("interface", "ip-address"):
        device_name = (data.get("device") or {}).get("name")
    elif model == "vlan":
        return {"status": "skipped", "reason": "vlan change — trigger manually"}

    if not device_name:
        return {"status": "skipped", "reason": "cannot determine device"}

    nb = get_nb()
    device = get_device_by_name(nb, device_name)
    platform = get_platform(device)
    generated = None
    error = None
    try:
        generated = build_config(nb, device)
    except Exception as e:
        error = str(e)

    current = get_current_config(device)
    diff    = make_diff(current, generated) if generated else ""
    run_id  = create_pipeline_run(device, platform, generated, current, diff, error)

    logging.info("NetBox webhook: device=%s run_id=%s", device_name, run_id)
    return {"status": "ok", "device": device_name, "run_id": run_id}


@router.post("/webhooks/gitlab")
async def gitlab_webhook(request: Request):
    body    = await request.json()
    ref     = body.get("ref", "")
    project = body.get("project", {}).get("path_with_namespace", "")
    msg     = sync_templates()
    logging.info("GitLab webhook: ref=%s project=%s sync=%s", ref, project, msg)
    return {"status": "ok", "sync": msg}
