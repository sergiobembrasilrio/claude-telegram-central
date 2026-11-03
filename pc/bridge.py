# -*- coding: utf-8 -*-
"""Links the PC to the bot on the server (server/central.py --bridge) over SSH, with no AI.
Sends the stalled questions and the delivered ids; fetches undelivered answers into answers.jsonl.
The coordinator session reads answers.jsonl, hands each answer to the session and records the id in delivered.txt
(see answers.py). A server that is down never takes the watcher down.

Settings (env or pc.env): CENTRAL_SSH_HOST (user@host, required), CENTRAL_SSH_KEY (optional private key path),
CENTRAL_REMOTE_CODE (server code dir, relative to the remote home; default claude-telegram-central/server)."""
import json, os, subprocess, sys, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common

ANSWERS = common.path("answers.jsonl")
DELIVERED = common.path("delivered.txt")
JOBS = common.path("jobs-queue.jsonl")
INBOX = common.path("inbox-queue.jsonl")
INBOX_STATE = common.path("inbox-state.json")
LOG = "bridge.log"


def ssh_command():
    host = common.setting("CENTRAL_SSH_HOST")
    if not host:
        raise RuntimeError("CENTRAL_SSH_HOST is not set")
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]
    key = common.setting("CENTRAL_SSH_KEY")
    if key:
        cmd += ["-i", os.path.expanduser(key)]
    return cmd + [host]


def remote_code():
    return common.setting("CENTRAL_REMOTE_CODE", "claude-telegram-central/server")


def remote_central(flag):
    return "PYTHONIOENCODING=utf-8 python3 %s/central.py %s" % (remote_code(), flag)


def delivered_ids():
    try:
        return set(open(DELIVERED, encoding="utf-8").read().split())
    except OSError:
        return set()


def to_deliver():
    """Answers brought from the server that were not delivered yet."""
    done = delivered_ids()
    try:
        lines = [json.loads(l) for l in open(ANSWERS, encoding="utf-8") if l.strip()]
    except OSError:
        return []
    return [l for l in lines if l["id"] not in done]


def package(p):
    """Only what the bot needs; dates become text."""
    return {"key": str(p["key"]), "id": p.get("id") or "", "title": p.get("title"), "project": p.get("project"),
            "context": p.get("context"), "since": p["since"].isoformat(), "questions": p.get("questions") or []}


def pids_of(path):
    try:
        return {json.loads(l)["pid"] for l in open(path, encoding="utf-8") if l.strip()}
    except (OSError, ValueError):
        return set()


def send_via_server(text):
    """Notice to Telegram through the server. True if sent."""
    try:
        r = subprocess.run(ssh_command() + [remote_central("--send")], input=text.encode("utf-8"), capture_output=True, timeout=60, creationflags=common.FLAGS)
        return r.returncode == 0
    except Exception:
        return False


def send_telegram(text):
    """Notice to the owner's chat. Directly with the local token, or through the server when TELEGRAM_VIA_SERVER is set
    (needed when the server runs a local Bot API server: the official API refuses the token from elsewhere). True if sent."""
    if common.setting("TELEGRAM_VIA_SERVER"):
        return send_via_server(text)
    token, chat = common.setting("TELEGRAM_TOKEN"), common.setting("TELEGRAM_CHAT_ID")
    if not token or not chat:
        common.log(LOG, "no TELEGRAM_TOKEN/TELEGRAM_CHAT_ID: cannot notify")
        return False
    api = common.setting("TELEGRAM_API", "https://api.telegram.org")
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000]}).encode()
    try:
        with urllib.request.urlopen("%s/bot%s/sendMessage" % (api, token), data=data, timeout=20) as r:
            return bool(json.load(r).get("ok"))
    except Exception as e:  # the error text may carry the URL with the token: do not log it
        common.log(LOG, "Telegram failed: " + type(e).__name__)
        return False


def checked_inbox_ids():
    """Already delivered to the coordinator and the attachment checked here: the server may delete."""
    try:
        st = json.load(open(INBOX_STATE, encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return sorted(p for p, e in st.items() if e.get("delivered"))


def store_new(path, items):
    """Queue brought from the server, which only stops sending once the PC says it has it."""
    have = pids_of(path)
    new = [p for p in items if p.get("pid") not in have]
    if new:
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in new))


def exchange(questions, active):
    """Returns the set of keys already on Telegram. Without the server, returns empty and tries again next round."""
    request = {"problems": [package(p) for p in questions], "active": active, "delivered": sorted(delivered_ids()),
               "jobs_seen": sorted(pids_of(JOBS)), "inbox_seen": sorted(pids_of(INBOX)), "inbox_delivered": checked_inbox_ids()}
    try:
        r = subprocess.run(ssh_command() + [remote_central("--bridge")], input=json.dumps(request).encode("utf-8"),
                           capture_output=True, timeout=120, creationflags=common.FLAGS)
        if r.returncode:
            raise RuntimeError(r.stderr.decode("utf-8", "replace")[-200:])
        d = json.loads(r.stdout.decode("utf-8"))
    except Exception as e:
        common.log(LOG, "bridge failed: %s: %s" % (type(e).__name__, str(e)[:200]))
        return set()
    tmp = "%s.%d.tmp" % (ANSWERS, os.getpid())     # the listener and the watcher both come through here
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write("".join(json.dumps(l, ensure_ascii=False) + "\n" for l in d["answers"]))
        os.replace(tmp, ANSWERS)
    except OSError as e:     # the other process updates the same list right after
        common.log(LOG, "answers not written now: %s" % type(e).__name__)
    store_new(JOBS, d.get("jobs") or [])
    store_new(INBOX, d.get("inbox") or [])
    return set(d["sent"])
