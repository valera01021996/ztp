#!usr/bin/python
# ZTP script for H3C Comware (uses comware/platformtools API)
# Python 2.7 on device
import comware

ZTP_SERVER = "http://192.168.1.200"


def get_serial():
    # display device manuinfo output example:
    # Slot 1:
    #   Board SN:     210235A09CH123456789
    lines = comware.CLI("display device manuinfo", False)
    if hasattr(lines, "get_output"):
        lines = lines.get_output()
    for line in (lines or []):
        line = line.strip()
        low = line.lower()
        if "board sn" in low or "device sn" in low:
            parts = line.split(":")
            if len(parts) > 1:
                return parts[-1].strip()
    return ""


def main():
    serial = get_serial()
    if not serial:
        comware.CLI("system-view ;info-center loghost 192.168.1.200 ;return", False)
        return

    # Download config from ZTP server to flash
    url_path = "config/" + serial
    result = comware.Transfer("http", "192.168.1.200", url_path, "flash:/startup.cfg")

    # Set as startup config and reboot
    comware.CLI(
        "startup saved-configuration flash:/startup.cfg main ;"
        "reboot force",
        False,
    )


main()
