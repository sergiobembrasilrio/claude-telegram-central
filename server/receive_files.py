#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Downloads the attachments the owner sent to the bot and transcribes audio, with no paid API and no PC. Cron: every minute, under flock.
Reads the queue written by central.py and writes received/<id>.<ext> and received/<id>.json (with sha256), which releases the message to the PC.
Disk cap in CAP: the queue waits and the bot warns once. A video gets its audio transcribed and 4 frames, if possible.
Local Whisper (see transcribe.py). Text that looks like a key is not passed on: for audio and video the file stays, only the transcript is withheld."""
import hashlib, json, os, shutil, subprocess, sys, time, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import central as C

WHISPER_PYTHON = os.environ.get("WHISPER_PYTHON") or os.path.expanduser("~/claude-telegram-central/whisper/venv/bin/python")
SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "transcribe.py")
TRANSCRIBE_TIMEOUT_S = 3600
MAX_TRIES = 5
CAP = int(float(os.environ.get("CENTRAL_DISK_CAP_GB", "20")) * 1024 ** 3)
CAP_WARNED = os.path.join(C.BASE, "cap-warned.txt")
VIDEOS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".3gp"}


def usage():
    return sum(e.stat().st_size for e in os.scandir(C.RECEIVED) if e.is_file())


def download(l):
    info = C.tg("getFile", http_timeout=120, file_id=l["file_id"])
    ext = os.path.splitext(info["file_path"])[1] or os.path.splitext(l.get("name") or "")[1]
    dest = os.path.join(C.RECEIVED, l["pid"] + ext)
    if os.path.isabs(info["file_path"]) and os.path.exists(info["file_path"]):     # local Bot API server: the file is already on this machine
        shutil.copyfile(info["file_path"], dest + ".part")
    else:
        url = "%s/file/bot%s/%s" % (C.API, C.conf()["TELEGRAM_TOKEN"], info["file_path"])
        with urllib.request.urlopen(url, timeout=600) as r, open(dest + ".part", "wb") as f:
            shutil.copyfileobj(r, f)
    os.replace(dest + ".part", dest)
    return dest


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1048576), b""):
            h.update(block)
    return h.hexdigest()


def transcribe(path):
    """(text, seconds) or (None, reason)."""
    start = time.time()
    p = subprocess.run([WHISPER_PYTHON, SCRIPT, path], capture_output=True, timeout=TRANSCRIBE_TIMEOUT_S)
    if p.returncode:
        return None, p.stderr.decode("utf-8", "replace")[-200:]
    return p.stdout.decode("utf-8", "replace").strip(), round(time.time() - start)


def extract_frames(path, pid):
    """4 frames spread over the video. Empty list if it cannot: it never holds up the delivery."""
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path], capture_output=True, timeout=120)
        duration = float(p.stdout.decode().strip())
    except (ValueError, subprocess.SubprocessError, OSError):
        return []
    made = []
    for i in range(4):
        name = "%s.frame%d.jpg" % (pid, i + 1)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", str(duration * (i + 0.5) / 4), "-i", path, "-frames:v", "1", "-q:v", "4", os.path.join(C.RECEIVED, name)],
                       capture_output=True, timeout=300)
        if os.path.exists(os.path.join(C.RECEIVED, name)):
            made.append(name)
    return made


def process(l):
    owner = C.conf()["TELEGRAM_CHAT_ID"]
    path = download(l)
    done = {"error": "", "file": os.path.basename(path), "real_size": os.path.getsize(path), "sha256": sha256_of(path)}
    video = l["type"] == "file" and os.path.splitext(path)[1].lower() in VIDEOS
    if video:
        done["frames"] = extract_frames(path, l["pid"])
    if l["type"] == "audio" or video:
        try:
            text, info = transcribe(path)
        except Exception as e:
            text, info = None, type(e).__name__
        if text is None:
            done["error"] = "no transcript: %s" % info
            C.log("transcription failed %s: %s" % (l["pid"], str(info)[:120]))
        elif C.looks_secret(text):
            # Nothing the owner sends is deleted; the file stays, only the transcript is withheld.
            C.log("refused: audio that looks like a secret %s" % l["pid"])
            C.tg_safe("sendMessage", chat_id=owner, text="🔒 That audio seems to contain a key or card number. I kept the file but did not pass the text on.")
            done["error"] = "transcript withheld: it looked like a secret"
        elif text:
            done.update(transcript=text, seconds=info)
            C.log("transcribed %s in %s s, %d characters" % (l["pid"], info, len(text)))
    return done


def main():
    os.makedirs(C.RECEIVED, exist_ok=True)
    for l in C.read_inbox():
        if l.get("type", "text") == "text" or l.get("big"):
            continue
        mark = os.path.join(C.RECEIVED, l["pid"] + ".json")
        if os.path.exists(mark):
            continue
        if usage() + (l.get("size") or 0) > CAP:     # nothing is lost: the rest of the queue waits for the PC to fetch and delete
            if not os.path.exists(CAP_WARNED):
                open(CAP_WARNED, "w").close()
                C.log("disk cap reached")
                C.tg_safe("sendMessage", chat_id=C.conf()["TELEGRAM_CHAT_ID"], text="⚠️ The server reached its %d GB cap of files waiting for the PC. What you send stays on Telegram and I download it when the PC has taken the rest." % (CAP // 1024 ** 3))
            break
        if os.path.exists(CAP_WARNED):
            os.remove(CAP_WARNED)
        counter = os.path.join(C.RECEIVED, l["pid"] + ".tries")
        n = int(open(counter).read()) if os.path.exists(counter) else 0
        try:
            done = process(l)
        except Exception as e:   # failed to download: retry next minute; after 5 the queue moves on and the coordinator is told
            C.log("download failed %s: %s" % (l["pid"], type(e).__name__))
            with open(counter, "w") as f:
                f.write(str(n + 1))
            if n + 1 < MAX_TRIES:
                break
            done = {"error": "could not download from Telegram"}
        with open(mark + ".tmp", "w", encoding="utf-8") as f:
            json.dump(done, f, ensure_ascii=False)
        os.replace(mark + ".tmp", mark)
    return 0


if __name__ == "__main__":
    sys.exit(main())
