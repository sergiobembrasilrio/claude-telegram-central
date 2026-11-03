#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Claude Code session watcher, no AI: reads the end of the transcripts and finds stalled sessions.
Run it on a schedule (see README): `watcher.py` as the full pass (every ~15 min), `watcher.py --fast` as the quick pass (every ~3 min,
only transcripts touched in the last 30 min).

Stalled = a question or plan with no answer, a tool with no result (permission prompt or hung), a request Claude never answered,
or an API error. Questions go to the bot on the server (which does not repeat them). Other problems are sent once to Telegram directly.
The current list is written to <data>/stalled.md."""
import datetime as dt, glob, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge, common

CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
WINDOW_H = 72          # a session idle for longer than this is no longer today's problem
MIN_QUESTION = 10      # minutes; leaves time to answer at the keyboard before a notice goes out
MIN_TOOL = 20
MIN_SUBAGENT = 120
MIN_NO_REPLY = 30
MIN_ERROR = 15
RECENT_MIN = 30        # the quick pass only reads transcripts touched in this many minutes
QUESTIONS = {"AskUserQuestion", "ExitPlanMode"}
LONG_RUNNING = {"Agent", "Task", "Workflow"}


def read_tail(path, size=3000000):
    with open(path, "rb") as f:
        f.seek(0, 2)
        n = f.tell()
        f.seek(max(0, n - size))
        data = f.read()
    lines = data.decode("utf-8", "replace").splitlines()
    return lines[1:] if n > size else lines     # the first line may be cut


def parse_time(text):
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def analyze(lines, now):
    """Returns None or {key, type, since, wait, ...}. Looks only at the main conversation, not at subagents."""
    last, last_ts = None, None
    uses, done, final_msg = {}, set(), None
    context, folder = "", ""
    for line in lines:
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("type") not in ("user", "assistant") or o.get("isSidechain") or o.get("isMeta"):
            continue
        m = o.get("message")
        if not isinstance(m, dict) or not o.get("timestamp"):
            continue
        last, last_ts = o, parse_time(o["timestamp"])
        folder = o.get("cwd") or folder
        c = m.get("content")
        if o["type"] == "assistant":
            if m.get("id") != final_msg:   # a new message: the previous one's tools are no longer pending
                final_msg, uses = m.get("id"), {}
            for b in c if isinstance(c, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    uses[b.get("id")] = b
                elif isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip():
                    context = b["text"]
        else:
            has_text = False
            for b in c if isinstance(c, list) else [c]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    done.add(b.get("tool_use_id"))
                else:
                    has_text = True
            if has_text:
                uses = {}
    if not last or (now - last_ts) > dt.timedelta(hours=WINDOW_H):
        return None
    idle = (now - last_ts).total_seconds() / 60
    extra = dict(context=context, project=os.path.basename(folder.rstrip("\\/")))
    pending = [b for i, b in uses.items() if i not in done]
    for b in pending:
        if b.get("name") in QUESTIONS and idle >= MIN_QUESTION:
            q = (b.get("input") or {}).get("questions") or []
            wait = q[0].get("question") if q else "approve the plan"
            questions = [dict(question=x.get("question") or "", multiple=bool(x.get("multiSelect")),
                              options=[dict(label=o.get("label") or "", description=o.get("description") or "") for o in x.get("options") or []])
                         for x in q] or [dict(question="Approve this session's plan?", multiple=False, options=[dict(label="Approve", description=""), dict(label="Do not approve", description="")])]
            return dict(key=b["id"], type="question", since=last_ts, wait=wait, questions=questions, **extra)
    if pending:
        names = [b.get("name", "?").split("__")[-1] for b in pending]
        limit = MIN_SUBAGENT if any(n in LONG_RUNNING for n in names) else MIN_TOOL
        if idle >= limit:
            return dict(key=pending[0]["id"], type="tool", since=last_ts,
                        wait="permission or result of %s; only resolved by clicking in the app" % ", ".join(sorted(set(names))[:4]), **extra)
        return None
    m = last["message"]
    if last["type"] == "assistant":
        if last.get("isApiErrorMessage") and idle >= MIN_ERROR:
            return dict(key=last.get("uuid"), type="error", since=last_ts, wait="error: " + text_of(m.get("content"))[:160])
        return None
    if "Request interrupted" not in text_of(m.get("content")) and idle >= MIN_NO_REPLY:
        return dict(key=last.get("uuid"), type="no-reply", since=last_ts, wait="Claude did not answer the last step; open the session and tell it to continue")
    return None


def from_hook(now):
    """Questions and permissions the hook saw and the session has not resolved yet (the transcript only records them afterwards)."""
    found, active = [], set()
    for path in glob.glob(os.path.join(common.HOOK_DIR, "*.json")):
        try:
            g = json.load(open(path, encoding="utf-8"))
            lines = read_tail(g["transcript_path"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if g.get("hook_event_name") == "PreToolUse":
            resolved = any('"tool_use_id":"%s"' % g["key"] in l.replace(" ", "") for l in lines)
        else:     # a permission request has no id: the session wrote again after it, so it was resolved
            resolved = os.path.getmtime(g["transcript_path"]) > g["time"] + 5
        idle = (now.timestamp() - g["time"]) / 60
        if resolved or idle > WINDOW_H * 60:
            os.replace(path, path + ".done")
            continue
        active.add(g.get("session_id"))
        is_question = g.get("tool_name") in QUESTIONS and g.get("hook_event_name") == "PreToolUse"
        if idle < (MIN_QUESTION if is_question else MIN_TOOL):
            continue
        fake = json.dumps(dict(type="assistant", timestamp=dt.datetime.fromtimestamp(g["time"], dt.timezone.utc).isoformat(), cwd=g.get("cwd"),
                               message=dict(id="hook", content=[dict(type="tool_use", id=g["key"], name=g.get("tool_name"), input=g.get("tool_input") or {})])))
        r = analyze(lines + [fake], now)
        if not r:
            continue
        r["title"] = title_of(lines) or "session " + str(g.get("session_id"))[:8]
        r["id"] = g.get("session_id") or ""
        if not r.get("context"):
            r["context"] = last_request(lines)
        found.append(r)
    return found, active


def last_request(lines):
    for line in reversed(lines):
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("type") == "user" and not o.get("isMeta"):
            t = text_of((o.get("message") or {}).get("content"))
            if t.strip():
                return "Request to this session: " + t
    return ""


def title_of(lines):
    for line in reversed(lines):
        if '"custom-title"' in line:
            try:
                return json.loads(line).get("customTitle")
            except ValueError:
                pass
    return None


def problems(now, recent=False):
    """recent=True: only transcripts touched in the last 30 min, so the quick pass is light on the PC."""
    found = []
    limit = (RECENT_MIN * 60) if recent else (WINDOW_H * 3600)
    hooked, with_hook = from_hook(now)
    found += hooked
    for transcript in glob.glob(os.path.join(CLAUDE_DIR, "projects", "*", "*.jsonl")):
        try:
            if (now.timestamp() - os.path.getmtime(transcript)) > limit:
                continue
            lines = read_tail(transcript)
            r = analyze(lines, now)
        except OSError:
            continue
        if not r:
            continue
        session = os.path.basename(transcript)[:-6]
        if session in with_hook:     # the hook already covers this session, with more detail
            continue
        r["title"] = title_of(lines) or "session " + session[:8]
        r["id"] = session
        found.append(r)
    return found, with_hook


def line_of(p):
    return "%s: stalled since %s: %s" % (p["title"], p["since"].astimezone().strftime("%d/%m %H:%M"), " ".join(p["wait"].split())[:220])


def stalled_report(items, now):
    out = ["## Stalled sessions", "", "Checked at %s." % now.astimezone().strftime("%d/%m %H:%M"), ""]
    out += ["- **ALERT:** " + line_of(p) for p in items] or ["No stalled sessions."]
    answers = bridge.to_deliver()
    if answers:
        out += ["", "**%d answer(s) to deliver** (`answers.jsonl` in the data dir):" % len(answers)]
        out += ["- %s, %s: %s" % (r["id"], r["title"], " ".join(r["answer"].split())[:200]) for r in answers]
    return out


def run(recent=False):
    now = dt.datetime.now(dt.timezone.utc)
    items, _ = problems(now, recent)
    try:
        state = json.load(open(common.path("watcher-state.json"), encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    new_state = dict(state) if recent else {}
    questions = [p for p in items if p["type"] == "question"]
    # questions go to the bot on the server, which does not repeat them; "active" only counts in the full pass
    sent = bridge.exchange(questions, None if recent else [str(p["key"]) for p in questions])
    for p in items:
        k = str(p["key"])
        new_state[k] = state.get(k) or {"seen": now.isoformat(timespec="seconds"), "notified": False}
        if p["type"] == "question":
            new_state[k]["notified"] = k in sent
        elif not new_state[k]["notified"] and bridge.send_telegram("Session " + line_of(p)):
            new_state[k]["notified"] = True
            common.log("watcher.log", "notified: " + line_of(p)[:300])
    tmp = common.path("watcher-state.json") + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(new_state, f, ensure_ascii=False, indent=0)
    os.replace(tmp, common.path("watcher-state.json"))
    if not recent:     # the partial list must not replace the full one
        with open(common.path("stalled.md"), "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(stalled_report(items, now)) + "\n")
    return items


def main():
    items = run("--fast" in sys.argv)
    print("%d stalled" % len(items))
    for p in items:
        print("- " + line_of(p))
    try:
        import listener
        listener.deliver()     # the owner's Telegram messages for the coordinator session
    except Exception as e:     # a failure here must not stop the watcher
        common.log("watcher.log", "coordinator link failed: %s: %s" % (type(e).__name__, e))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        common.log("watcher.log", "ERROR %s: %s" % (type(e).__name__, e))
        raise
