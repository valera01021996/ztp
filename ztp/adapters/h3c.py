import time
from fastapi import HTTPException
from config import SWITCH_USER, SWITCH_PASSWORD

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

    def get_vlans(self) -> list:
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

    def _cli(self, commands: list):
        import paramiko

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
            if "change" in output.lower() or "[y/n]" in output.lower():
                shell.send("n\n")
                _recv(shell, 1)
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

    def set_trunk_vlans(self, interface: str, vlans: list):
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


def get_h3c(device) -> H3CNetconf:
    if not device.primary_ip4:
        raise HTTPException(status_code=400, detail=f"У устройства {device.name} не задан primary_ip4")
    ip = str(device.primary_ip4).split("/")[0]
    return H3CNetconf(ip, SWITCH_USER, SWITCH_PASSWORD)
