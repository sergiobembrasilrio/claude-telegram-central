# -*- coding: utf-8 -*-
"""Offline tests for the server bot with Telegram faked (nothing leaves the machine). Usage: python -I tests/test_central.py"""
import json, os, sys, tempfile, time

T = tempfile.mkdtemp()
os.environ["CENTRAL_HOME"] = T
os.environ["CENTRAL_CONFIG"] = os.path.join(T, "no-such-telegram.env")
os.environ["CENTRAL_JOBS_FILE"] = os.path.join(T, "jobs.json")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
import central as C
import receive_files as R

OWNER = "111"
STRANGER = "999"
C.conf = lambda: {"TELEGRAM_CHAT_ID": OWNER, "TELEGRAM_TOKEN": "x"}
sent = []


def fake_tg(method, http_timeout=30, **d):
    sent.append((method, d))
    return {"message_id": len(sent)}


C.tg = fake_tg
SINCE = "2026-01-02T11:27:00+00:00"
PROB = dict(key="toolu_1", id="session_x", title="Test session", project="folder", since=SINCE, context="I will send 6 requests.",
            questions=[dict(question="Can I send them?", multiple=False, options=[dict(label="Yes", description="sends"), dict(label="No", description="")])])
errors = 0


def check(name, ok):
    global errors
    errors += not ok
    print("%s %s" % ("ok   " if ok else "ERROR", name))


def queue():
    return C.to_deliver()


def click(data, who=OWNER):
    return {"update_id": 1, "callback_query": {"id": "c", "data": data, "from": {"id": int(who)}, "message": {"chat": {"id": int(who)}}}}


def msg(who=OWNER, **k):
    return {"update_id": 2, "message": dict({"message_id": 5, "date": int(time.time()), "from": {"id": int(who)}, "chat": {"id": int(who)}}, **k)}


def last_text():
    return sent[-1][1].get("text", "")


st = C.read_state()
C.send_problem(PROB, st)
q = C.id_of("toolu_1", 0)
text = sent[0][1]["text"]
keys = sent[0][1]["reply_markup"]["inline_keyboard"]
check("message carries project, session, context and question", all(x in text for x in ("folder", "Test session", "6 requests", "Can I send them?")))
check("buttons: 2 options + Other", [t[0]["text"] for t in keys] == ["Yes", "No", "Other (text or audio)"])
n = len(sent)
C.send_problem(PROB, st)
check("same question is not repeated", len(sent) == n)

C.handle_update(click("r:%s:0" % q, STRANGER), st, OWNER)
check("click from another chat refused", not queue() and st["questions"][q]["state"] == "open")
C.handle_update(msg(STRANGER, text="yes"), st, OWNER)
check("text from another chat refused", not queue())
st["other"] = q
C.handle_update(msg(STRANGER, text="yes"), st, OWNER)
check("another chat cannot answer even with Other open", not queue())
check("a stranger gets a refusal, not data", "only serves its owner" in last_text())
st["other"] = None

C.handle_update(click("r:%s:0" % q), st, OWNER)
check("button writes the queue", [(r["id"], r["session"], r["answer"]) for r in queue()] == [(q, "session_x", "Yes")] and "time" in queue()[0])
C.handle_update(click("r:%s:1" % q), st, OWNER)
check("second click does not write", len(queue()) == 1)

C.send_problem(dict(PROB, key="toolu_2"), st)
q2 = C.id_of("toolu_2", 0)
C.handle_update(msg(text="loose text"), st, OWNER)
check("text without Other does not answer", len(queue()) == 1)
C.handle_update(click("o:%s" % q2), st, OWNER)
fake_key = "s" + "k-ant-abcdefghijklmnop"
C.handle_update(msg(text=fake_key), st, OWNER)
check("pasted key is not stored and not deleted", len(queue()) == 1 and not any(m == "deleteMessage" for m, _ in sent))
check("link with a long id does not look like a secret", not C.looks_secret("https://example.com/user/status/2117190183120773243"))
check("valid card number looks like a secret", C.looks_secret("4111111111111111"))
check("long number that fails Luhn does not", not C.looks_secret("1234567890123456"))
check("bot token looks like a secret", C.looks_secret("bot" + "123456789" + ":" + "A" * 35))
C.handle_update(msg(text="Send only the first one"), st, OWNER)
check("Other with text writes the queue", queue()[-1]["answer"] == "Send only the first one" and queue()[-1]["type"] == "text")

P3 = dict(PROB, key="toolu_3", questions=[dict(question="What is the bank password?", multiple=False, options=[dict(label="Later", description="")])])
n = len(sent)
C.send_problem(P3, st)
check("password question goes without buttons", "reply_markup" not in sent[n][1] and "Do it on the PC" in sent[n][1]["text"])
check("sensitive question text is not stored", st["questions"][C.id_of("toolu_3", 0)]["question"] == "(text on the PC)")

C.send_problem(dict(PROB, key="toolu_4"), st)
q4 = C.id_of("toolu_4", 0)
C.close_gone({"toolu_9"}, st)
C.handle_update(click("r:%s:0" % q4), st, OWNER)
check("answered on the PC closes the buttons", st["questions"][q4]["state"] == "closed" and q4 not in [r["id"] for r in queue()])

C.send_problem(dict(PROB, key="toolu_5"), st)
C.handle_update(click("r:%s:0" % C.id_of("toolu_5", 0)), st, OWNER)
check("PC off: Telegram says the answer is kept", "off" in last_text())
C.write_state(st)
r = C.bridge({"problems": [], "active": None, "delivered": []})
check("PC back: Telegram says so and the stored answer comes", any("back" in d.get("text", "") for _, d in sent[-3:]) and q in [x["id"] for x in r["answers"]])
n = len(sent)
r = C.bridge({"problems": [dict(PROB, key="toolu_6")], "active": None, "delivered": [q]})
check("bridge receives a new question", "toolu_6" in r["sent"] and len(sent) > n)
check("delivered leaves the list and Telegram is told", q not in [x["id"] for x in r["answers"]] and any("Delivered" in d.get("text", "") for _, d in sent[n:]))
n = len(sent)
C.bridge({"problems": [], "active": None, "delivered": [q]})
check("delivery is not announced twice", len(sent) == n)

# jobs: only exact names from the closed list
json.dump({"backup": ["nightly-backup", "Nightly backup"]}, open(C.JOBS_FILE, "w"))
C.handle_update(msg(text="/job backup"), st, OWNER)
C.handle_update(msg(text="/job rm -rf"), st, OWNER)
jobs = [json.loads(l) for l in open(C.JOBS)]
check("job from the list is queued, anything else is refused", [j["job"] for j in jobs] == ["nightly-backup"] and "not in the list" in last_text())
n = len(jobs)
C.handle_update(click("job:backup", STRANGER), st, OWNER)
check("job click from another chat refused", len([1 for _ in open(C.JOBS)]) == n)

# free messages, audio, files (the earlier "loose text" also landed in the inbox: start clean)
if os.path.exists(C.INBOX):
    os.remove(C.INBOX)
C.handle_update(msg(message_id=20, text="remember to call the vet"), st, OWNER)
C.handle_update(msg(message_id=21, voice={"file_id": "f1", "file_size": 1000}), st, OWNER)
inbox = C.read_inbox()
check("free text and audio land in the inbox, in order", [(l["type"], l["text"]) for l in inbox] == [("text", "remember to call the vet"), ("audio", "")])
check("inbox stops at an audio still being transcribed", [l["type"] for l in C.inbox_ready(set())] == ["text"])
big = msg(message_id=22, document={"file_id": "f2", "file_size": C.DOWNLOAD_LIMIT + 1, "file_name": "huge.bin"})
C.handle_update(big, st, OWNER)
check("file over the limit is flagged, not downloaded", C.read_inbox()[-1]["big"] and "over" in last_text())

# receive_files: transcript, secret withheld, video frames, nothing deleted
os.makedirs(C.RECEIVED, exist_ok=True)


def fake_download(l):
    p = os.path.join(C.RECEIVED, l["pid"] + ".ogg")
    open(p, "wb").write(b"audio-bytes")
    return p


R.download = fake_download
R.extract_frames = lambda path, pid: [pid + ".frame1.jpg"]
R.transcribe = lambda path: ("hello from a voice note", 3)
done = R.process({"pid": "aaa", "type": "audio", "file_id": "f"})
check("audio gets a transcript and a sha256", done["transcript"] == "hello from a voice note" and len(done["sha256"]) == 64)
R.transcribe = lambda path: ("my key is " + fake_key, 3)
done = R.process({"pid": "bbb", "type": "audio", "file_id": "f"})
check("secret in a transcript is withheld, file kept", "transcript" not in done and "withheld" in done["error"] and os.path.exists(os.path.join(C.RECEIVED, "bbb.ogg")))
R.download = lambda l: (open(os.path.join(C.RECEIVED, l["pid"] + ".mp4"), "wb").write(b"v"), os.path.join(C.RECEIVED, l["pid"] + ".mp4"))[1]
R.transcribe = lambda path: ("video narration", 5)
done = R.process({"pid": "ccc", "type": "file", "file_id": "f"})
check("video gets frames and a transcript", done["frames"] == ["ccc.frame1.jpg"] and done["transcript"] == "video narration")
inbox_pid = [l["pid"] for l in C.read_inbox() if l["type"] == "audio"][0]
json.dump(done, open(os.path.join(C.RECEIVED, inbox_pid + ".json"), "w"))
check("inbox releases the audio once it is received", [l["type"] for l in C.inbox_ready(set())][:2] == ["text", "audio"])
C.delete_delivered([inbox_pid])
check("delivered attachment leaves the server", inbox_pid not in [l["pid"] for l in C.read_inbox()])

print("errors:", errors)
sys.exit(1 if errors else 0)
