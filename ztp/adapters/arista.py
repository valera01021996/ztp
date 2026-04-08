import json
import urllib.request
import urllib.error
from base64 import b64encode
from fastapi import HTTPException
from config import SWITCH_USER, SWITCH_PASSWORD


class AristaEAPI:
    def __init__(self, ip: str, username: str, password: str):
        self.url = f"http://{ip}/command-api"
        creds = b64encode(f"{username}:{password}".encode()).decode()
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Basic {creds}",
        }

    def run(self, commands: list) -> list:
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


def get_eapi(device) -> AristaEAPI:
    if not device.primary_ip4:
        raise HTTPException(status_code=400, detail=f"У устройства {device.name} не задан primary_ip4")
    ip = str(device.primary_ip4).split("/")[0]
    return AristaEAPI(ip, SWITCH_USER, SWITCH_PASSWORD)
