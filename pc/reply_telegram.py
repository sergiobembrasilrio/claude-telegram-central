#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The coordinator session replies to the owner on Telegram. Usage: python -I reply_telegram.py <message id> "reply"
Writes only to the owner's chat. Secrets do not leave: if the text looks like a key or card number, it is refused."""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge, common


def main(argv):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if len(argv) != 3 or not argv[2].strip():
        print('Usage: reply_telegram.py <id> "reply"')
        return 2
    text = argv[2].strip()
    if common.looks_secret(text):
        print("Refused: it looks like a key or card number. Nothing was sent.")
        return 3
    pieces = [text[i:i + 3900] for i in range(0, len(text), 3900)]
    ok = all(bridge.send_telegram("💬 " + p) for p in pieces)
    common.log("listener.log", "reply to %s, %d characters, %s" % (argv[1], len(text), "ok" if ok else "failed"))
    print("sent" if ok else "failed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
