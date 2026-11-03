#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The coordinator session sends files to the owner on Telegram, through the server.
Usage: python -I send_files_telegram.py "caption" file1 [file2 ...]
Writes only to the owner's chat, using the token that is already on the server; the temporary copy on the server is deleted at the end.
Note: each file is read into memory on the server while it is uploaded, so keep the limit below the server's free RAM."""
import os, shlex, subprocess, sys, uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge, common

LIMIT = int(float(common.setting("CENTRAL_MAX_SEND_MB", "2000")) * 1024 * 1024)
# Runs on the server: imports central.py for the token, chat id and API address.
SERVER_SIDE = r'''
import json, os, sys, uuid
sys.path.insert(0, os.path.expanduser(sys.argv[3]))
import central
c = central.conf()
path, caption = sys.argv[1], sys.argv[2]
boundary = uuid.uuid4().hex
body = b""
for k, v in (("chat_id", c["TELEGRAM_CHAT_ID"]), ("caption", caption[:1000])):
    body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode()
body += ("--%s\r\nContent-Disposition: form-data; name=\"document\"; filename=\"%s\"\r\nContent-Type: application/octet-stream\r\n\r\n" % (boundary, os.path.basename(path))).encode() + open(path, "rb").read() + ("\r\n--%s--\r\n" % boundary).encode()
import urllib.request
req = urllib.request.Request("%s/bot%s/sendDocument" % (central.API, c["TELEGRAM_TOKEN"]), body, {"Content-Type": "multipart/form-data; boundary=" + boundary})
try:
    with urllib.request.urlopen(req, timeout=300) as r:
        print("ok", json.load(r)["result"]["message_id"])
except Exception as e:  # the error text may carry the URL with the token
    print("failed", type(e).__name__)
    sys.exit(1)
'''


def scp_command():
    cmd = ["scp", "-q", "-o", "BatchMode=yes"]
    key = common.setting("CENTRAL_SSH_KEY")
    return cmd + (["-i", os.path.expanduser(key)] if key else [])


def main(argv):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if len(argv) < 3:
        print('Usage: send_files_telegram.py "caption" file1 [file2 ...]')
        return 2
    caption, files = argv[1], argv[2:]
    for a in files:
        if not os.path.isfile(a) or os.path.getsize(a) > LIMIT:
            print("Refused, nothing was sent: file missing or too big:", a)
            return 3
    ssh, host = bridge.ssh_command(), common.setting("CENTRAL_SSH_HOST")
    folder = "/tmp/central-send-" + uuid.uuid4().hex
    failures = 0
    subprocess.run(ssh + ["mkdir -p -m 700 " + folder], check=True, timeout=60)
    try:
        for a in files:
            name = os.path.basename(a)
            dest = folder + "/" + name
            subprocess.run(scp_command() + [a, "%s:%s" % (host, shlex.quote(dest))], check=True, timeout=600)
            cmd = "python3 - %s %s %s" % (shlex.quote(dest), shlex.quote(caption + " | " + name), shlex.quote(bridge.remote_code()))
            r = subprocess.run(ssh + [cmd], input=SERVER_SIDE.encode("utf-8"), capture_output=True, timeout=600)
            out = r.stdout.decode("utf-8", "replace").strip()
            print(name, "->", out or "failed with no output")
            failures += r.returncode != 0
    finally:
        subprocess.run(ssh + ["rm -rf " + folder], timeout=60)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
