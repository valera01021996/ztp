#!/usr/bin/env python
# ZTP script for Arista EOS (Python 2/3 compatible, ASCII only)

from __future__ import print_function
import json
import subprocess
import sys

try:
    from urllib.request import urlopen
    from urllib.error import HTTPError
except ImportError:
    from urllib2 import urlopen, HTTPError

ZTP_SERVER = "http://192.168.1.200"


def log(msg):
    print("[ZTP] " + str(msg))
    sys.stdout.flush()


def get_serial():
    try:
        out = subprocess.check_output(["FastCli", "-c", "show version | json"])
        if isinstance(out, bytes):
            out = out.decode("utf-8")
        data = json.loads(out)
        return data.get("serialNumber", "").strip()
    except Exception as e:
        log("Error getting serial number: " + str(e))
        return None


def get_config(serial):
    url = ZTP_SERVER + "/config/" + serial
    log("Fetching config from: " + url)
    try:
        resp = urlopen(url, timeout=30)
        data = resp.read()
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        return data
    except HTTPError as e:
        if e.code == 404:
            log("Device serial=" + serial + " not found in NetBox")
        else:
            log("HTTP error: " + str(e.code))
        return None
    except Exception as e:
        log("Config request error: " + str(e))
        return None


def apply_config(config):
    path = "/mnt/flash/startup-config"
    with open(path, "w") as f:
        f.write(config)
    log("Config written to " + path)


def notify_done(serial):
    url = ZTP_SERVER + "/ztp-done/" + serial
    try:
        urlopen(url, timeout=10)
        log("Server notified of successful ZTP")
    except Exception as e:
        log("Failed to notify server: " + str(e))


def main():
    log("ZTP started")

    serial = get_serial()
    if not serial:
        log("Could not get serial number - exiting")
        sys.exit(1)

    log("Serial: " + serial)

    config = get_config(serial)
    if not config:
        log("Config not received - exiting")
        sys.exit(1)

    apply_config(config)
    notify_done(serial)

    log("ZTP completed. Switch will reload.")


main()
