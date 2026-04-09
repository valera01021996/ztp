import difflib
import logging
from fastapi import HTTPException
from config import SWITCH_USER, SWITCH_PASSWORD
from netbox_client import get_platform
from adapters.arista import get_eapi
from adapters.h3c import get_h3c
from database import get_db


def _device_creds(device) -> dict:
    if not device.primary_ip4:
        raise HTTPException(status_code=400, detail=f"У устройства {device.name} не задан primary_ip4")
    return {
        "ip":       str(device.primary_ip4).split("/")[0],
        "username": SWITCH_USER,
        "password": SWITCH_PASSWORD,
    }


def get_current_config(device) -> str:
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
        logging.warning("Could not get current config from %s: %s", device.name, e)
        return f"# Could not connect to device: {e}"
    return ""


def make_diff(current: str, generated: str) -> str:
    a = (current or "").splitlines(keepends=True)
    b = (generated or "").splitlines(keepends=True)
    return "".join(difflib.unified_diff(a, b, fromfile="current", tofile="generated", lineterm="\n"))


def get_or_create_tag(nb, slug: str, name: str, color: str = "00bcd4"):
    tag = nb.extras.tags.get(slug=slug)
    if not tag:
        tag = nb.extras.tags.create({"name": name, "slug": slug, "color": color})
    return tag


def create_pipeline_run(device, platform: str, generated: str, current: str,
                        diff: str, error: str = None) -> int:
    status = "pending" if not error else "failed"
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO pipeline_runs
               (device_name, device_serial, platform, status, generated_config, current_config, diff, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (device.name, device.serial or "", platform, status,
             generated, current, diff, error)
        )
        run_id = cur.lastrowid
        conn.commit()
    return run_id


def deploy_config(device, config: str):
    platform = get_platform(device)
    if platform == "eos":
        cmds = ["enable", "configure terminal"]
        for line in config.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("!") and stripped != "end":
                cmds.append(stripped)
        cmds.append("end")
        cmds.append("write memory")
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
