"""
Jarvis — Text-to-Speech module (ElevenLabs; ffmpeg OPTIONAL).

ffmpeg is NOT required for the web voice experience. The server streams the raw
MP3 bytes returned by synthesize() straight to the browser, and Chrome's WebAudio
decodes MP3 directly — so ElevenLabs voice works whether or not ffmpeg is
installed. ffmpeg is only used by the optional Opus conversion helpers below
(to_opus / speak), which exist for Opus consumers like Telegram voice notes. When
ffmpeg is absent those helpers degrade gracefully (return the MP3 unchanged) instead
of raising, so a missing ffmpeg can never make voice crash or go silent.

Three public functions:

    synthesize(text)   -> mp3 bytes | None
        Calls ElevenLabs API; returns raw MP3 audio bytes.
        Returns None if credentials are not configured.
        Retries once on transient failures; logs response body on non-200.
        This is the function the web server uses — no ffmpeg involved.

    to_opus(mp3)       -> bytes
        Converts raw MP3 bytes to OGG/Opus via ffmpeg (stdin→stdout pipe) when
        ffmpeg is available. If ffmpeg is NOT installed, logs one INFO line and
        returns the MP3 bytes unchanged (graceful fallback — never raises for a
        missing ffmpeg). Suitable for Telegram voice notes or any Opus consumer
        that tolerates MP3 as a fallback.

    speak(text)        -> bytes | None
        Convenience: synthesize → to_opus.  Returns None if TTS is disabled.
        Returns Opus bytes when ffmpeg is present, otherwise the MP3 bytes.

Config keys read from Jarvis/.env (via python-dotenv):
    ELEVENLABS_API_KEY    — required; TTS disabled if absent/empty
    ELEVENLABS_VOICE_ID   — required; TTS disabled if absent/empty
    ELEVENLABS_MODEL      — optional; default "eleven_turbo_v2_5"
                            Tip: set to "eleven_multilingual_v2" for even
                            better number/date reading (higher quality,
                            slightly more latency).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent.parent   # Jarvis/ root
load_dotenv(BASE_DIR / ".env")

ELEVENLABS_API_KEY: str = os.environ.get("ELEVENLABS_API_KEY", "").strip()
# Invalidate placeholder tokens (matching Telegram Agent convention)
if ELEVENLABS_API_KEY.startswith("PASTE_"):
    ELEVENLABS_API_KEY = ""

ELEVENLABS_VOICE_ID: str = os.environ.get("ELEVENLABS_VOICE_ID", "").strip()
ELEVENLABS_MODEL: str = os.environ.get(
    "ELEVENLABS_MODEL", "eleven_turbo_v2_5"
).strip()

# Text normalization mode sent to ElevenLabs.
# "on"   — explicit normalisation (best quality; some turbo/flash builds reject it)
# "auto" — engine decides (always accepted; fall back here if API returns 4xx)
TTS_NORMALIZATION: str = "on"

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
logger = logging.getLogger("jarvis.voice_tts")
if not logger.handlers:
    _sh = logging.StreamHandler(sys.stderr)
    _sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_sh)
    logger.setLevel(logging.INFO)


# --------------------------------------------------------------------------- #
# ffmpeg detection (OPTIONAL — only the Opus helpers need it)
# --------------------------------------------------------------------------- #
# Detected once at import. ffmpeg is NOT required for the web voice path; it's
# only used by to_opus()/speak() to produce Opus for consumers like Telegram.
# Most giveaway recipients won't have ffmpeg, and that's fine: voice still works
# because the server streams raw MP3 (synthesize) which the browser decodes.
def _ffmpeg_path() -> str | None:
    """Return the path to the ffmpeg binary, or None if it isn't installed.

    Wrapped in a function (not a module constant) so tests can monkeypatch it to
    simulate an ffmpeg-absent machine. Works on Windows and macOS/Linux —
    shutil.which() resolves the right executable name per platform.
    """
    return shutil.which("ffmpeg")


# --------------------------------------------------------------------------- #
# Pre-formatter — normalise text before sending to ElevenLabs
# --------------------------------------------------------------------------- #
# Month names for ISO date expansion
_MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

# Currency: $1,234.56  or  $1,500  →  "1234.56 dollars" / "1500 dollars"
_RE_CURRENCY = re.compile(r'\$(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)')

# Bare thousands separators in plain numbers: 24,500 → 24500
# Only matches digit,digit patterns — not prose commas.
_RE_THOUSANDS = re.compile(r'\b(\d{1,3}(?:,\d{3})+)\b')

# Percent: 87% → "87 percent"
_RE_PERCENT = re.compile(r'\b(\d+(?:\.\d+)?)%')

# ISO date: 2026-06-04 → "June 4, 2026"
_RE_ISO_DATE = re.compile(r'\b(\d{4})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\b')


def _normalize_for_speech(text: str) -> str:
    """Pre-format text so ElevenLabs reads numbers, currency, and dates correctly.

    Transformations (conservative — normal prose is never altered):
      - Currency  : $1,234.56 → "1234.56 dollars"  |  $1,500 → "1500 dollars"
      - Thousands : 24,500    → "24500"  (removes grouping comma so TTS reads
                                          it as a single number, not "two four,
                                          five hundred")
      - Percent   : 87%       → "87 percent"
      - ISO dates : 2026-06-04 → "June 4, 2026"

    Order matters: currency is stripped first so its commas don't trigger the
    bare-thousands pass.
    """
    # 1. Currency — strip $ and thousands commas, append " dollars"
    def _expand_currency(m: re.Match) -> str:
        value = m.group(1).replace(",", "")
        return f"{value} dollars"

    text = _RE_CURRENCY.sub(_expand_currency, text)

    # 2. Bare thousands separators in plain numbers
    def _strip_thousands(m: re.Match) -> str:
        return m.group(1).replace(",", "")

    text = _RE_THOUSANDS.sub(_strip_thousands, text)

    # 3. Percent sign
    text = _RE_PERCENT.sub(lambda m: f"{m.group(1)} percent", text)

    # 4. ISO dates
    def _expand_date(m: re.Match) -> str:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{_MONTH_NAMES[month]} {day}, {year}"

    text = _RE_ISO_DATE.sub(_expand_date, text)

    return text


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def synthesize(text: str) -> bytes | None:
    """Call ElevenLabs TTS and return raw MP3 bytes.

    Returns None if ELEVENLABS_API_KEY or ELEVENLABS_VOICE_ID are not set.

    Applies _normalize_for_speech() before sending so numbers, currency,
    percentages, and dates are spoken correctly regardless of model.

    Also sends apply_text_normalization="on" (TTS_NORMALIZATION) to the API.
    If the model rejects "on" with a 4xx, retries once with "auto".

    Retries once on transient failures (ElevenLabs free/scoped keys can throw
    spurious 401s under request bursts).  Logs the response body on non-200
    so real failures are diagnosable without exposing the API key.

    Args:
        text: The text to synthesize.

    Returns:
        MP3 audio bytes, or None if credentials are not configured.

    Raises:
        RuntimeError: if both retry attempts fail.
    """
    if not (ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID):
        logger.warning(
            "ElevenLabs credentials not configured; TTS disabled. "
            "Set ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID in .env."
        )
        return None

    import httpx  # lazy import keeps module importable without the dep

    # Pre-format text for reliable number/date reading
    text = _normalize_for_speech(text)

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "accept": "audio/mpeg",
        "content-type": "application/json",
    }

    def _build_payload(normalization_value: str) -> dict:
        return {
            "text": text,
            "model_id": ELEVENLABS_MODEL,
            "voice_settings": {
                "stability": 0.4,          # lower = livelier, more expressive/personality
                "similarity_boost": 0.75,
                "style": 0.3,              # adds character/attitude to the delivery
                "speed": 1.1,              # ~10% faster talking (range 0.7–1.2)
            },
            "apply_text_normalization": normalization_value,
        }

    mp3: bytes | None = None
    last_err: str = ""
    normalization_used = TTS_NORMALIZATION

    for attempt in range(2):
        payload = _build_payload(normalization_used)
        try:
            r = httpx.post(url, json=payload, headers=headers, timeout=60.0)
            if r.status_code == 200:
                mp3 = r.content
                break
            # If the model rejected our normalization value, retry with "auto"
            body_snippet = r.text[:300]
            if (
                r.status_code in (400, 422)
                and normalization_used != "auto"
                and "normalization" in body_snippet.lower()
            ):
                logger.warning(
                    "apply_text_normalization=%r rejected (HTTP %d); "
                    "retrying with 'auto'",
                    normalization_used, r.status_code,
                )
                normalization_used = "auto"
                # don't count this against the retry budget — loop again
                continue
            last_err = f"HTTP {r.status_code}: {body_snippet}"
        except Exception as exc:
            last_err = repr(exc)

        logger.warning(
            "ElevenLabs TTS attempt %d/%d failed: %s", attempt + 1, 2, last_err
        )
        if attempt == 0:
            time.sleep(1.5)

    if mp3 is None:
        raise RuntimeError(f"ElevenLabs TTS failed after retries: {last_err}")

    logger.info(
        "ElevenLabs TTS ok: %d mp3 bytes (normalization=%r)",
        len(mp3), normalization_used,
    )
    return mp3


def to_opus(mp3: bytes) -> bytes:
    """Convert MP3 bytes to OGG/Opus bytes using ffmpeg (stdin→stdout pipe).

    Uses 32 kbps Opus in an OGG container — suitable for Telegram voice notes
    and most Opus consumers.

    ffmpeg is OPTIONAL. If ffmpeg is not installed, this logs one INFO line and
    returns the MP3 bytes UNCHANGED — a graceful fallback so a missing ffmpeg
    never crashes voice. (Most giveaway recipients won't have ffmpeg; the web
    voice path doesn't need this function at all — it streams raw MP3.)

    Args:
        mp3: Raw MP3 audio bytes.

    Returns:
        OGG/Opus audio bytes when ffmpeg is available; otherwise the original
        MP3 bytes unchanged.

    Raises:
        RuntimeError: only if ffmpeg IS present but returns a non-zero exit code
            (a real conversion failure — not a missing-binary situation).
    """
    if _ffmpeg_path() is None:
        logger.info(
            "ffmpeg not found — skipping MP3→Opus conversion, returning MP3 "
            "bytes as-is (install ffmpeg for Opus output; not needed for "
            "browser voice)."
        )
        return mp3

    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "error",
                "-i", "pipe:0",
                "-c:a", "libopus",
                "-b:a", "32k",
                "-f", "ogg",
                "pipe:1",
            ],
            input=mp3,
            capture_output=True,
        )
    except FileNotFoundError:
        # ffmpeg disappeared between detection and exec (rare race / PATH change).
        # Degrade gracefully rather than crash.
        logger.info(
            "ffmpeg vanished before exec — returning MP3 bytes unchanged."
        )
        return mp3

    if proc.returncode != 0 or not proc.stdout:
        stderr_text = proc.stderr.decode("utf-8", "replace")[:400]
        raise RuntimeError(f"ffmpeg mp3→ogg/opus failed: {stderr_text}")

    logger.info("ffmpeg conversion ok: %d ogg bytes", len(proc.stdout))
    return proc.stdout


def speak(text: str) -> bytes | None:
    """Synthesize text to speech and return audio bytes.

    Convenience wrapper: synthesize(text) → to_opus(mp3).

    Returns OGG/Opus bytes when ffmpeg is installed; when ffmpeg is ABSENT it
    returns the raw MP3 bytes instead (to_opus degrades gracefully). Returns
    None if TTS credentials are not configured (synthesize returns None).

    Note: callers that strictly require Opus should check for ffmpeg themselves;
    today this function has no in-tree callers (the web server uses synthesize()
    directly), so the MP3 fallback is a safe, non-breaking convenience.

    Args:
        text: The text to speak.

    Returns:
        OGG/Opus audio bytes (ffmpeg present) or MP3 audio bytes (ffmpeg
        absent), or None if TTS is disabled.

    Raises:
        RuntimeError: on ElevenLabs errors, or a real ffmpeg conversion failure
            (not a missing ffmpeg).
    """
    mp3 = synthesize(text)
    if mp3 is None:
        return None
    return to_opus(mp3)


# --------------------------------------------------------------------------- #
# Inline normalizer checks (run with: python -m jarvis.voice_tts)
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    _cases = [
        ("Currency with cents",     "The price is $1,299.99.",
                                    "The price is 1299.99 dollars."),
        ("Currency no cents",       "Revenue is $24,500 this month.",
                                    "Revenue is 24500 dollars this month."),
        ("Bare thousands",          "We have 130 reservations.",
                                    "We have 130 reservations."),          # unchanged
        ("Bare thousands (large)",  "Total: 1,200 nights.",
                                    "Total: 1200 nights."),
        ("Percent",                 "Occupancy is 87%.",
                                    "Occupancy is 87 percent."),
        ("ISO date",                "Next check-in is 2026-06-04.",
                                    "Next check-in is June 4, 2026."),
        ("Combined",
         "Revenue is $24,500 and occupancy is 87%.",
         "Revenue is 24500 dollars and occupancy is 87 percent."),
        ("Normal prose",            "Hello, how are you today?",
                                    "Hello, how are you today?"),           # unchanged
    ]

    all_ok = True
    for label, inp, expected in _cases:
        got = _normalize_for_speech(inp)
        status = "OK" if got == expected else "FAIL"
        if status == "FAIL":
            all_ok = False
        print(f"[{status}] {label}")
        print(f"       in : {inp!r}")
        print(f"       out: {got!r}")
        if status == "FAIL":
            print(f"  expect : {expected!r}")
        print()

    # ----------------------------------------------------------------------- #
    # ffmpeg-absent fallback check: to_opus() must return the input bytes
    # unchanged (not raise) when ffmpeg is missing. Simulate by forcing the
    # detector to report no ffmpeg.
    # ----------------------------------------------------------------------- #
    _fake_mp3 = b"FAKE_MP3_BYTES_FOR_TEST"
    _orig_detect = _ffmpeg_path
    try:
        globals()["_ffmpeg_path"] = lambda: None   # simulate ffmpeg absent
        out = to_opus(_fake_mp3)
        fallback_ok = out == _fake_mp3 and isinstance(out, (bytes, bytearray))
    except Exception as exc:  # any raise here is a failure
        fallback_ok = False
        print(f"[FAIL] ffmpeg-absent fallback raised: {exc!r}")
    finally:
        globals()["_ffmpeg_path"] = _orig_detect

    print(f"[{'OK' if fallback_ok else 'FAIL'}] to_opus() ffmpeg-absent fallback "
          f"returns MP3 bytes unchanged")
    if not fallback_ok:
        all_ok = False
    print()

    sys.exit(0 if all_ok else 1)
