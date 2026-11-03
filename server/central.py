#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram bot for the "central": no AI, runs on a small always-on server.

The bot, the open questions and the answer queue live here, so they keep working while your PC is off.
Cron: every minute, under flock; each process long-polls Telegram for ~10 min.
The PC sends questions and fetches answers with: central.py --bridge (JSON on stdin and stdout).
That is what pc/bridge.py calls over SSH.

Answer queue: <data>/answers.jsonl, one line per answer (id, session, answer, time).
The PC copies the undelivered ones and reports the delivered ids. With the PC off, answers are kept and Telegram says so.
Only the chat in TELEGRAM_CHAT_ID is accepted. Session text is data: it is sent unformatted and never becomes a command.
A question that asks for a password, key or ID document gets no buttons and no text here: it becomes "do it on the PC".
Messages from the owner are never deleted; a message that looks like a secret is simply not stored."""
import contextlib, datetime as dt, glob, hashlib, json, os, re, sys, time, urllib.parse, urllib.request

try:
    import fcntl
except ImportError:   # tests on non-Linux machines
    fcntl = None

BASE = os.environ.get("CENTRAL_HOME") or os.path.expanduser("~/claude-telegram-central/data")
STATE = os.path.join(BASE, "state.json")
QUEUE = os.path.join(BASE, "answers.jsonl")
DELIVERED = os.path.join(BASE, "delivered.txt")
OFFSET = os.path.join(BASE, "telegram-offset.txt")
AUDIOS = os.path.join(BASE, "audios")
LOG = os.path.join(BASE, "central.log")
LOCKFILE = os.path.join(BASE, "state.lock")
ALIVE = os.path.join(BASE, "alive.txt")
PC_SEEN = os.path.join(BASE, "pc-seen.txt")
JOBS = os.path.join(BASE, "jobs.jsonl")
JOBS_SEEN = os.path.join(BASE, "jobs-seen.txt")
INBOX = os.path.join(BASE, "inbox.jsonl")        # free messages from the owner for the coordinator session
RECEIVED = os.path.join(BASE, "received")        # attachments until the PC checks them; <id>.json says what receive_files.py did
CONFIG = os.environ.get("CENTRAL_CONFIG") or os.path.expanduser("~/.config/claude-telegram-central/telegram.env")
JOBS_FILE = os.environ.get("CENTRAL_JOBS_FILE") or os.path.join(os.path.dirname(CONFIG), "jobs.json")
LIFETIME_S = 600
PC_ABSENT_S = 600    # the PC talks every ~3 min; older than this means it is off
ALBUM_S = 20         # album parts arrive as separate messages within a few seconds

# Words that mean "this question is about a secret". Portuguese terms kept on purpose.
ASKS_SECRET = re.compile(r"password|passcode|passphrase|\btoken\b|api[ -]?key|\bsecret\b|verification code|security code|\b2fa\b|\bcvv\b|\bssn\b|passport|card number"
                         r"|senha|chave (de |da )?api|c[oó]digo de (verifica|seguran)|\bcpf\b|passaporte|n[uú]mero do cart", re.I)
# Character classes keep literal key prefixes out of this source file, so secret scanners do not flag it.
SECRET_PATTERN = re.compile(r"s[k]-[A-Za-z0-9_-]{10,}|gh[p]_[A-Za-z0-9]{10,}|AI[z]a[A-Za-z0-9_-]{10,}|(?:bot)?\d{6,}:[A-Za-z0-9_-]{30,}")
LINK = re.compile(r"(?:https?://|www\.)\S+|\b[\w-]+(?:\.[\w-]+)+/\S*", re.I)
LONG_NUMBER = re.compile(r"\b\d{13,19}\b")


def luhn(n):
    s = 0
    for i, d in enumerate(reversed(n)):
        d = int(d) * (2 if i % 2 else 1)
        s += d - 9 if d > 9 else d
    return s % 10 == 0


def looks_secret(text):
    """Pasted key or card number. Links (post ids, for example) do not count; a long number counts only if it passes Luhn."""
    no_links = LINK.sub(" ", text or "")
    return bool(SECRET_PATTERN.search(no_links)) or any(luhn(n) for n in LONG_NUMBER.findall(no_links))


def load_conf():
    try:
        return dict(l.strip().split("=", 1) for l in open(CONFIG, encoding="utf-8") if "=" in l and not l.lstrip().startswith("#"))
    except OSError:
        return {}


def api_address():
    """TELEGRAM_API in the config points at a local Bot API server, when you run one."""
    return os.environ.get("CENTRAL_TELEGRAM_API") or load_conf().get("TELEGRAM_API") or "https://api.telegram.org"


API = api_address()
LOCAL = API != "https://api.telegram.org"
DOWNLOAD_LIMIT = (4000 if LOCAL else 20) * 1024 * 1024     # the official API downloads 20 MB; a local server, up to Telegram's own limit


def load_jobs():
    """Closed list for /job: {"key": ["job-id", "Label"]} in jobs.json. Empty if the file is missing. The PC runs them, not this server."""
    try:
        return {k: tuple(v) for k, v in json.load(open(JOBS_FILE, encoding="utf-8")).items()}
    except (OSError, ValueError, TypeError):
        return {}


def log(text):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write("%s\t%s\n" % (dt.datetime.now().isoformat(timespec="seconds"), text))


def conf():
    c = load_conf()
    if "TELEGRAM_TOKEN" not in c or "TELEGRAM_CHAT_ID" not in c:
        raise RuntimeError("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set in %s" % CONFIG)
    return c


def tg(method, http_timeout=30, **data):
    """Call the Telegram API. Errors never log the URL, which carries the token."""
    body = urllib.parse.urlencode({k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in data.items()}).encode()
    url = "%s/bot%s/%s" % (API, conf()["TELEGRAM_TOKEN"], method)
    with urllib.request.urlopen(url, data=body, timeout=http_timeout) as r:
        return json.load(r).get("result")


def tg_safe(method, **data):
    try:
        return tg(method, **data)
    except Exception as e:
        log("%s failed: %s" % (method, type(e).__name__))
        return None


@contextlib.contextmanager
def lock():
    """The bot and the bridge touch the same state: one at a time."""
    if not fcntl:
        yield
        return
    with open(LOCKFILE, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def pc_online():
    try:
        return time.time() - os.path.getmtime(PC_SEEN) < PC_ABSENT_S
    except OSError:
        return False


def delivery_notice():
    if pc_online():
        return "Still to be handed to the session."
    return "The PC is off: the answer is kept here and delivered when it is back."


def read_state():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except (OSError, ValueError):
        return {"questions": {}, "other": None}


def write_state(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=0)
    os.replace(tmp, STATE)


def id_of(key, n):
    """Same question, same id: this is what prevents repeats."""
    return hashlib.sha1(("%s:%s" % (key, n)).encode()).hexdigest()[:8]


def clip(text, n):
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[:n - 1] + "…"


def header(p, q_id, total, n):
    return ["❓ Claude Code question  ·  id %s" % q_id,
            "Project: %s" % clip(p.get("project"), 60),
            "Session: %s" % clip(p.get("title"), 80),
            "Waiting since: %s" % dt.datetime.fromisoformat(p["since"]).astimezone().strftime("%d/%m %H:%M"),
            "Context: %s" % (clip(p.get("context"), 300) or "(the session wrote nothing before asking)"),
            "",
            "Question%s:" % (" %d of %d" % (n + 1, total) if total > 1 else "")]


def send_problem(p, st):
    """Send the not-yet-sent questions of this problem. True if everything is sent."""
    all_sent = True
    questions = p.get("questions") or []
    for n, q in enumerate(questions):
        q_id = id_of(p["key"], n)
        if q_id in st["questions"]:
            continue
        options = q.get("options") or []
        joined = " ".join([q["question"]] + [o["label"] for o in options])
        sensitive = bool(ASKS_SECRET.search(joined))
        extra = {}
        if sensitive:     # neither the text nor the context stays on the server
            q, options = dict(q, question="(text on the PC)"), []
            p = dict(p, context="(on the PC)")
            lines = header(p, q_id, len(questions), n) + ["🔒 This asks for a password, key or document. Do it on the PC, never here."]
        else:
            lines = header(p, q_id, len(questions), n) + [clip(q["question"], 900)]
            for o in options:
                if o.get("description"):
                    lines.append("• %s: %s" % (clip(o["label"], 60), clip(o["description"], 140)))
            if q.get("multiple"):
                lines += ["", "More than one allowed: for several, use Other and write them."]
            keys = [[{"text": clip(o["label"], 60), "callback_data": "r:%s:%d" % (q_id, i)}] for i, o in enumerate(options)]
            keys.append([{"text": "Other (text or audio)", "callback_data": "o:%s" % q_id}])
            extra["reply_markup"] = {"inline_keyboard": keys}
        try:
            m = tg("sendMessage", chat_id=conf()["TELEGRAM_CHAT_ID"], text="\n".join(lines)[:4000], **extra)
        except Exception as e:
            log("send failed: %s" % type(e).__name__)
            all_sent = False
            continue
        st["questions"][q_id] = {"key": str(p["key"]), "session": p.get("id") or "", "title": p.get("title"), "project": p.get("project"),
                                 "question": q["question"], "options": [o["label"] for o in options], "msg": m["message_id"],
                                 "state": "on-pc" if sensitive else "open", "sent": dt.datetime.now().isoformat(timespec="seconds")}
        log("sent %s: %s" % (q_id, clip(p.get("title"), 60)))
    return all_sent


def close_gone(active, st):
    """Open question the session no longer waits for (answered on the PC): remove the buttons. Only with the full watcher."""
    for q_id, q in st["questions"].items():
        if q["state"] == "open" and q["key"] not in active:
            q["state"] = "closed"
            if st.get("other") == q_id:
                st["other"] = None
            tg_safe("editMessageReplyMarkup", chat_id=conf()["TELEGRAM_CHAT_ID"], message_id=q["msg"], reply_markup={"inline_keyboard": []})
            tg_safe("sendMessage", chat_id=conf()["TELEGRAM_CHAT_ID"], reply_to_message_id=q["msg"], text="That question (%s) is no longer waiting. Nothing to do." % q_id)


def save_answer(q_id, q, answer, kind, st):
    line = {"id": q_id, "session": q["session"], "title": q["title"], "question": q["question"], "answer": answer, "type": kind,
            "time": dt.datetime.now().isoformat(timespec="seconds")}
    with open(QUEUE, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
    q["state"] = "answered"
    if st.get("other") == q_id:
        st["other"] = None
    log("answer %s (%s)" % (q_id, kind))


def handle_click(c, st, owner):
    if str((c.get("message") or {}).get("chat", {}).get("id")) != owner or str((c.get("from") or {}).get("id")) != owner:
        log("refused: click from another chat")
        tg_safe("answerCallbackQuery", callback_query_id=c.get("id"), text="Not authorized.")
        return
    parts = str(c.get("data") or "").split(":")
    if parts[0] == "job":
        text = request_job(parts[1] if len(parts) == 2 else "", owner)
        tg_safe("answerCallbackQuery", callback_query_id=c.get("id"), text=text[:190])
        return
    q = st["questions"].get(parts[1]) if len(parts) >= 2 else None
    if not q or q["state"] != "open":
        tg_safe("answerCallbackQuery", callback_query_id=c.get("id"), text="That question is already closed.")
        return
    q_id = parts[1]
    if parts[0] == "o":
        st["other"] = q_id
        tg_safe("answerCallbackQuery", callback_query_id=c.get("id"))
        tg_safe("sendMessage", chat_id=owner, reply_to_message_id=q["msg"], text="Write the answer to question %s. The next text you send counts for it." % q_id)
        return
    try:
        choice = q["options"][int(parts[2])]
    except (IndexError, ValueError):
        tg_safe("answerCallbackQuery", callback_query_id=c.get("id"), text="Invalid option.")
        return
    save_answer(q_id, q, choice, "button", st)
    tg_safe("answerCallbackQuery", callback_query_id=c.get("id"), text="Saved.")
    tg_safe("editMessageReplyMarkup", chat_id=owner, message_id=q["msg"], reply_markup={"inline_keyboard": []})
    tg_safe("sendMessage", chat_id=owner, reply_to_message_id=q["msg"], text="✅ Saved (%s): %s\n%s" % (q_id, clip(choice, 200), delivery_notice()))


def request_job(key, owner):
    """Only an exact name from the list becomes a request. Returns the text that was sent."""
    jobs = load_jobs()
    if key not in jobs:
        log("refused: job not in the list: %r" % key[:40])
        text = "That one is not in the list. Send /job to see the options."
    else:
        job_id, label = jobs[key]
        pid = hashlib.sha1(("%s:%s" % (key, time.time())).encode()).hexdigest()[:10]
        with open(JOBS, "a", encoding="utf-8") as f:
            f.write(json.dumps({"pid": pid, "job": job_id, "time": dt.datetime.now().isoformat(timespec="seconds")}) + "\n")
        log("job request %s: %s" % (pid, job_id))
        text = "✅ Requested: %s. %s" % (label, "The PC picks it up within 3 min." if pc_online() else "The PC is off: it runs when the PC is back.")
    tg_safe("sendMessage", chat_id=owner, text=text)
    return text


def media_of(m):
    """(type, file_id, size, name) of the message attachment, or None."""
    for field, kind in (("voice", "audio"), ("audio", "audio"), ("video_note", "file"), ("video", "file"), ("animation", "file"), ("document", "file"), ("sticker", "file")):
        x = m.get(field)
        if x:
            return kind, x["file_id"], x.get("file_size") or 0, x.get("file_name") or field
    if m.get("photo"):
        x = m["photo"][-1]
        return "file", x["file_id"], x.get("file_size") or 0, "photo.jpg"
    return None


def store_inbox(m, st, owner):
    """Everything the owner sends outside a question goes, in order, to the coordinator queue. Nothing here executes: the session reads it.
    Downloading and transcribing is receive_files.py's job, so the bot never stalls."""
    text = (m.get("text") or m.get("caption") or "").strip()
    media = media_of(m)
    if not text and not media:
        tg_safe("sendMessage", chat_id=owner, text="I cannot store that kind of message. Send text, audio, a photo or a file.")
        return
    pid = hashlib.sha1(("%s:%s" % (m.get("message_id"), m.get("date"))).encode()).hexdigest()[:10]
    line = {"pid": pid, "type": "text", "text": text, "time": dt.datetime.now().isoformat(timespec="seconds")}
    big = False
    if media:
        big = media[2] > DOWNLOAD_LIMIT
        line.update(type=media[0], file_id=media[1], size=media[2], name=media[3], big=big, group=m.get("media_group_id"))
    with open(INBOX, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
    log("message to the coordinator %s, %s, %d characters" % (pid, line["type"], len(text)))
    notice = "📨 Got it. It reaches the coordinator within 3 min." if pc_online() else "💻 The PC is not responding: queued, delivered when it is back."
    if big:
        notice = "⚠️ That file is over %d MB, the current limit: I could not download it. The coordinator is told. Send it another way." % (DOWNLOAD_LIMIT // 1048576)
    elif media and media[0] == "audio":
        notice = "🎙️ Got the audio. I transcribe it here on the server. " + ("Passing it on next." if pc_online() else "The PC is not responding: queued until it is back.")
    open_ones = [i for i, x in st["questions"].items() if x["state"] == "open"]
    if open_ones:
        notice += "\nTo answer question %s, tap Other under it." % open_ones[-1]
    tg_safe("sendMessage", chat_id=owner, reply_to_message_id=m.get("message_id"), allow_sending_without_reply="true", text=notice)


def now_s():
    return time.time()


def read_inbox():
    try:
        return [json.loads(l) for l in open(INBOX, encoding="utf-8") if l.strip()]
    except OSError:
        return []


def inbox_ready(seen):
    """Queue for the PC, in order: stops at the first message still being downloaded or transcribed."""
    ready = []
    lines = read_inbox()
    for l in lines:
        if l.get("group") and any(x.get("group") == l["group"] and now_s() - dt.datetime.fromisoformat(x["time"]).timestamp() < ALBUM_S for x in lines):
            break     # the album may still be arriving: it goes whole, not mixed with another send
        if l.get("type", "text") != "text" and not l.get("big"):
            try:
                l.update(json.load(open(os.path.join(RECEIVED, l["pid"] + ".json"), encoding="utf-8")))
            except (OSError, ValueError):
                break
        if l["pid"] not in seen:
            ready.append(l)
    return ready


def delete_delivered(pids):
    """The PC delivered and checked the file: it leaves the server."""
    lines = read_inbox()
    pids = [p for p in pids if any(l["pid"] == p for l in lines) or glob.glob(os.path.join(RECEIVED, p + ".*"))]
    if not pids:
        return
    rest = [l for l in lines if l["pid"] not in pids]
    tmp = INBOX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("".join(json.dumps(l, ensure_ascii=False) + "\n" for l in rest))
    os.replace(tmp, INBOX)
    for pid in pids:
        for f in glob.glob(os.path.join(RECEIVED, pid + ".*")):
            os.remove(f)
        log("deleted from the server: %s" % pid)


def queue_to_send(queue, seen_file, seen):
    """Records what the PC already has and returns the rest."""
    try:
        have = set(open(seen_file, encoding="utf-8").read().split())
    except OSError:
        have = set()
    new = [i for i in seen if i not in have]
    if new:
        with open(seen_file, "a", encoding="utf-8") as f:
            f.write("".join(i + "\n" for i in new))
    have.update(new)
    try:
        lines = [json.loads(l) for l in open(queue, encoding="utf-8") if l.strip()]
    except OSError:
        return []
    return [l for l in lines if l["pid"] not in have]


def download_audio(file_id, q_id):
    info = tg("getFile", file_id=file_id)
    url = "%s/file/bot%s/%s" % (API, conf()["TELEGRAM_TOKEN"], info["file_path"])
    os.makedirs(AUDIOS, exist_ok=True)
    dest = os.path.join(AUDIOS, "%s%s" % (q_id, os.path.splitext(info["file_path"])[1] or ".ogg"))
    with urllib.request.urlopen(url, timeout=60) as r, open(dest, "wb") as f:
        f.write(r.read())
    return dest


def handle_message(m, st, owner):
    chat = str((m.get("chat") or {}).get("id"))
    if chat != owner or str((m.get("from") or {}).get("id")) != owner:
        log("refused: message from another chat")
        tg_safe("sendMessage", chat_id=chat, text="This bot only serves its owner.")
        return
    command = (m.get("text") or "").strip()
    if command.lower() == "/job":
        keys = [[{"text": label, "callback_data": "job:%s" % k}] for k, (_, label) in load_jobs().items()]
        tg_safe("sendMessage", chat_id=owner, text="Which job should run?" if keys else "No jobs configured (see jobs.example.json).", reply_markup={"inline_keyboard": keys})
        return
    if command.lower().startswith("/job"):
        parts = command.split()
        request_job(parts[1].lower() if len(parts) == 2 and parts[0].lower() == "/job" else "", owner)
        return
    if looks_secret(m.get("text") or m.get("caption") or ""):
        # The owner's messages are never deleted; this one is just not stored.
        tg_safe("sendMessage", chat_id=owner, text="🔒 That looks like a key or a card number, so I did not store it. Your message is still in the chat; delete it yourself if you want.")
        log("refused: text that looks like a secret")
        return
    q_id = st.get("other")
    q = st["questions"].get(q_id) if q_id else None
    if not q or q["state"] != "open":
        store_inbox(m, st, owner)
        return
    voice = m.get("voice") or m.get("audio")
    if voice:
        try:
            path = download_audio(voice["file_id"], q_id)
        except Exception as e:
            log("audio failed: %s" % type(e).__name__)
            tg_safe("sendMessage", chat_id=owner, text="I could not download the audio. Send it again or write.")
            return
        save_answer(q_id, q, "[audio, not transcribed: %s]" % path, "audio", st)
    else:
        text = m.get("text") or m.get("caption") or ""
        if not text.strip():
            tg_safe("sendMessage", chat_id=owner, text="Send text or audio.")
            return
        save_answer(q_id, q, text.strip(), "text", st)
    tg_safe("editMessageReplyMarkup", chat_id=owner, message_id=q["msg"], reply_markup={"inline_keyboard": []})
    tg_safe("sendMessage", chat_id=owner, reply_to_message_id=q["msg"], text="✅ Saved (%s). %s" % (q_id, delivery_notice()))


def handle_update(u, st, owner):
    if u.get("callback_query"):
        handle_click(u["callback_query"], st, owner)
    elif u.get("message"):
        handle_message(u["message"], st, owner)


def to_deliver():
    """Answers in the queue that the coordinator has not yet marked in delivered.txt."""
    try:
        done = set(open(DELIVERED, encoding="utf-8").read().split())
    except OSError:
        done = set()
    try:
        lines = [json.loads(l) for l in open(QUEUE, encoding="utf-8") if l.strip()]
    except OSError:
        return []
    return [l for l in lines if l["id"] not in done]


def listen(seconds):
    """Read Telegram for up to this many seconds, by long poll."""
    owner = conf()["TELEGRAM_CHAT_ID"]
    end = time.time() + seconds
    while time.time() < end:
        try:
            offset = int(open(OFFSET).read())
        except (OSError, ValueError):
            offset = 0
        wait = max(1, min(25, int(end - time.time())))
        try:
            new = tg("getUpdates", http_timeout=wait + 15, offset=offset, timeout=wait, allowed_updates=["message", "callback_query"])
        except Exception as e:
            log("getUpdates failed: %s" % type(e).__name__)
            time.sleep(10)
            continue
        with open(ALIVE, "w") as f:
            f.write(dt.datetime.now().isoformat(timespec="seconds"))
        for u in new or []:
            with lock():
                st = read_state()
                try:
                    handle_update(u, st, owner)
                except Exception as e:   # one bad message must not stall the queue
                    log("update %s failed: %s" % (u.get("update_id"), type(e).__name__))
                write_state(st)
            with open(OFFSET, "w") as f:
                f.write(str(u["update_id"] + 1))


def bridge(request):
    """Talks to the PC. In: {problems, active (or null), delivered, jobs_seen, inbox_seen, inbox_delivered}.
    Out: {answers: undelivered, sent: keys already on Telegram, jobs, inbox}."""
    owner = conf()["TELEGRAM_CHAT_ID"]
    was_online = pc_online()
    with open(PC_SEEN, "w") as f:
        f.write(dt.datetime.now().isoformat(timespec="seconds"))
    with lock():
        st = read_state()
        for p in request.get("problems") or []:
            send_problem(p, st)
        if request.get("active") is not None:
            close_gone(set(request["active"]), st)
        try:
            done = set(open(DELIVERED, encoding="utf-8").read().split())
        except OSError:
            done = set()
        new = [i for i in request.get("delivered") or [] if i not in done]
        if new:
            with open(DELIVERED, "a", encoding="utf-8") as f:
                f.write("".join(i + "\n" for i in new))
        for i in new:
            q = st["questions"].get(i)
            if q:
                tg_safe("sendMessage", chat_id=owner, reply_to_message_id=q["msg"], text="📬 Delivered to the session (%s)." % i)
        write_state(st)
        pending = to_deliver()
        jobs = queue_to_send(JOBS, JOBS_SEEN, request.get("jobs_seen") or [])
        inbox = inbox_ready(set(request.get("inbox_seen") or []))
        delete_delivered(request.get("inbox_delivered") or [])
    if pending and not was_online:
        tg_safe("sendMessage", chat_id=owner, text="💻 The PC is back. %d stored answer(s) ready for delivery." % len(pending))
    if inbox and not was_online:
        tg_safe("sendMessage", chat_id=owner, text="💻 The PC is back. Passing %d queued message(s) to the coordinator." % len(inbox))
    return {"answers": pending, "sent": sorted({q["key"] for q in st["questions"].values()}), "jobs": jobs, "inbox": inbox}


def main():
    os.makedirs(BASE, exist_ok=True)
    if "--send" in sys.argv:     # the PC sends notices through here when a local Telegram Bot API server is on
        tg("sendMessage", chat_id=conf()["TELEGRAM_CHAT_ID"], text=sys.stdin.read()[:4000])
        return 0
    if "--bridge" in sys.argv:
        json.dump(bridge(json.load(sys.stdin)), sys.stdout, ensure_ascii=False)
        return 0
    if os.path.exists(os.path.join(BASE, "paused")):     # Bot API server switch in progress
        return 0
    listen(LIFETIME_S)
    return 0


if __name__ == "__main__":
    sys.exit(main())
