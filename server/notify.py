#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Failure notice from the server. Usage: notify.py SOURCE "text"
Always appends to <data>/alerts.log. If the Telegram config exists, also sends it to the owner's chat.
The same notice within 24 h is not repeated on Telegram."""
import datetime as dt, json, os, sys, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import central as C

LOG = os.path.join(C.BASE, "alerts.log")
API = C.API


def repeated(key, now):
    """True if the same notice went out in the last 24 h."""
    if not os.path.exists(LOG):
        return False
    for line in open(LOG, encoding="utf-8", errors="replace"):
        p = line.rstrip("\n").split("\t", 2)
        if len(p) == 3 and p[1] + "\t" + p[2] == key:
            try:
                if now - dt.datetime.fromisoformat(p[0]) < dt.timedelta(hours=24):
                    return True
            except ValueError:
                pass
    return False


def telegram(text):
    """Returns None if sent, or the reason it was not."""
    c = C.load_conf()
    if "TELEGRAM_TOKEN" not in c or "TELEGRAM_CHAT_ID" not in c:
        return "no Telegram channel configured"
    data = urllib.parse.urlencode({"chat_id": c["TELEGRAM_CHAT_ID"], "text": text[:4000]}).encode()
    try:
        url = "%s/bot%s/sendMessage" % (API, c["TELEGRAM_TOKEN"])
        with urllib.request.urlopen(url, data=data, timeout=20) as r:
            return None if json.load(r).get("ok") else "Telegram refused"
    except Exception as e:  # the error text may carry the URL with the token: do not log it
        return "Telegram failed: " + type(e).__name__


def notify(source, text):
    os.makedirs(C.BASE, exist_ok=True)
    now = dt.datetime.now()
    text = " ".join(str(text).split())
    key = source + "\t" + text
    if repeated(key, now):  # not logged: the 24 h count from the first notice
        return "repeated"
    with open(LOG, "a", encoding="utf-8") as f:
        f.write("%s\t%s\n" % (now.isoformat(timespec="seconds"), key))
    error = telegram("Server, %s: %s" % (source, text))
    if error:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s\tnotify\t%s\n" % (now.isoformat(timespec="seconds"), error))
    return error or "sent"


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    print(notify(sys.argv[1], sys.argv[2]))
