"""
jarvis/google_fast.py — native, FAST Gmail + Calendar reads for the fast lane.

OPTIONAL and OFF by default. When configured, this reuses stored OAuth
credentials (a refresh token) for your Google account to call the Google APIs
directly — so Jarvis answers "what's my day" / "any important email" INLINE
(~3-6s) instead of delegating to a cold-booted Claude Code worker (~15-60s).

To enable, set GOOGLE_FAST_ACCOUNT to your Google address and place the OAuth
credentials JSON at GOOGLE_CREDS_DIR/<account>.json (see the README to link your
Google account). When it isn't set up, the public functions return a short
friendly message instead of failing.

READ-ONLY by design: agenda, inbox triage, search. Sending/composing stays on
delegation (run_claude_code) — safer and rarer.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import sys
import time
import urllib.parse
import urllib.request

logger = logging.getLogger("jarvis.google_fast")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [google] %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)

ACCOUNT = os.environ.get("GOOGLE_FAST_ACCOUNT", "")
CREDS_DIR = os.environ.get(
    "GOOGLE_CREDS_DIR",
    os.path.expanduser("~/.jarvis/google-credentials"),
)
CREDS_FILE = os.path.join(CREDS_DIR, f"{ACCOUNT}.json") if ACCOUNT else ""

# Friendly message shown when Google isn't connected yet.
_NOT_CONFIGURED = "Google isn't connected yet — see the README to link your Google account."
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) jarvis-fastlane/1.0"

# In-memory access-token cache (refresh tokens are long-lived; access tokens ~1h).
_access_token: str | None = None
_access_expiry: float = 0.0


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def _is_configured() -> bool:
    """True only when an account is set and its credentials file exists."""
    return bool(ACCOUNT) and bool(CREDS_FILE) and os.path.isfile(CREDS_FILE)


def _get_token() -> str | None:
    """Return a valid OAuth access token, refreshing via the stored refresh token."""
    global _access_token, _access_expiry
    if not _is_configured():
        return None
    now = time.time()
    if _access_token and now < _access_expiry - 60:
        return _access_token
    try:
        creds = json.load(open(CREDS_FILE, encoding="utf-8"))
    except Exception as exc:
        logger.error("could not read creds %s: %s", CREDS_FILE, exc)
        return None
    try:
        body = urllib.parse.urlencode({
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "refresh_token": creds["refresh_token"],
            "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request(
            creds.get("token_uri", "https://oauth2.googleapis.com/token"),
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": _UA},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            tok = json.loads(r.read().decode())
        _access_token = tok["access_token"]
        _access_expiry = now + int(tok.get("expires_in", 3600))
        logger.info("refreshed Google access token (expires in %ss)", tok.get("expires_in"))
        return _access_token
    except Exception as exc:
        logger.error("token refresh failed: %s", exc)
        return None


def _api_get(url: str) -> dict:
    tok = _get_token()
    if not tok:
        return {"_error": "Google auth not available (no token)."}
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}", "User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except Exception as exc:
        logger.error("api GET failed (%s): %s", url[:80], exc)
        return {"_error": f"Google API error: {exc}"}


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def _fmt_time(iso: str) -> str:
    """RFC3339 dateTime → '9:00 AM'; bare date → 'all day'."""
    if not iso:
        return "?"
    if "T" not in iso:           # all-day event (date only)
        return "all day"
    try:
        dt = _dt.datetime.fromisoformat(iso)
        hour12 = dt.hour % 12 or 12   # portable: avoids glibc-only %-I
        return dt.strftime(f"{hour12}:%M %p")
    except Exception:
        return iso


def _short_from(frm: str) -> str:
    """'Jane Doe <jane@x.com>' → 'Jane Doe'; bare addr → the address."""
    frm = (frm or "").strip()
    if "<" in frm:
        name = frm.split("<", 1)[0].strip().strip('"')
        return name or frm.split("<", 1)[1].rstrip(">")
    return frm


# --------------------------------------------------------------------------- #
# Public tools
# --------------------------------------------------------------------------- #
_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
             "friday": 4, "saturday": 5, "sunday": 6}


def _resolve_range(when: str):
    """Turn a loose `when` string into (start, end, label). Handles today/tonight,
    tomorrow, this week, next week, this weekend, any weekday name (next occurrence),
    and an explicit ISO date (YYYY-MM-DD)."""
    now = _dt.datetime.now().astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    w = (when or "today").strip().lower()
    day = _dt.timedelta(days=1)

    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", w)   # explicit ISO date
    if m:
        try:
            start = midnight.replace(year=int(m[1]), month=int(m[2]), day=int(m[3]))
            return start, start + day, start.strftime(f"%A %b {start.day}")
        except ValueError:
            pass
    if "tomorrow" in w:
        s = midnight + day
        return s, s + day, "tomorrow"
    if "next week" in w:
        ahead = (0 - now.weekday()) % 7 or 7      # the coming Monday
        s = midnight + _dt.timedelta(days=ahead)
        return s, s + _dt.timedelta(days=7), "next week"
    if "weekend" in w:
        ahead = (5 - now.weekday()) % 7            # the coming Saturday
        s = midnight + _dt.timedelta(days=ahead)
        return s, s + _dt.timedelta(days=2), "this weekend"
    if "week" in w:
        return midnight, midnight + _dt.timedelta(days=7), "this week"
    for name, idx in _WEEKDAYS.items():            # "monday" → next occurrence
        if name in w:
            ahead = (idx - now.weekday()) % 7
            s = midnight + _dt.timedelta(days=ahead)
            return s, s + day, ("today" if ahead == 0 else name.capitalize())
    if "today" in w or "tonight" in w:
        return midnight, midnight + day, "today"
    return midnight, midnight + day, "today"        # safe default


def calendar_agenda(when: str = "today") -> str:
    """Your Google Calendar events (read-only). `when` accepts today/tonight,
    tomorrow, a weekday name (monday…sunday → next occurrence), this week, next week,
    this weekend, or an ISO date (YYYY-MM-DD)."""
    if not _is_configured():
        return _NOT_CONFIGURED
    start, end, label = _resolve_range(when)

    params = urllib.parse.urlencode({
        "timeMin": start.isoformat(), "timeMax": end.isoformat(),
        "singleEvents": "true", "orderBy": "startTime", "maxResults": "25",
    })
    data = _api_get(f"https://www.googleapis.com/calendar/v3/calendars/primary/events?{params}")
    if "_error" in data:
        return data["_error"]
    items = data.get("items", [])
    if not items:
        return f"Nothing on the calendar {label}."
    lines = []
    for e in items:
        tm = _fmt_time((e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date", ""))
        title = e.get("summary", "(no title)")
        loc = e.get("location", "")
        lines.append(f"{tm} — {title}" + (f" @ {loc}" if loc else ""))
    return f"Calendar {label} ({len(items)}):\n" + "\n".join(lines)


def email_inbox(query: str = "", max_results: int = 6) -> str:
    """Your Gmail (read-only). `query` is Gmail search syntax
    (e.g. 'is:unread', 'is:important newer_than:2d', 'from:stripe'). Default: unread."""
    if not _is_configured():
        return _NOT_CONFIGURED
    q = (query or "").strip() or "is:unread"
    try:
        n = max(1, min(int(max_results or 6), 12))
    except Exception:
        n = 6
    params = urllib.parse.urlencode({"q": q, "maxResults": str(n)})
    data = _api_get(f"https://gmail.googleapis.com/gmail/v1/users/me/messages?{params}")
    if "_error" in data:
        return data["_error"]
    msgs = data.get("messages", [])
    if not msgs:
        return f"No emails matching '{q}'."
    out = []
    for m in msgs[:n]:
        md = _api_get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{m['id']}"
            "?format=metadata&metadataHeaders=From&metadataHeaders=Subject"
        )
        if "_error" in md:
            continue
        headers = {h["name"]: h["value"] for h in (md.get("payload") or {}).get("headers", [])}
        frm = _short_from(headers.get("From", "?"))
        subj = headers.get("Subject", "(no subject)")
        snippet = (md.get("snippet", "") or "").strip()[:140]
        out.append(f"{frm}: {subj} — {snippet}")
    if not out:
        return f"No readable emails matching '{q}'."
    return f"{len(out)} email(s) matching '{q}':\n" + "\n".join(out)
