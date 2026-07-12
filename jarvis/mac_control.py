"""
jarvis/mac_control.py — cross-platform desktop control tools for Jarvis.

Despite the historical "mac_control" name, every function here now works on
macOS, Windows, and Linux (or degrades gracefully where a feature is
OS-specific). Function names, signatures, and return types are unchanged so
callers keep working everywhere.

Each function returns a short status string (or dict for the b64 capture) and
logs to stderr. No secrets are ever printed or returned.

Tools:
  open_app(name)           — open an installed app (Mac `open -a` / Windows `start` / Linux `xdg-open`)
  open_url(url)            — open a URL/domain in the default browser (cross-platform webbrowser)
  install_app(name)        — install via Homebrew cask/formula (Mac-only; graceful elsewhere)
  see_screen(prompt)       — capture a screenshot; return path + byte size
  run_applescript(script)  — run AppleScript via osascript (Mac-only; graceful elsewhere)
  browse_web(url)          — headless agent-browser snapshot (read page content)
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
import sys
import tempfile
import webbrowser
from pathlib import Path

# --------------------------------------------------------------------------- #
# OS detection
# --------------------------------------------------------------------------- #
#   sys.platform: "darwin" = macOS, "win32" = Windows, else (e.g. "linux") = Linux/other
IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"
IS_LINUX = not IS_MAC and not IS_WINDOWS

# --------------------------------------------------------------------------- #
# Logging (stderr only)
# --------------------------------------------------------------------------- #
logger = logging.getLogger("jarvis.mac_control")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [mac_control] %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)


def _screenshot_path() -> str:
    """Return a per-OS temp path for the screenshot PNG.

    Uses the platform temp dir (so it works on Windows, which has no /tmp).
    """
    return str(Path(tempfile.gettempdir()) / "jarvis-screen.png")


# Kept as a module-level constant for backwards-compat with any importer.
# On Windows /tmp doesn't exist, so we resolve to the real temp dir here.
SCREENSHOT_PATH = _screenshot_path()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _run(cmd: list[str], timeout: int = 30, capture: bool = True) -> subprocess.CompletedProcess:
    """Run a subprocess, capturing output if requested."""
    logger.info("run: %s", " ".join(cmd))
    return subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        timeout=timeout,
    )


def _normalise_url(url: str) -> str:
    """Prepend https:// to bare domains."""
    url = url.strip()
    if url and not url.startswith(("http://", "https://", "ftp://", "file://")):
        url = "https://" + url
    return url


# --------------------------------------------------------------------------- #
# Web-app fallback map  (case-insensitive substring match)
# --------------------------------------------------------------------------- #
WEB_APPS: list[tuple[str, str]] = [
    ("instagram",        "https://www.instagram.com"),
    ("facebook",         "https://www.facebook.com"),
    ("whatsapp",         "https://web.whatsapp.com"),
    ("gmail",            "https://mail.google.com"),
    ("google calendar",  "https://calendar.google.com"),
    ("calendar",         "https://calendar.google.com"),
    ("youtube",          "https://www.youtube.com"),
    ("linkedin",         "https://www.linkedin.com"),
    ("tiktok",           "https://www.tiktok.com"),
    ("gohighlevel",      "https://app.gohighlevel.com"),
    ("ghl",              "https://app.gohighlevel.com"),
    ("twitter",          "https://x.com"),
    ("x.com",            "https://x.com"),
]

_URL_PREFIXES = ("http://", "https://", "ftp://", "file://", "www.")


def _match_web_app(name: str) -> str | None:
    """Return a URL if name matches a known web-only service, else None."""
    low = name.lower().strip()
    for keyword, url in WEB_APPS:
        if keyword in low:
            return url
    return None


def _looks_like_url(name: str) -> bool:
    """Return True if name looks like a URL or bare domain."""
    low = name.lower().strip()
    return any(low.startswith(p) for p in _URL_PREFIXES) or (
        "." in low and " " not in low
    )


# --------------------------------------------------------------------------- #
# Tool implementations
# --------------------------------------------------------------------------- #
def _open_native_app(name: str) -> bool:
    """Try to launch a native installed app by name on the current OS.

    Returns True if the launch command succeeded, False otherwise. Never raises.
      - macOS:   `open -a <name>`
      - Windows: `start "" "<name>"` via cmd (resolves Start-menu app names / exes)
      - Linux:   try `<name>` directly, then `xdg-open <name>` as a fallback
    """
    try:
        if IS_MAC:
            result = _run(["open", "-a", name])
            return result.returncode == 0
        if IS_WINDOWS:
            # `start` is a cmd built-in, so it must run through the shell.
            # First arg in quotes is the window title (empty), then the app/name.
            result = subprocess.run(
                f'start "" "{name}"',
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return result.returncode == 0
        # Linux / other: try launching the binary, then xdg-open.
        try:
            result = _run([name])
            if result.returncode == 0:
                return True
        except Exception:
            pass
        result = _run(["xdg-open", name])
        return result.returncode == 0
    except Exception as exc:
        logger.error("_open_native_app(%r) exception: %s", name, exc)
        return False


def open_app(name: str) -> str:
    """Open an installed app by name, with automatic web-app fallback.

    Cross-platform. Resolution order:
      1. Try native app (macOS `open -a`, Windows `start`, Linux binary/`xdg-open`).
         If it launches → done.
      2. Check WEB_APPS map (case-insensitive substring). If matched → open the
         service URL in the default browser (uses the user's logged-in session).
      3. If name looks like a URL/domain → delegate to open_url().
      4. Else → friendly error with instructions.

    Returns:
        Short status string. Never crashes.
    """
    if not name:
        return "Error: app name is required."

    # --- Step 1: try native app ---
    if _open_native_app(name):
        logger.info("open_app: launched/focused native app %r", name)
        return f"Opened {name}."

    # --- Step 2: web-app map ---
    url = _match_web_app(name)
    if url:
        try:
            ok = webbrowser.open(url)
            logger.info("open_app: opened %r as web app at %s (ok=%s)", name, url, ok)
            if ok:
                return f"Opened {name} as a web app."
            return f"Couldn't open {name} as a web app on this OS."
        except Exception as exc:
            logger.error("open_app web-app attempt exception: %s", exc)
            return f"Error opening {name} as a web app: {exc}"

    # --- Step 3: URL/domain fallback ---
    if _looks_like_url(name):
        return open_url(name)

    # --- Step 4: unknown ---
    return (
        f"Couldn't open '{name}' on this OS — no app by that name is installed, "
        "and I don't have a web app mapped for it. Tell me the URL and I'll open it."
    )


def open_url(url: str) -> str:
    """Open a URL (or bare domain) in the user's default browser.

    Cross-platform via Python's built-in `webbrowser` module — it picks the
    right opener on macOS, Windows, and Linux. The default browser uses the
    user's logged-in sessions, so "open my Instagram" works:
    open_url("instagram.com").

    Returns:
        Short status string. Never crashes.
    """
    if not url:
        return "Error: URL is required."
    url = _normalise_url(url)
    try:
        ok = webbrowser.open(url)
        if ok:
            logger.info("open_url: opened %s", url)
            return f"Opened {url} in the default browser."
        logger.warning("open_url: webbrowser.open returned False for %s", url)
        return f"Couldn't open '{url}' in a browser on this OS."
    except Exception as exc:
        logger.error("open_url exception: %s", exc)
        return f"Error opening URL '{url}': {exc}"


def install_app(name: str) -> str:
    """Install an app via Homebrew (macOS only).

    On macOS: tries `brew install --cask <name>` first; if the cask is not
    found, falls back to `brew install <name>` (formula).

    On Windows/Linux: app install through this tool isn't wired up, so it
    returns a friendly message instead of crashing.

    Returns:
        Short status string (first error line on failure).
    """
    if not name:
        return "Error: app name is required."
    if not IS_MAC:
        logger.info("install_app: not supported on this OS (sys.platform=%s)", sys.platform)
        return (
            f"App install via this tool is Mac-only (uses Homebrew); can't install "
            f"'{name}' on this OS. Install it manually for now."
        )
    try:
        logger.info("install_app: trying cask %r", name)
        result = _run(["brew", "install", "--cask", name], timeout=300)
        if result.returncode == 0:
            return f"Installed cask '{name}' via Homebrew."
        stderr = (result.stderr or "").strip()
        # If cask not found, fall back to formula
        if "cask" in stderr.lower() and ("not found" in stderr.lower() or "no cask" in stderr.lower()):
            logger.info("install_app: cask not found, trying formula %r", name)
            result2 = _run(["brew", "install", name], timeout=300)
            if result2.returncode == 0:
                return f"Installed formula '{name}' via Homebrew."
            err2 = (result2.stderr or result2.stdout or "").strip().splitlines()
            first_err = err2[0] if err2 else "unknown error"
            return f"Failed to install '{name}' (formula): {first_err}"
        # Some other cask error
        err_lines = stderr.splitlines()
        first_err = err_lines[0] if err_lines else "unknown error"
        return f"Failed to install cask '{name}': {first_err}"
    except Exception as exc:
        logger.error("install_app exception: %s", exc)
        return f"Error installing '{name}': {exc}"


# --------------------------------------------------------------------------- #
# Screen capture (cross-platform, no extra pip deps)
# --------------------------------------------------------------------------- #
# Windows screen grab using only built-in OS tooling: PowerShell +
# System.Windows.Forms / System.Drawing (ships with .NET on Windows). Captures
# the full virtual screen (all monitors) to a PNG file.
_WIN_CAPTURE_PS = r"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$b = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp = New-Object System.Drawing.Bitmap($b.Width, $b.Height)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.X, $b.Y, 0, 0, $bmp.Size)
$bmp.Save('{path}', [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose()
$bmp.Dispose()
"""


def _capture_to_file(path: str) -> tuple[bool, str]:
    """Capture the whole screen to `path` (PNG) using built-in OS tools only.

    Returns (ok, error_message). On success error_message is "". Never raises.
      - macOS:   `screencapture -x` (silent)
      - Windows: PowerShell + System.Windows.Forms/System.Drawing CopyFromScreen
      - Linux/other: not wired up → graceful failure message
    """
    try:
        if IS_MAC:
            result = _run(["screencapture", "-x", path], capture=True)
            if result.returncode != 0:
                err = (result.stderr or "").strip()
                return False, (
                    f"screencapture failed (returncode={result.returncode}): {err or 'unknown'}. "
                    "Screen Recording permission may be needed for this Python binary in "
                    "System Settings → Privacy & Security → Screen Recording."
                )
            return True, ""

        if IS_WINDOWS:
            # PowerShell wants forward slashes or escaped backslashes in the literal;
            # forward slashes are accepted by .NET path APIs on Windows.
            script = _WIN_CAPTURE_PS.replace("{path}", path.replace("\\", "/"))
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    script,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                err = (result.stderr or result.stdout or "").strip()
                return False, (
                    f"Windows screen capture failed (returncode={result.returncode}): "
                    f"{err or 'unknown'}."
                )
            return True, ""

        # Linux / other
        return False, "Screen capture isn't set up on this OS yet."
    except FileNotFoundError as exc:
        return False, f"Screen capture tool not found on this OS: {exc}"
    except Exception as exc:
        return False, f"Error capturing screen: {exc}"


def see_screen(prompt: str = "What is on screen?") -> str:
    """Capture a screenshot of the entire display.

    Cross-platform: macOS `screencapture -x` (silent), Windows PowerShell screen
    grab (built-in .NET, no extra deps). On other OSes it degrades gracefully.
    macOS needs Screen Recording permission for this Python binary.

    Returns:
        Path to the screenshot + byte size, or a friendly note if capture isn't
        available / produced an empty file. Never crashes.
    """
    path = _screenshot_path()
    try:
        ok, err = _capture_to_file(path)
        if not ok:
            logger.warning("see_screen: capture failed: %s", err)
            return err
        p = Path(path)
        if not p.exists():
            return (
                f"Screenshot file not created at {path}. "
                + ("Screen Recording permission may be needed." if IS_MAC else "")
            ).strip()
        size = p.stat().st_size
        if size < 1000:
            logger.warning("see_screen: screenshot is suspiciously small (%d bytes)", size)
            hint = (
                "Grant Screen Recording permission to this Python binary in "
                "System Settings → Privacy & Security → Screen Recording, then retry."
                if IS_MAC
                else "The capture may have failed; retry."
            )
            return (
                f"Screenshot captured at {path} but it is only {size} bytes (likely black/empty). "
                + hint
            )
        logger.info("see_screen: captured %s (%d bytes)", path, size)
        return f"Screenshot saved at {path} ({size:,} bytes). Prompt: {prompt}"
    except Exception as exc:
        logger.error("see_screen exception: %s", exc)
        return f"Error capturing screen: {exc}"


def capture_screen_b64(max_px: int = 1568) -> dict:
    """Capture the screen and return it as base64 PNG, ready to feed a vision model.

    This is the tool that actually lets Jarvis *read* the screen: `see_screen` only
    returns a text path, but this returns the pixels. Downscaled with `sips` so the
    longest edge is <= max_px (1568 = Anthropic's vision sweet spot; bigger is just
    downsampled server-side and wastes tokens).

    Cross-platform: macOS `screencapture`, Windows PowerShell screen grab
    (built-in .NET, no extra deps), graceful failure elsewhere.

    Returns {"ok": True, "media_type": "image/png", "data": <b64>, "bytes": n}
    or      {"ok": False, "error": <reason, incl. permission hint>}.
    """
    path = _screenshot_path()
    try:
        ok, err = _capture_to_file(path)
        if not ok:
            return {"ok": False, "error": err}
        p = Path(path)
        if not p.exists():
            return {"ok": False, "error": (
                "Screenshot file not created"
                + (" — Screen Recording permission likely missing." if IS_MAC else " on this OS.")
            )}
        size = p.stat().st_size
        if size < 1000:
            hint = (
                "grant Screen Recording permission to this Python binary, then retry."
                if IS_MAC
                else "the capture may have failed; retry."
            )
            return {"ok": False, "error": (
                f"Screenshot is only {size} bytes (likely black/empty) — {hint}")}
        # Downscale in place (best-effort, macOS only via `sips`). On other OSes,
        # or if sips fails, send full-res; the API downsamples anyway — it just
        # costs more tokens. No extra pip deps are added for resizing.
        if IS_MAC:
            try:
                _run(["sips", "-Z", str(max_px), path], capture=True)
            except Exception as exc:
                logger.warning("capture_screen_b64: sips resize failed (%s); sending full-res", exc)
        data = base64.b64encode(p.read_bytes()).decode("ascii")
        logger.info("capture_screen_b64: %d b64 chars (target<=%dpx)", len(data), max_px)
        return {"ok": True, "media_type": "image/png", "data": data, "bytes": p.stat().st_size}
    except Exception as exc:
        logger.error("capture_screen_b64 exception: %s", exc)
        return {"ok": False, "error": f"Error capturing screen: {exc}"}


def run_applescript(script: str) -> str:
    """Execute an AppleScript string via osascript (macOS only).

    On Windows/Linux there is no AppleScript, so this returns a friendly
    message instead of crashing.

    Returns:
        stdout from osascript (trimmed), or an error/notice message.
    """
    if not script:
        return "Error: script is required."
    if not IS_MAC:
        logger.info("run_applescript: not supported on this OS (sys.platform=%s)", sys.platform)
        return "AppleScript only works on Mac."
    try:
        result = _run(["osascript", "-e", script], timeout=30)
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        if result.returncode == 0:
            logger.info("run_applescript: ok, output=%r", stdout[:200])
            return stdout or "(AppleScript ran with no output)"
        logger.warning("run_applescript: returncode=%d stderr=%s", result.returncode, stderr)
        return f"AppleScript error: {stderr or 'unknown error'}"
    except Exception as exc:
        logger.error("run_applescript exception: %s", exc)
        return f"Error running AppleScript: {exc}"


def browse_web(url: str) -> str:
    """Open a page with agent-browser and return its accessibility snapshot.

    This is for Jarvis to READ/interact with page content headlessly.
    Separate from open_url (which shows the page on screen in a real browser).

    Requires the `agent-browser` CLI to be installed
    (which it is on this Studio at /opt/homebrew/bin/agent-browser).

    Returns:
        Accessibility snapshot text, or an error message.
    """
    if not url:
        return "Error: URL is required."
    url = _normalise_url(url)
    try:
        # Open the page
        logger.info("browse_web: opening %s", url)
        open_result = _run(["agent-browser", "open", url], timeout=30)
        if open_result.returncode != 0:
            err = (open_result.stderr or open_result.stdout or "").strip()
            return f"agent-browser open failed: {err or 'unknown error'}"

        # Capture snapshot
        logger.info("browse_web: capturing snapshot")
        snap_result = _run(["agent-browser", "snapshot"], timeout=30)
        stdout = (snap_result.stdout or "").strip()
        stderr = (snap_result.stderr or "").strip()

        if snap_result.returncode != 0:
            return f"agent-browser snapshot failed: {stderr or stdout or 'unknown error'}"

        if not stdout:
            return f"agent-browser snapshot returned empty output for {url}."

        logger.info("browse_web: snapshot length=%d chars", len(stdout))
        return stdout[:8000]  # Cap output to avoid flooding context
    except FileNotFoundError:
        return (
            "agent-browser CLI not found. "
            "Install it via: brew install agent-browser"
        )
    except Exception as exc:
        logger.error("browse_web exception: %s", exc)
        return f"Error browsing {url}: {exc}"
