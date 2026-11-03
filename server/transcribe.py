#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Transcribes an audio file with local Whisper (faster-whisper). Run it with the Whisper venv's python. Usage: transcribe.py file. Text goes to stdout.
ffmpeg decodes the audio: faster-whisper's built-in decoder can break depending on the installed PyAV version.
Env: WHISPER_MODEL (default "small"), WHISPER_MODEL_DIR (model cache), WHISPER_LANGUAGE (optional, e.g. "en"; default auto-detect)."""
import os, subprocess, sys

import numpy as np
from faster_whisper import WhisperModel

audio = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", sys.argv[1], "-f", "s16le", "-ac", "1", "-ar", "16000", "-"], capture_output=True, check=True).stdout
samples = np.frombuffer(audio, np.int16).astype(np.float32) / 32768.0
model = WhisperModel(os.environ.get("WHISPER_MODEL", "small"), device="cpu", compute_type="int8",
                     download_root=os.environ.get("WHISPER_MODEL_DIR") or os.path.expanduser("~/claude-telegram-central/whisper/models"))
segments, _ = model.transcribe(samples, beam_size=1, vad_filter=True, language=os.environ.get("WHISPER_LANGUAGE") or None)
print(" ".join(s.text.strip() for s in segments))
