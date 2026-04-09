from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from config import UI_TEMPLATES_DIR, NETWORK_ROLE_SLUGS
from netbox_client import get_nb, get_device_by_name, get_platform
from adapters.arista import get_eapi
from adapters.h3c import get_h3c
from builder import build_config
from pipeline import get_current_config, make_diff, create_pipeline_run, deploy_config
from database import get_db

router = APIRouter()
ui_templates = Jinja2Templates(directory=UI_TEMPLATES_DIR)
ui_templates.env.filters["tojson_load"] = __import__("json").loads


@router.get("/ui", response_class=HTMLResponse)
def ui_dashboard(request: Request):
    with get_db() as conn:
        runs = conn.execute(
            "SELECT * FROM pipeline_runs ORDER BY id DESC LIMIT 100"
        ).fetchall()
    return ui_templates.TemplateResponse("dashboard.html", {
        "request": request, "runs": runs, "active": "dashboard"
    })


@router.get("/ui/devices", response_class=HTMLResponse)
def ui_devices(request: Request):
    nb = get_nb()
    all_roles = list(nb.dcim.device_roles.all())
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


@router.get("/ui/devices/{device_name}", response_class=HTMLResponse)
def ui_device_manage(request: Request, device_name: str,
                     error: str = None, success: str = None):
    nb = get_nb()
    device = get_device_by_name(nb, device_name)
    # Collect VLANs with interface assignments
    vlans = []
    try:
        # vid -> {name, interfaces: [{name, mode}]}
        vid_map: dict[int, dict] = {}
        for iface in nb.dcim.interfaces.filter(device_id=device.id):
            if iface.untagged_vlan:
                v = iface.untagged_vlan
                vid_map.setdefault(v.vid, {"name": v.name, "interfaces": []})
                vid_map[v.vid]["interfaces"].append({"name": iface.name, "mode": "access"})
            for v in (iface.tagged_vlans or []):
                vid_map.setdefault(v.vid, {"name": v.name, "interfaces": []})
                vid_map[v.vid]["interfaces"].append({"name": iface.name, "mode": "trunk"})
        vlans = [
            {"vid": vid, "name": d["name"], "interfaces": d["interfaces"]}
            for vid, d in sorted(vid_map.items())
        ]
    except Exception:
        pass

    role_obj = getattr(device, 'role', None) or getattr(device, 'device_role', None)
    return ui_templates.TemplateResponse("device_manage.html", {
        "request":    request,
        "device_name": device_name,
        "platform":   get_platform(device),
        "role":       role_obj.name if role_obj else "—",
        "primary_ip": str(device.primary_ip4).split("/")[0] if device.primary_ip4 else "—",
        "vlans":      vlans,
        "error":      error,
        "success":    success,
        "active":     "devices",
    })


@router.post("/ui/devices/{device_name}/vlans")
def ui_add_vlan(device_name: str, vid: int = Form(...), name: str = Form("")):
    try:
        nb = get_nb()
        device = get_device_by_name(nb, device_name)
        platform = get_platform(device)

        existing = next(iter(nb.ipam.vlans.filter(vid=vid)), None)
        if not existing:
            nb.ipam.vlans.create({"vid": vid, "name": name or f"VLAN{vid}"})

        if platform == "comware":
            get_h3c(device).create_vlan(vid, name or f"VLAN{vid}")
        else:
            get_eapi(device).run([
                "enable", "configure",
                f"vlan {vid}",
                f"name {name or f'VLAN{vid}'}",
            ])

        return RedirectResponse(f"/ui/devices/{device_name}?success=VLAN+{vid}+added", status_code=303)
    except Exception as e:
        return RedirectResponse(f"/ui/devices/{device_name}?error={str(e)[:120]}", status_code=303)


@router.post("/ui/devices/{device_name}/vlans/{vid}/delete_global")
def ui_delete_vlan_global(device_name: str, vid: int):
    """Delete VLAN globally from switch only — does not touch NetBox."""
    try:
        nb = get_nb()
        device = get_device_by_name(nb, device_name)
        platform = get_platform(device)

        if platform == "comware":
            get_h3c(device)._cli([f"undo vlan {vid}"])
        else:
            get_eapi(device).run(["enable", "configure", f"no vlan {vid}"])

        return RedirectResponse(f"/ui/devices/{device_name}?success=VLAN+{vid}+removed+from+switch", status_code=303)
    except Exception as e:
        return RedirectResponse(f"/ui/devices/{device_name}?error={str(e)[:120]}", status_code=303)


@router.post("/ui/devices/{device_name}/vlans/{vid}/remove_from_port")
def ui_remove_vlan_from_port(device_name: str, vid: int, interface: str = Form(...)):
    """Remove VLAN from a specific interface — updates both switch and NetBox."""
    try:
        nb = get_nb()
        device = get_device_by_name(nb, device_name)
        platform = get_platform(device)

        # Update NetBox interface
        nb_iface = nb.dcim.interfaces.get(device_id=device.id, name=interface)
        is_access = False
        if nb_iface:
            if nb_iface.untagged_vlan and nb_iface.untagged_vlan.vid == vid:
                is_access = True
                nb_iface.update({"untagged_vlan": None, "mode": None})
            else:
                new_tagged = [v.id for v in (nb_iface.tagged_vlans or []) if v.vid != vid]
                nb_iface.update({"tagged_vlans": new_tagged})

        # Update switch — different command for access vs trunk
        if platform == "comware":
            if is_access:
                get_h3c(device)._cli([
                    f"interface {interface}",
                    "undo port access vlan",
                ])
            else:
                get_h3c(device)._cli([
                    f"interface {interface}",
                    f"undo port trunk permit vlan {vid}",
                ])
        else:
            if is_access:
                get_eapi(device).run([
                    "enable", "configure",
                    f"interface {interface}",
                    "no switchport access vlan",
                ])
            else:
                get_eapi(device).run([
                    "enable", "configure",
                    f"interface {interface}",
                    f"switchport trunk allowed vlan remove {vid}",
                ])

        return RedirectResponse(f"/ui/devices/{device_name}?success=VLAN+{vid}+removed+from+{interface}", status_code=303)
    except Exception as e:
        return RedirectResponse(f"/ui/devices/{device_name}?error={str(e)[:120]}", status_code=303)


@router.post("/ui/devices/{device_name}/trunk")
def ui_trunk(device_name: str, interface: str = Form(...), vlans: str = Form(...)):
    try:
        vlan_list = [int(v.strip()) for v in vlans.split(",") if v.strip()]
        nb = get_nb()
        device = get_device_by_name(nb, device_name)
        site_id  = device.site.id if device.site else None
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


@router.post("/ui/devices/{device_name}/access")
def ui_access(device_name: str, interface: str = Form(...),
              vlan: int = Form(...), description: str = Form("")):
    try:
        nb = get_nb()
        device = get_device_by_name(nb, device_name)
        site_id  = device.site.id if device.site else None
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


@router.post("/ui/generate/{device_name}")
def ui_generate(device_name: str):
    nb = get_nb()
    device = get_device_by_name(nb, device_name)
    platform = get_platform(device)

    generated = ""
    error = None
    try:
        generated = build_config(nb, device)
    except Exception as e:
        error = str(e)

    current = get_current_config(device)
    diff    = make_diff(current, generated) if generated else ""
    run_id  = create_pipeline_run(device, platform, generated, current, diff, error)

    return RedirectResponse(f"/ui/runs/{run_id}", status_code=303)


@router.get("/ui/runs/{run_id}", response_class=HTMLResponse)
def ui_run_detail(request: Request, run_id: int):
    with get_db() as conn:
        run = conn.execute("SELECT * FROM pipeline_runs WHERE id = ?", (run_id,)).fetchone()
    if not run:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Run not found")
    return ui_templates.TemplateResponse("run_detail.html", {
        "request": request, "run": run, "active": "dashboard"
    })


@router.post("/ui/runs/{run_id}/approve")
def ui_approve(run_id: int):
    with get_db() as conn:
        run = conn.execute("SELECT * FROM pipeline_runs WHERE id = ?", (run_id,)).fetchone()
        if not run:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Run not found")

    nb = get_nb()
    device = get_device_by_name(nb, run["device_name"])
    error  = None

    check_result = None
    try:
        deploy_config(device, run["generated_config"])
        status = "deployed"
        # Обновляем теги в NetBox
        from pipeline import get_or_create_tag, post_deploy_check
        current_tags = [t for t in (device.tags or []) if t.slug not in ("config-pending", "day0-deployed")]
        deployed_tag = get_or_create_tag(nb, "config-deployed", "config-deployed", "4caf50")
        current_tags.append(deployed_tag)
        device.update({"tags": [{"id": t.id} for t in current_tags]})
        # Проверяем состояние BGP/OSPF после деплоя
        import json
        check_result = json.dumps(post_deploy_check(device), ensure_ascii=False)
    except Exception as e:
        error  = str(e)
        status = "failed"

    with get_db() as conn:
        conn.execute(
            "UPDATE pipeline_runs SET status=?, error=?, check_result=?, updated_at=datetime('now','localtime') WHERE id=?",
            (status, error, check_result, run_id)
        )
        conn.commit()

    return RedirectResponse(f"/ui/runs/{run_id}", status_code=303)


@router.post("/ui/devices/{device_name}/rollback")
def ui_rollback(device_name: str):
    from builder import build_config
    from pipeline import deploy_config_replace
    try:
        nb = get_nb()
        device = get_device_by_name(nb, device_name)
        config = build_config(nb, device, day0_only=True)
        deploy_config_replace(device, config)
        return RedirectResponse(f"/ui/devices/{device_name}?success=Rollback+to+Day0+complete", status_code=303)
    except Exception as e:
        return RedirectResponse(f"/ui/devices/{device_name}?error={str(e)[:120]}", status_code=303)


@router.post("/ui/runs/{run_id}/reject")
def ui_reject(run_id: int):
    with get_db() as conn:
        conn.execute(
            "UPDATE pipeline_runs SET status='rejected', updated_at=datetime('now','localtime') WHERE id=?",
            (run_id,)
        )
        conn.commit()
    return RedirectResponse(f"/ui/runs/{run_id}", status_code=303)
