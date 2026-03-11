#!usr/bin/python
# ZTP script for H3C Comware (uses comware/platformtools API)
# Python 2.7 on device

# Fix for H3C restricted Python environment: sys.modules is unavailable,
# which breaks warnings.catch_warnings used internally by urllib2/httplib.
# Patch catch_warnings to a no-op before importing comware.
import warnings

class _NoopCatchWarnings(object):
    def __init__(self, *args, **kwargs):
        pass
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass

warnings.catch_warnings = _NoopCatchWarnings

import comware

ZTP_SERVER = "192.168.1.200"


def get_serial():
    lines = comware.CLI("display device manuinfo")
    if hasattr(lines, "get_output"):
        lines = lines.get_output()
    for line in (lines or []):
        line = line.strip()
        low = line.lower()
        if "device_serial_number" in low or "board sn" in low:
            parts = line.split(":")
            if len(parts) > 1:
                return parts[-1].strip()
    return ""


def main():
    serial = get_serial()

    probe = "debug/serial-" + serial if serial else "debug/no-serial"
    comware.Transfer("http", ZTP_SERVER, probe, "flash:/ztp-debug.tmp")

    if not serial:
        return

    comware.Transfer("http", ZTP_SERVER, "config/" + serial, "flash:/startup.cfg")

    comware.CLI(
        "startup saved-configuration flash:/startup.cfg main ;"
        "reboot force"
    )


main()
