#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Claude Code hook (PreToolUse for AskUserQuestion/ExitPlanMode, and PermissionRequest), no AI.
Why: a session only writes a question to its transcript after it is answered, so the watcher would not see it waiting.
This writes the request to <data>/hook-pending/ and exits silently: it never answers, never blocks and never changes the session's decision."""
import json, os, sys, time

try:
    d = json.load(sys.stdin.buffer)   # UTF-8, whatever the Windows code page
    if d.get("hook_event_name") == "PermissionRequest" and d.get("tool_name") in ("AskUserQuestion", "ExitPlanMode"):
        sys.exit(0)                   # those already arrive through PreToolUse
    data_dir = os.environ.get("CENTRAL_DATA_DIR") or os.path.expanduser("~/.claude-telegram-central")
    folder = os.path.join(data_dir, "hook-pending")
    os.makedirs(folder, exist_ok=True)
    key = d.get("tool_use_id") or "%s-%d" % (d.get("session_id"), int(time.time()))
    record = {k: d.get(k) for k in ("session_id", "transcript_path", "cwd", "tool_name", "tool_input", "hook_event_name")}
    record.update(key=key, time=time.time())
    with open(os.path.join(folder, "".join(c for c in key if c.isalnum() or c in "_-") + ".json"), "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False)
except Exception:   # the hook must never get in the session's way
    pass
sys.exit(0)
