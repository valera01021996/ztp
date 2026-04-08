import os
import re
import ipaddress
import logging
from jinja2 import Environment, FileSystemLoader
from config import TEMPLATES_DIR, TEMPLATES_REPO_DIR, GITLAB_TEMPLATES_URL


def _cidr_to_mask(cidr: str) -> str:
    iface = ipaddress.ip_interface(cidr)
    return f"{iface.ip} {iface.netmask}"


def _comware_iface_name(name: str) -> str:
    m = re.match(r'^[Vv][Ll][Aa][Nn]\s*(\d+)$', name.strip())
    if m:
        return "Vlan-interface" + m.group(1)
    return name


def make_jinja_env(templates_dir: str) -> Environment:
    env = Environment(
        loader=FileSystemLoader(templates_dir),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["cidr_to_mask"]  = _cidr_to_mask
    env.filters["comware_iface"] = _comware_iface_name
    env.tests["match"]      = lambda value, pattern: bool(re.match(pattern, str(value)))
    env.tests["vlan_iface"] = lambda value: bool(
        re.match(r'^[Vv][Ll][Aa][Nn]\s*\d+$', str(value).strip())
    )
    return env


_jinja_env = make_jinja_env(TEMPLATES_DIR)


def get_jinja_env() -> Environment:
    return _jinja_env


def sync_templates() -> str:
    global _jinja_env
    if not GITLAB_TEMPLATES_URL:
        return "GITLAB_TEMPLATES_URL not set, using local templates"
    try:
        from git import Repo
        if os.path.exists(os.path.join(TEMPLATES_REPO_DIR, ".git")):
            repo = Repo(TEMPLATES_REPO_DIR)
            repo.remotes.origin.pull()
            msg = "Templates pulled from GitLab"
        else:
            os.makedirs(TEMPLATES_REPO_DIR, exist_ok=True)
            Repo.clone_from(GITLAB_TEMPLATES_URL, TEMPLATES_REPO_DIR)
            msg = "Templates cloned from GitLab"
        _jinja_env = make_jinja_env(TEMPLATES_REPO_DIR)
        logging.info(msg)
        return msg
    except Exception as e:
        logging.warning("Template sync failed: %s — using local templates", e)
        return f"sync failed: {e}"
