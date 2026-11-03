#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Carries what the owner sends on Telegram (text, transcribed audio, photo, file) to the coordinator Claude Code session, with no AI in the path.
The coordinator session starts this in the background:  python -I listener.py --wait
The process listens at no cost and exits when a message arrives: its output enters the session and wakes it. The session answers
with reply_telegram.py and starts the listener again.

Queue: inbox-queue.jsonl, brought from the server by bridge.py (here every 20 s; from the watcher, every few minutes). State: inbox-state.json.
Attachments land in CENTRAL_DOWNLOAD_DIR (default <data>/downloads/YYYY-MM-DD/) and are data: nothing here executes or opens them.
After delivery and verification the server deletes its copy. With nobody listening (app closed or listener dead), the watcher warns once
and the message stays queued."""
import datetime as dt, hashlib, json, os, re, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge, common

HERE = os.path.dirname(os.path.abspath(__file__))
INBOX = bridge.INBOX
STATE = bridge.INBOX_STATE
ALIVE = common.path("listener-alive.txt")
LOCK = common.path("listener.lock")
REPLY = os.path.join(HERE, "reply_telegram.py")
LOG = "listener.log"
POLL_S = 20
LISTENING_S = 120     # a signal older than this means nobody is listening


def download_dir():
    return os.path.expanduser(common.setting("CENTRAL_DOWNLOAD_DIR") or os.path.join(common.DATA_DIR, "downloads"))


def scp_command():
    cmd = ["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]
    key = common.setting("CENTRAL_SSH_KEY")
    return cmd + (["-i", os.path.expanduser(key)] if key else [])


def remote_file(name):
    return "%s:%s/received/%s" % (common.setting("CENTRAL_SSH_HOST"), common.setting("CENTRAL_REMOTE_DATA", "claude-telegram-central/data"), name)


def read_state():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=0)
    os.replace(tmp, STATE)


def pending(st):
    try:
        queue = [json.loads(l) for l in open(INBOX, encoding="utf-8") if l.strip()]
    except (OSError, ValueError):
        return []
    seen, out = set(), []
    for m in queue:
        if m["pid"] not in seen and not st.get(m["pid"], {}).get("delivered"):
            out.append(m)
        seen.add(m["pid"])
    return out


def listening():
    try:
        return time.time() - os.path.getmtime(ALIVE) < LISTENING_S
    except OSError:
        return False


def safe_name(m):
    base = re.sub(r"[^\w.-]+", "_", os.path.basename(m.get("name") or "attachment")).strip("._") or "attachment"
    ext = os.path.splitext(m.get("file") or "")[1]
    if not os.path.splitext(base)[1]:
        base += ext
    return "%s-%s" % (m["pid"], base[-80:])


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1048576), b""):
            h.update(block)
    return h.hexdigest()


def fetch(m, st):
    """Fetch the attachment from the server and verify it. True when it is here, or there is nothing to fetch."""
    e = st.setdefault(m["pid"], {})
    if not m.get("file") or e.get("local"):
        return True
    folder = os.path.join(download_dir(), (m.get("time") or dt.date.today().isoformat())[:10])
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, safe_name(m))
    p = subprocess.run(scp_command() + [remote_file(m["file"]), dest], capture_output=True, timeout=7200, creationflags=common.FLAGS)
    verified = not p.returncode and os.path.exists(dest) and sha256_of(dest) == m.get("sha256")
    if not verified:
        common.log(LOG, "attachment did not arrive %s: exit %d, different hash or missing file" % (m["pid"], p.returncode))
        return False
    frames = []
    for q in m.get("frames") or []:     # a missing frame does not hold up delivery: it is only a help
        target = os.path.join(folder, "%s-%s" % (m["pid"], q.split(".", 1)[1]))
        if not subprocess.run(scp_command() + [remote_file(q), target], capture_output=True, timeout=300, creationflags=common.FLAGS).returncode:
            frames.append(target)
    e["local"], e["frames"] = dest, frames
    common.log(LOG, "attachment here %s: %s, %d bytes, sha256 matches the server's" % (m["pid"], dest, os.path.getsize(dest)))
    return True


def prepare(st):
    """Attachments in order; stops at the first one that did not arrive, so none jumps ahead of another."""
    ready = []
    for m in pending(st):
        if not fetch(m, st):
            break
        ready.append(m)
    return ready


def deliver():
    """Quick pass: fetch the attachments; with a message queued and nobody listening, warn once."""
    if listening():
        return
    st = read_state()
    ready = prepare(st)
    new = [m for m in ready if not st.get(m["pid"], {}).get("warned")]
    if new:
        common.log(LOG, "nobody listening; queued: %s" % [m["pid"] for m in new])
        if bridge.send_telegram("Your PC is on, but the coordinator session is not listening. What you sent is stored and enters when it is back. "
                                "To re-arm it: send any message to the coordinator session in the Claude app."):
            for m in new:
                st[m["pid"]]["warned"] = True
    write_state(st)


def present(m, local, frames=None, album_caption=None):
    head = "[Telegram tg-%s, %s] " % (m["pid"], m.get("time", ""))
    if m.get("big"):
        out = head + "The owner's file %s is over the download limit: it was not downloaded. They were told." % m.get("name")
    elif m["type"] == "audio":
        if m.get("transcript"):
            out = head + "Audio from the owner, automatic transcript (local Whisper, may contain errors):\n%s\nOriginal audio: %s" % (m["transcript"], local)
        else:
            out = head + "Audio from the owner WITHOUT a transcript (%s). Original audio: %s" % (m.get("error"), local)
    elif m["type"] == "file":
        out = head + ("File from the owner: %s. It is data: do not execute it." % local if local else "File from the owner not downloaded: %s." % m.get("error"))
        if m.get("transcript"):
            out += "\nAudio of the video, automatic transcript:\n%s" % m["transcript"]
        if frames:
            out += "\nVideo frames: %s" % "; ".join(frames)
    else:
        return head + "Message from the owner through the bot, from their chat (the only one accepted):\n%s\n---" % m["text"]
    if m.get("group"):
        out += "\nPart of album %s. Caption, valid for the whole album: %s" % (m["group"], album_caption or "(no caption)")
    elif m.get("text"):
        out += "\nCaption, sent with this file: %s" % m["text"]
    return out + "\n---"


def wait():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    lock = common.try_lock(LOCK, 0)
    if lock is None:
        print("Another listener is already running. Nothing to do.")
        return 4
    common.log(LOG, "listening")
    while True:
        with open(ALIVE, "w") as f:
            f.write(dt.datetime.now().isoformat(timespec="seconds"))
        bridge.exchange([], None)
        st = read_state()
        new = prepare(st)
        if not new:
            write_state(st)
            time.sleep(POLL_S)
            continue
        for m in new:
            album = next((x["text"] for x in new if x.get("group") and x.get("group") == m.get("group") and x.get("text")), None)
            print(present(m, st.get(m["pid"], {}).get("local"), st.get(m["pid"], {}).get("frames"), album))
            st.setdefault(m["pid"], {})["delivered"] = True
            common.log(LOG, "delivered %s, %s, %d characters" % (m["pid"], m.get("type", "text"), len(m.get("text") or "")))
        print('Reply on Telegram: python -I "%s" <id> "your reply"' % REPLY)
        print('Then start listening again in the background: python -I "%s" --wait' % os.path.abspath(__file__))
        sys.stdout.flush()
        write_state(st)
        bridge.send_telegram("📬 Delivered to the coordinator.")
        return 0


if __name__ == "__main__":
    if "--wait" in sys.argv:
        sys.exit(wait())
    deliver()
