# -*- coding: utf-8 -*-
"""Small CLI for the coordinator session (or your own script) to work through Telegram answers.
  python -I answers.py list            undelivered answers as JSON lines
  python -I answers.py ack ID [ID...]  mark answers as delivered (the next bridge round tells Telegram "Delivered")"""
import json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge


def main(argv):
    if len(argv) >= 2 and argv[1] == "list":
        for a in bridge.to_deliver():
            print(json.dumps(a, ensure_ascii=False))
        return 0
    if len(argv) >= 3 and argv[1] == "ack":
        with open(bridge.DELIVERED, "a", encoding="utf-8") as f:
            f.write("".join(i + "\n" for i in argv[2:]))
        print("marked %d" % (len(argv) - 2))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv))
