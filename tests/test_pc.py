# -*- coding: utf-8 -*-
"""Offline tests for the PC side (hook, watcher, bridge, listener). SSH and Telegram are faked. Usage: python -I tests/test_pc.py"""
import datetime as dt, json, os, subprocess, sys, tempfile, types

T = tempfile.mkdtemp()
os.environ["CENTRAL_DATA_DIR"] = os.path.join(T, "data")
os.environ["CENTRAL_PC_CONFIG"] = os.path.join(T, "no-such-pc.env")
os.environ["CENTRAL_SSH_HOST"] = "user@example.invalid"
os.environ["CENTRAL_DOWNLOAD_DIR"] = os.path.join(T, "downloads")
PC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pc")
sys.path.insert(0, PC)
import answers, bridge, common, listener, reply_telegram, watcher

errors = 0


def check(name, ok):
    global errors
    errors += not ok
    print("%s %s" % ("ok   " if ok else "ERROR", name))


NOW = dt.datetime.now(dt.timezone.utc)


def entry(kind, minutes_ago, content, **k):
    ts = (NOW - dt.timedelta(minutes=minutes_ago)).isoformat()
    return json.dumps(dict(type=kind, timestamp=ts, cwd="/work/demo", message=dict(id=k.pop("mid", kind + str(minutes_ago)), content=content), **k))


ASK = [dict(type="tool_use", id="toolu_q", name="AskUserQuestion",
            input=dict(questions=[dict(question="Which database?", multiSelect=False, options=[dict(label="SQLite", description="simple"), dict(label="Postgres", description="")])]))]
lines = [entry("user", 40, "set it up"), entry("assistant", 30, [dict(type="text", text="I need a choice.")] + ASK, mid="m1")]
r = watcher.analyze(lines, NOW)
check("unanswered question for 30 min is found, with options",
      r and r["type"] == "question" and r["key"] == "toolu_q" and [o["label"] for o in r["questions"][0]["options"]] == ["SQLite", "Postgres"] and r["project"] == "demo")
check("question idle for 3 min is not yet a problem", watcher.analyze([entry("user", 5, "go"), entry("assistant", 3, ASK, mid="m1")], NOW) is None)
answered = lines + [entry("user", 20, [dict(type="tool_result", tool_use_id="toolu_q", content="SQLite")])]
check("answered question is not a problem", watcher.analyze(answered, NOW) is None)
tool = [entry("user", 40, "run it"), entry("assistant", 25, [dict(type="tool_use", id="toolu_b", name="Bash", input={})], mid="m2")]
check("tool without result for 25 min is a problem", (watcher.analyze(tool, NOW) or {}).get("type") == "tool")
check("a session idle for days is ignored", watcher.analyze([entry("user", 60 * 24 * 10, "old")], NOW) is None)

# hook: writes a pending record and says nothing
payload = json.dumps(dict(hook_event_name="PreToolUse", tool_name="AskUserQuestion", tool_use_id="toolu_h", session_id="sess-1", transcript_path=os.path.join(T, "t.jsonl"),
                          cwd="/work/demo", tool_input=dict(questions=[dict(question="Deploy now?", options=[dict(label="Yes"), dict(label="No")])])))
p = subprocess.run([sys.executable, "-I", os.path.join(PC, "central_hook.py")], input=payload.encode(), capture_output=True, env=dict(os.environ))
pending = os.path.join(os.environ["CENTRAL_DATA_DIR"], "hook-pending", "toolu_h.json")
check("hook writes the pending request and prints nothing", p.returncode == 0 and not p.stdout and os.path.exists(pending))
p = subprocess.run([sys.executable, "-I", os.path.join(PC, "central_hook.py")], input=b"not json", capture_output=True, env=dict(os.environ))
check("hook never fails the session, even on garbage input", p.returncode == 0 and not p.stdout)

# watcher + hook: the transcript has no question yet, the hook record gives it away
with open(os.path.join(T, "t.jsonl"), "w", encoding="utf-8") as f:
    f.write(entry("user", 60, "deploy the site") + "\n")
rec = json.load(open(pending))
rec["time"] = (NOW - dt.timedelta(minutes=30)).timestamp()
json.dump(rec, open(pending, "w"))
found, active = watcher.from_hook(NOW)
check("hook record becomes a question with options", len(found) == 1 and found[0]["key"] == "toolu_h" and [o["label"] for o in found[0]["questions"][0]["options"]] == ["Yes", "No"] and "sess-1" in active)

# bridge: fake SSH
calls = []


def fake_run(cmd, input=None, **kw):
    calls.append((cmd, json.loads(input) if input and cmd[-1].endswith("--bridge") else input))
    out = {"answers": [dict(id="a1", session="s", title="t", question="q", answer="Yes", type="button", time="x")], "sent": ["toolu_h"],
           "jobs": [dict(pid="j1", job="nightly-backup")], "inbox": [dict(pid="m1", type="text", text="hi", time="2026-01-02T10:00:00")]}
    return types.SimpleNamespace(returncode=0, stdout=json.dumps(out).encode(), stderr=b"")


bridge.subprocess.run = fake_run
sent_keys = bridge.exchange(found, [str(found[0]["key"])])
req = calls[0][1]
check("bridge sends the question package", req["problems"][0]["key"] == "toolu_h" and req["problems"][0]["since"] and req["active"] == ["toolu_h"])
check("bridge goes over ssh to the configured host, in batch mode", calls[0][0][0] == "ssh" and "BatchMode=yes" in calls[0][0] and "user@example.invalid" in calls[0][0])
check("bridge stores answers, jobs and inbox", sent_keys == {"toolu_h"} and [a["id"] for a in bridge.to_deliver()] == ["a1"] and bridge.pids_of(bridge.JOBS) == {"j1"} and bridge.pids_of(bridge.INBOX) == {"m1"})
answers.main(["answers.py", "ack", "a1"])
check("ack marks the answer delivered", bridge.to_deliver() == [])
bridge.exchange([], None)
check("next round reports delivered ids and what it already has", calls[1][1]["delivered"] == ["a1"] and calls[1][1]["jobs_seen"] == ["j1"] and calls[1][1]["inbox_seen"] == ["m1"])


def broken(cmd, input=None, **kw):
    return types.SimpleNamespace(returncode=255, stdout=b"", stderr=b"connection refused")


bridge.subprocess.run = broken
check("server down: bridge returns empty and does not raise", bridge.exchange([], None) == set())

# reply: secrets never leave
outbox = []
bridge.send_telegram = lambda text: outbox.append(text) or True
check("reply with a normal text is sent", reply_telegram.main(["r", "tg-m1", "On it."]) == 0 and outbox == ["💬 On it."])
check("reply that looks like a key is refused", reply_telegram.main(["r", "tg-m1", "use s" + "k-abcdefghijklmnop"]) == 3 and len(outbox) == 1)
check("reply with a valid card number is refused", reply_telegram.main(["r", "tg-m1", "4111111111111111"]) == 3)

# listener: presentation is data, not commands
m = dict(pid="p1", type="audio", time="2026-01-02T10:00:00", transcript="please check the build")
check("audio is presented with transcript and original path", "please check the build" in listener.present(m, "/x/a.ogg") and "/x/a.ogg" in listener.present(m, "/x/a.ogg"))
v = dict(pid="p2", type="file", time="t", transcript="narration")
out = listener.present(v, "/x/v.mp4", ["/x/f1.jpg"])
check("video shows transcript and frames, and says do not execute", "narration" in out and "/x/f1.jpg" in out and "do not execute" in out)
check("attachment name is sanitised", listener.safe_name(dict(pid="p3", name="../../evil name?.mp4")) == "p3-evil_name_.mp4")
check("text message is framed as coming from the owner chat", "only one accepted" in listener.present(dict(pid="p4", type="text", text="hello", time="t"), None))

print("errors:", errors)
sys.exit(1 if errors else 0)
