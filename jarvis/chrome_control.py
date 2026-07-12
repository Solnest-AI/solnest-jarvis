"""
jarvis/chrome_control.py — full Chrome control for Jarvis via agent-browser (CDP).

Jarvis drives a REAL, logged-in Google Chrome using the `agent-browser` CLI,
which speaks CDP under the hood (no AppleScript, no macOS Automation/TCC).

WHY A DEDICATED PROFILE:
Chrome 149 blocks remote-debugging on the DEFAULT profile (a 136+ security
change), and the default profile is locked by your running Chrome anyway.
So Jarvis uses its own persistent profile directory (PROFILE_DIR) that you
log into ONCE. agent-browser launches real Chrome with that profile, headed
(visible), and a fixed `--session jarvis` so browser state persists across
calls.

Each tool returns a concise string, logs to stderr, and never prints secrets.

Tools:
  chrome_open(url)            — open/navigate the visible Jarvis Chrome
  chrome_read()              — accessibility snapshot (clickable @refs)
  chrome_text()              — plain page text (fallback when snapshot is huge)
  chrome_click(target)       — click a @ref (from snapshot) or CSS selector
  chrome_type(target, text)  — clear+fill a field by @ref or CSS selector
  chrome_eval(js)            — run JavaScript on the page; return result
  chrome_screenshot()        — screenshot to /tmp/jarvis-chrome.png; return path
  chrome_url()               — current page URL
  chrome_title()             — current page title
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# Logging (stderr only)
# --------------------------------------------------------------------------- #
logger = logging.getLogger("jarvis.chrome_control")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [chrome_control] %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)

# --------------------------------------------------------------------------- #
# Config — real Chrome + dedicated persistent profile + fixed session
# --------------------------------------------------------------------------- #
CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PROFILE_DIR = str(Path(os.path.expanduser("~")) / ".jarvis" / "chrome-profile")
SESSION = "jarvis"
SCREENSHOT_PATH = "/tmp/jarvis-chrome.png"
_TIMEOUT = 60


# --------------------------------------------------------------------------- #
# Core helper
# --------------------------------------------------------------------------- #
def _daemon_running() -> bool:
    """Return True if an agent-browser daemon for SESSION is already alive.

    The launch flags (--executable-path / --profile / --headed) must ONLY be
    passed on a COLD start. Passing them to a running daemon makes agent-browser
    reset the live page to about:blank and emit a warning, so we detect the
    daemon first via `session list` and skip the launch flags when it's up.
    """
    try:
        result = subprocess.run(
            ["agent-browser", "session", "list"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return False
    out = (result.stdout or "") + (result.stderr or "")
    # Match the session token on its own (avoids matching e.g. "jarvis-x").
    for line in out.splitlines():
        token = line.strip().lstrip("→").strip()
        if token == SESSION:
            return True
    return False


def _ab(*args: str, headed: bool = False) -> str:
    """Run agent-browser against the dedicated Jarvis Chrome profile/session.

    Cold start (no daemon yet) builds:
        agent-browser --executable-path <CHROME> --profile <PROFILE_DIR>
                      --session jarvis --headed <args...>
    Warm (daemon already running) builds:
        agent-browser --session jarvis <args...>

    Launch flags go BEFORE the subcommand and are only used on a cold start —
    passing them to a live daemon resets its page. We always cold-start headed
    so you can SEE the Jarvis Chrome window. Creates PROFILE_DIR if missing.
    Returns stdout (trimmed) on success, or a trimmed error string on failure.
    """
    # Ensure the persistent profile directory exists.
    try:
        Path(PROFILE_DIR).mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.error("could not create profile dir %s: %s", PROFILE_DIR, exc)
        return f"Error: could not create Chrome profile dir: {exc}"

    cold = not _daemon_running()
    cmd: list[str] = ["agent-browser"]
    if cold:
        # Real Chrome + persistent logged-in profile, visible window.
        cmd += ["--executable-path", CHROME_PATH, "--profile", PROFILE_DIR]
    cmd += ["--session", SESSION]
    if cold:
        cmd.append("--headed")  # always headed on cold start (visible to you)
    cmd.extend(args)

    # Log the command without leaking any typed text (e.g. fill values) or
    # JS payloads (eval scripts may contain tokens/cookies).
    safe_args = list(args)
    if safe_args and safe_args[0] == "fill" and len(safe_args) >= 3:
        safe_args[2] = "<text>"
    elif safe_args and safe_args[0] == "eval" and len(safe_args) >= 2:
        safe_args[1] = f"<{len(safe_args[1])} chars>"
    logger.info("ab: %s%s", " ".join(safe_args), " [cold-start headed]" if cold else "")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    except FileNotFoundError:
        return (
            "agent-browser CLI not found. Install it via: brew install agent-browser"
        )
    except subprocess.TimeoutExpired:
        return f"Error: agent-browser timed out after {_TIMEOUT}s."
    except Exception as exc:
        logger.error("ab exception: %s", exc)
        return f"Error running agent-browser: {exc}"

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if result.returncode != 0:
        msg = stderr or stdout or "unknown error"
        logger.warning("ab returncode=%d: %s", result.returncode, msg[:300])
        return f"Error (agent-browser): {msg[:500]}"
    return stdout


def _normalise_url(url: str) -> str:
    """Prepend https:// to bare domains."""
    url = url.strip()
    if url and not url.startswith(("http://", "https://", "ftp://", "file://")):
        url = "https://" + url
    return url


# --------------------------------------------------------------------------- #
# Tool implementations
# --------------------------------------------------------------------------- #
def chrome_open(url: str) -> str:
    """Open/navigate the visible Jarvis Chrome to a URL (or bare domain)."""
    if not url:
        return "Error: URL is required."
    url = _normalise_url(url)
    out = _ab("open", url, headed=True)
    if out.startswith("Error"):
        return out
    return f"Opened {url} in the Jarvis Chrome window." + (f"\n{out}" if out else "")


def chrome_read() -> str:
    """Return the page accessibility snapshot (clickable @refs + structure)."""
    out = _ab("snapshot")
    if not out:
        return "Snapshot returned empty output."
    return out[:8000]  # cap to avoid flooding context


def chrome_text() -> str:
    """Return the page's plain visible text (fallback when snapshot is huge)."""
    out = _ab("get", "text", "body")
    if not out:
        return "Page text returned empty output."
    return out[:8000]


def chrome_click(target: str) -> str:
    """Click an element by @ref (from chrome_read) or CSS selector."""
    if not target:
        return "Error: target (@ref or CSS selector) is required."
    out = _ab("click", target)
    if out.startswith("Error"):
        return out
    return f"Clicked {target}." + (f"\n{out}" if out else "")


def chrome_type(target: str, text: str) -> str:
    """Clear and fill a field by @ref or CSS selector with text."""
    if not target:
        return "Error: target (@ref or CSS selector) is required."
    out = _ab("fill", target, text)
    if out.startswith("Error"):
        return out
    return f"Typed into {target}." + (f"\n{out}" if out else "")


def chrome_eval(js: str) -> str:
    """Run JavaScript on the current page and return the result."""
    if not js:
        return "Error: js is required."
    out = _ab("eval", js)
    return out or "(eval ran with no output)"


def chrome_screenshot() -> str:
    """Screenshot the current page to /tmp/jarvis-chrome.png; return the path."""
    out = _ab("screenshot", SCREENSHOT_PATH)
    if out.startswith("Error"):
        return out
    return SCREENSHOT_PATH


def chrome_url() -> str:
    """Return the current page URL."""
    out = _ab("get", "url")
    return out or "(no URL)"


def chrome_title() -> str:
    """Return the current page title."""
    out = _ab("get", "title")
    return out or "(no title)"
