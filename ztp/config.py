import os
from dotenv import load_dotenv

load_dotenv()

NETBOX_URL      = os.environ["NETBOX_URL"]
NETBOX_TOKEN    = os.environ["NETBOX_TOKEN"]
ADMIN_PASSWORD  = os.environ.get("ADMIN_PASSWORD", "123456")
SWITCH_USER     = os.environ.get("SWITCH_USER", "admin")
SWITCH_PASSWORD = os.environ.get("SWITCH_PASSWORD", ADMIN_PASSWORD)

GITLAB_TEMPLATES_URL = os.environ.get("GITLAB_TEMPLATES_URL", "")

BASE_DIR            = os.path.dirname(__file__)
ZTP_SCRIPT_PATH     = os.path.join(BASE_DIR, "ztp_script.py")
H3C_ZTP_SCRIPT_PATH = os.path.join(BASE_DIR, "h3c_ztp_script.py")
TEMPLATES_DIR       = os.path.join(BASE_DIR, "templates")
TEMPLATES_REPO_DIR  = os.path.join(BASE_DIR, "templates_repo")
UI_TEMPLATES_DIR    = os.path.join(BASE_DIR, "templates", "ui")
DB_PATH             = os.path.join(BASE_DIR, "pipeline.db")

ROLE_TEMPLATES = {
    ("data-sw", "eos"):     "eos/data-sw.j2",
    ("oam",     "eos"):     "eos/oam.j2",
    ("leaf",    "eos"):     "eos/leaf.j2",
    ("data-sw", "comware"): "comware/data-sw.j2",
    ("oam",     "comware"): "comware/oam.j2",
    ("leaf",    "comware"): "comware/leaf.j2",
}
DEFAULT_TEMPLATES = {
    "eos":     "eos/default.j2",
    "comware": "comware/default.j2",
}

NETWORK_ROLE_SLUGS = {"leaf", "spine", "data-sw", "oam", "access", "distribution", "core", "router"}
