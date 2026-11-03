# -*- coding: utf-8 -*-
"""Shared helpers for the PC side: settings, data directory, locks, secret filter.

Settings come from environment variables, falling back to the file in CENTRAL_PC_CONFIG
(default ~/.config/claude-telegram-central/pc.env, KEY=value lines). The data directory comes from the environment only
(CENTRAL_DATA_DIR), so the hook and the watcher always agree on it."""
import datetime as dt, os, re, subprocess, time

DATA_DIR = os.environ.get("CENTRAL_DATA_DIR") or os.path.expanduser("~/.claude-telegram-central")
CONFIG_FILE = os.environ.get("CENTRAL_PC_CONFIG") or os.path.expanduser("~/.config/claude-telegram-central/pc.env")
HOOK_DIR = os.path.join(DATA_DIR, "hook-pending")
FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # no console window flashing on Windows

# Character classes keep literal key prefixes out of this source file, so secret scanners do not flag it.
SECRET_PATTERN = re.compile(r"s[k]-[A-Za-z0-9_-]{10,}|gh[p]_[A-Za-z0-9]{10,}|AI[z]a[A-Za-z0-9_-]{10,}|(?:bot)?\d{6,}:[A-Za-z0-9_-]{30,}")
LINK = re.compile(r"(?:https?://|www\.)\S+|\b[\w-]+(?:\.[\w-]+)+/\S*", re.I)
LONG_NUMBER = re.compile(r"\b\d{13,19}\b")


def ensure_dir(path=None):
    os.makedirs(path or DATA_DIR, exist_ok=True)
    return path or DATA_DIR


def path(name):
    """A file inside the data directory."""
    ensure_dir()
    return os.path.join(DATA_DIR, name)


def file_settings():
    try:
        return dict(l.strip().split("=", 1) for l in open(CONFIG_FILE, encoding="utf-8") if "=" in l and not l.lstrip().startswith("#"))
    except OSError:
        return {}


def setting(name, default=None):
    return os.environ.get(name) or file_settings().get(name) or default


def log(name, text):
    with open(path(name), "a", encoding="utf-8") as f:
        f.write("%s\t%s\n" % (dt.datetime.now().isoformat(timespec="seconds"), text))


def luhn(n):
    s = 0
    for i, d in enumerate(reversed(n)):
        d = int(d) * (2 if i % 2 else 1)
        s += d - 9 if d > 9 else d
    return s % 10 == 0


def looks_secret(text):
    """Pasted key or card number. Links do not count; a long number counts only if it passes Luhn."""
    no_links = LINK.sub(" ", text or "")
    return bool(SECRET_PATTERN.search(no_links)) or any(luhn(n) for n in LONG_NUMBER.findall(no_links))


def try_lock(lock_path, wait_s=0):
    """Open and lock a file; None if another process holds the lock after the wait."""
    f = open(lock_path, "a+")
    end = time.time() + wait_s
    while True:
        try:
            f.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except OSError:
            if time.time() >= end:
                f.close()
                return None
            time.sleep(0.5)
