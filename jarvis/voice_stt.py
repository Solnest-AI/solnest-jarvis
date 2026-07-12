"""
Jarvis — Speech-to-Text (cross-platform: Mac + Windows + Linux).

Used for PHONE voice: a recorded clip is transcribed locally with faster-whisper
(CPU, works everywhere). Desktop voice uses the browser's own speech recognition,
so this only runs for phone/mobile clips.

The model is NOT bundled — it downloads to a local cache on first use (~150MB for
"base"). Same public API as before:  transcribe(path) -> str
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# faster-whisper model name: "base" (balanced), "tiny" (faster), "small" (sharper).
WHISPER_MODEL: str = os.environ.get("WHISPER_MODEL", "base").strip() or "base"

logger = logging.getLogger("jarvis.voice_stt")
if not logger.handlers:
    _sh = logging.StreamHandler(sys.stderr)
    _sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_sh)
    logger.setLevel(logging.INFO)

_model = None


def model_loaded() -> bool:
    return _model is not None


def _load():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel  # lazy: importable without the dep
        logger.info("loading speech model %s (downloads on first use)", WHISPER_MODEL)
        _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    return _model


def transcribe(path: str) -> str:
    """Transcribe an audio file to text. Returns '' on empty.

    Raises ImportError if faster-whisper isn't installed (caller can message the user)."""
    model = _load()
    logger.info("transcribing %s with %s", path, WHISPER_MODEL)
    segments, _info = model.transcribe(path, language="en", vad_filter=True)
    text = " ".join(seg.text for seg in segments).strip()
    logger.info("transcription complete: %d chars", len(text))
    return text
