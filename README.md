# claude-telegram-central

Answer Claude Code questions and permission prompts, and talk to a coordinating Claude Code session, from a Telegram bot on your phone.

When a Claude Code session stops to ask you something (a question with options, a plan to approve, a permission prompt), the bot sends it to your Telegram with one button per option plus an **Other** button for free text or a voice note. Your answer is queued on a small server, so it is not lost if your PC is off. Anything else you send the bot (text, voice, photo, video, file up to 2 GB) is queued for a coordinator session on your PC, with voice and video audio transcribed by a local Whisper model and 4 frames pulled from each video.

No AI runs in the transport: it is plain scripts, SSH and cron.

## Architecture

```
 Claude Code sessions (PC)                     Server (small Linux VPS)                 Phone
 ─────────────────────────                     ────────────────────────                 ─────
 hook ──► hook-pending/*.json
 watcher.py  (reads transcripts,
   finds stalled questions) ──┐
                              │  SSH, JSON in/out
 bridge.py ◄──────────────────┴────────────────► central.py --bridge
   answers.jsonl  (undelivered answers)             │  state, answer queue, inbox
   inbox-queue.jsonl (your messages/files)          │
                                                    ├─ central.py (cron, long-poll) ◄──► Telegram Bot API ◄──► your bot chat
 listener.py --wait  (coordinator session)          ├─ receive_files.py (cron): download, sha256, Whisper, video frames
   prints new messages, then exits to wake          └─ optional local Bot API server (127.0.0.1) for files up to 2 GB
   the session
 reply_telegram.py / send_files_telegram.py ──────► your chat only
 answers.py list|ack  ◄── the coordinator hands each answer to the stalled session
```

Flow of a question: session stalls -> hook/watcher sees it -> bridge pushes it to the server -> bot posts buttons -> you tap -> answer stored in `answers.jsonl` on the server -> next bridge round copies it to the PC -> the coordinator (or your own script) delivers it to the session and runs `answers.py ack <id>` -> Telegram shows "Delivered".

This project does **not** type into a stalled session by itself. Handing the answer over is the coordinator's job (for example a Claude Code session that can message other sessions), or a script you write against `answers.py list`.

## Requirements

- A Telegram bot token from @BotFather and your own numeric chat id.
- A Linux server with Python 3.8+, cron, `flock`, and (optional) `ffmpeg` for video frames and audio.
- Your PC with Python 3.8+, `ssh`/`scp` and key-based access to the server.
- Optional: `faster-whisper` in its own venv on the server for transcription; a built `telegram-bot-api` for files over 20 MB.

## Setup

1. **Create the bot.** Talk to @BotFather, create a bot, keep the token. Send your bot any message, then open `https://api.telegram.org/bot<token>/getUpdates` in a browser once to read your numeric `chat.id`. Never paste the token into a chat with an AI or into a repository.

2. **Server.** Copy `server/` to the server (for example `~/claude-telegram-central/server`).
   ```
   mkdir -p ~/.config/claude-telegram-central
   cp server/config.example.env ~/.config/claude-telegram-central/telegram.env
   chmod 600 ~/.config/claude-telegram-central/telegram.env   # then fill in TELEGRAM_TOKEN and TELEGRAM_CHAT_ID
   crontab -e                                                  # add the two lines from server/crontab.example
   ```
   Data goes to `CENTRAL_HOME` (default `~/claude-telegram-central/data`). Run `python3 server/central.py` once by hand to check it listens.

3. **PC.** Copy `pc/config.example.env` to `~/.config/claude-telegram-central/pc.env` and fill in `CENTRAL_SSH_HOST` and `CENTRAL_SSH_KEY`. Check `ssh <host> true` works without a prompt. Optional: set `CENTRAL_DATA_DIR` in the environment to move the PC data directory (default `~/.claude-telegram-central`).

4. **Claude Code hook.** Register `pc/central_hook.py` in your Claude Code settings (`~/.claude/settings.json`), as in `pc/claude-settings.example.json`: a `PreToolUse` entry with matcher `AskUserQuestion|ExitPlanMode` and a `PermissionRequest` entry, both running `python3 -I "/path/to/claude-telegram-central/pc/central_hook.py"` (on Windows use `pythonw.exe` so no console flashes). The hook only records the request; it never answers, blocks or changes the decision.

5. **Watcher.** Schedule `python -I pc/watcher.py` every ~15 minutes and `python -I pc/watcher.py --fast` every ~3 minutes (cron, or `schtasks` on Windows). The fast pass reads only transcripts touched in the last 30 minutes.

6. **Coordinator session** (optional, for chatting from the phone). Tell a Claude Code session, in its `CLAUDE.md` or by prompt, to run `python -I pc/listener.py --wait` as a background task. It exits when you send a message; its output wakes the session. The session answers with `python -I pc/reply_telegram.py <id> "text"` and starts the listener again. It can also use `answers.py list` / `ack` to hand answers to stalled sessions.

7. **Files and voice (optional).** On the server: `apt install ffmpeg`, then `python3 -m venv ~/claude-telegram-central/whisper/venv && ~/claude-telegram-central/whisper/venv/bin/pip install -r server/requirements-whisper.txt`. Without this, audio and video arrive without a transcript. For files over 20 MB, build `telegram-bot-api` (github.com/tdlib/telegram-bot-api) into `~/claude-telegram-central/telegram-local/bin/`, put `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` in `telegram-local.env` (chmod 600, see `server/telegram-local.example.env`) and run `server/setup_local_bot_api.sh` once. Set `TELEGRAM_VIA_SERVER=1` in `pc.env` afterwards: the official API no longer accepts the token from the PC.

8. **Jobs (optional).** `/job` in the chat lists a closed set of named jobs from `jobs.json` (see `server/jobs.example.json`) and queues the chosen one in `jobs-queue.jsonl` on the PC. Nothing runs them for you: point your own runner at that file.

## Security notes

- **Owner-only chat.** Every message and button click must come from both the chat id and the user id in `TELEGRAM_CHAT_ID`. Anyone else gets a refusal and nothing is stored.
- **Secret filter.** Pasted API keys, bot tokens and card numbers (Luhn-checked; links and ids inside URLs are skipped) are not stored or forwarded, on the way in and on the way out (`reply_telegram.py`). The filter never deletes your messages; it only refuses to store them and tells you. If a voice note transcribes to something secret, the file is kept and only the transcript is withheld.
- **Questions about secrets** (passwords, keys, 2FA codes, card or ID numbers) get no buttons and no text on the server: the bot just says "do it on the PC".
- **Tokens only in env/config files** with `chmod 600`, never in code, URLs logged or command lines (the local-server script passes the token to curl through stdin). Error logs record the exception type only, because the URL contains the token.
- **Nothing auto-approves anything.** The hook is passive. Your answer is data handed to a session; permission prompts still need the real decision in Claude Code. Session text shown in Telegram and your messages shown to the coordinator are framed as data, never as commands. Attachments are saved and never opened or executed.
- **No open ports.** The server only makes outbound calls to Telegram; the optional local Bot API server binds to 127.0.0.1.
- Treat the SSH key like any production key: give it its own account on the server with no sudo.

## Tests

```
python -I tests/run_all.py
```
Runs offline with a fake Telegram and a fake SSH; nothing touches the network.

## Known limits

- A voice note sent as the answer to an open question is saved as a file path on the server, not transcribed (voice notes sent outside a question are transcribed). Use text for question answers, or add transcription in `handle_message`.
- `send_files_telegram.py` reads each file fully into server memory while uploading.
- User-facing bot messages are in English; they are plain text constants in `server/central.py`.

## License

MIT, see `LICENSE`.
