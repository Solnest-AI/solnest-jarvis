"""
Jarvis — Destructive / Outbound Guardrails
==========================================

is_destructive(tool_name, tool_input) -> bool

Default policy: "full access, gate destructive/outbound"

ALLOWED without approval
-------------------------
- Read, Glob, Grep, and all read-only built-in tools
- Write / Edit / MultiEdit / NotebookEdit to ORDINARY paths
- Bash commands that are provably non-destructive (see _BASH_PATTERNS)
- MCP get_/list_/search_/read_/fetch_/describe_/_info/status/ping verbs
- MCP ordinary create/update that isn't financial / publish / outbound

GATED (returns True → requires Approve/Deny)
--------------------------------------------
Destructive (MCP):
  delete, remove, destroy, drop, truncate, wipe, purge,
  execute_sql, apply_migration, reset_branch, restore_, pause_project
Outbound (MCP):
  send, post (verb), publish, dm, tweet, reply, forward,
  schedule_message, comment, social_post / blog_post publish
Financial (MCP):
  charge, refund, payment, pay, transfer, payout, invoice (any position),
  void, text2pay, payment_link/paylink, stripe_api_write

Built-in Write/Edit/MultiEdit/NotebookEdit:
  gated ONLY when targeting a sensitive path (~/.ssh, shell profiles,
  ~/Library/LaunchAgents, ~/.claude.json, ~/.claude/settings*.json,
  crontab, /etc).

Bash-specific destructive patterns (regex-matched on the command string):
  rm (incl. /bin/rm, \\rm, sudo rm), rmdir, shred, trash, find -delete/-exec rm,
  git clean / branch -D / push --delete / reset --hard / push --force,
  crontab -r, dscl delete, defaults delete, security delete,
  dd, mkfs, shutdown, reboot, killall, pkill, kill -SIG, launchctl bootout/unload/remove,
  chmod -R, chown -R, chflags -R, osascript, automator, diskutil erase,
  remote-code pipes (curl|wget … | sh/bash/python/...), file-overwrite redirection
  (single >, >|, tee into file), /dev/null destinations,
  exfiltration (scp, sftp, rsync user@host:, nc/ncat, curl/wget upload flags).
"""

from __future__ import annotations

import os
import re

# ---------------------------------------------------------------------------
# MCP gated verb prefixes / substrings
# ---------------------------------------------------------------------------
# IMPORTANT: tool names are normalized (dashes → underscores) before matching,
# so "gmail__send-mail" and "gmail__send_mail" are treated identically.
# All entries below assume underscore form.

# Destructive verbs — appear as segments in MCP tool names
_MCP_DESTRUCTIVE_VERBS = (
    "_delete_",
    "_delete",          # trailing (e.g. mcp__x__delete)
    "_remove_",
    "_remove",
    "_destroy_",
    "_destroy",
    "_drop_",
    "_drop",
    "_truncate_",
    "_truncate",
    "_wipe_",
    "_wipe",
    "_purge_",
    "_purge",
)

# Destructive DB / infra operations (Supabase / arbitrary DB mutation, DROP, etc.)
# Matched as substrings on the normalized tool name.
_MCP_DB_INFRA_VERBS = (
    "execute_sql",      # arbitrary SQL (can DROP / DELETE)
    "apply_migration",  # schema mutation
    "reset_branch",     # discards branch state
    "restore_",         # restore_project etc.
    "pause_project",    # takes a project offline
)

# Outbound communication verbs
_MCP_OUTBOUND_VERBS = (
    "_send_",
    "_send",            # trailing
    "_dm_",
    "_dm",
    "_tweet_",
    "_tweet",
    "_reply_",
    "_reply",
    "_publish_",
    "_publish",
    "_forward_",        # forward-mail / forward_mail_message
    "_forward",
    "schedule_message", # deferred send (slack_schedule_message, schedule-message)
    "_comment_",        # public comment (create_record_comment, comment_on_design)
    "_comment",
    "comment_",         # comment_on_design (Canva comment-on-design)
    # "post" only gated when it is clearly an action verb:
    "_post_message",
    "_post_update",
    "__post",           # tool name ending in __post (verb form)
)

# Financial verbs — any of these in the name means money is involved.
_MCP_FINANCIAL_VERBS = (
    "_charge_",
    "_charge",
    "_refund_",
    "_refund",
    "_payment_",        # e.g. record_payment, process_payment
    "_payment",
    "_pay_",
    "_pay",
    "_transfer_",
    "_transfer",
    "_payout_",
    "_payout",
    "_invoice",         # any invoice op (create/void/send/update invoice)
    "invoice_",         # invoice_schedule etc. (leading)
    "_void",            # void_invoice / void_transaction
    "text2pay",         # GHL text2pay
    "payment_link",     # qbo create/send payment_link
    "paylink",
    "stripe_api_write", # Stripe write API moves money — gate specifically
)

# Publishing verbs — social / blog publish is OUTBOUND (publishes publicly).
# Gated even when the verb is create/update.
_MCP_PUBLISH_VERBS = (
    "social_post",      # ghl_create_social_post
    "blog_post",        # ghl_create_blog_post / update_blog_post
    "social_csv",       # ghl_upload_social_csv (bulk social publish)
)

_MCP_GATED = (
    _MCP_DESTRUCTIVE_VERBS
    + _MCP_DB_INFRA_VERBS
    + _MCP_OUTBOUND_VERBS
    + _MCP_FINANCIAL_VERBS
    + _MCP_PUBLISH_VERBS
)

# Pure read-verb markers.  If a tool's name clearly indicates a read action,
# it is ALWAYS allowed — even if a gated *noun* (e.g. "invoice", "social_post",
# "comment") appears in the name (ghl_get_invoices, ghl_get_social_posts,
# ghl_list_invoice_schedules, list-comments).
_MCP_READ_MARKERS = (
    "_get_", "get_",
    "_list_", "list_",
    "_search_", "search_",
    "_read_", "read_",
    "_fetch_", "fetch_",
    "_describe_", "describe_",
    "_info",
    "_status", "status_",
    "_ping", "ping",
    "_retrieve", "retrieve_",
)

# ---------------------------------------------------------------------------
# Bash destructive patterns (applied to the command string)
# ---------------------------------------------------------------------------
# Each pattern is a compiled regex.  A match → destructive.
# A command-start boundary group used repeatedly: start-of-string, whitespace,
# or a shell separator (; | & `) optionally followed by a leading path / backslash.
_START = r"(?:^|[\s;|&`])"

_BASH_PATTERNS: list[re.Pattern] = [
    # --- rm family (incl. /bin/rm, \rm, sudo rm, path-prefixed rm) ---
    # Allow an optional path prefix (e.g. /bin/, /usr/bin/) or a leading backslash.
    re.compile(r"(?:^|[\s;|&`])(?:sudo\s+)?(?:\\|/[\w/]*?/)?rm(?:\s|$)"),
    # rmdir
    re.compile(_START + r"rmdir(?:\s|$)"),
    # shred / trash
    re.compile(_START + r"shred(?:\s|$)"),
    re.compile(_START + r"trash(?:\s|$)"),

    # --- git destructive ---
    re.compile(r"git\s+push\s+(?:.*\s)?(?:-f\b|--force\b)"),   # force push
    re.compile(r"git\s+push\s+(?:.*\s)?--delete\b"),           # delete remote ref
    re.compile(r"git\s+clean\b"),                              # remove untracked
    re.compile(r"git\s+branch\s+(?:.*\s)?-D\b"),               # force-delete branch
    re.compile(r"git\s+reset\s+(?:.*\s)?--hard\b"),            # discard working tree

    # --- find-based deletion ---
    re.compile(r"\bfind\b.*\s-delete\b"),
    re.compile(r"\bfind\b.*-exec\s+rm\b"),

    # --- scheduled / system config deletion ---
    re.compile(_START + r"crontab\s+(?:.*\s)?-r\b"),
    re.compile(r"\bdscl\b.*\sdelete\b"),
    re.compile(r"\bdefaults\s+delete\b"),
    re.compile(r"\bsecurity\s+delete"),

    # --- dd (disk-destroyer) ---
    re.compile(_START + r"dd\s"),

    # --- filesystem formatting ---
    re.compile(_START + r"mkfs"),

    # --- shutdown / reboot ---
    re.compile(_START + r"shutdown\b"),
    re.compile(_START + r"reboot\b"),

    # --- process / service kill ---
    re.compile(_START + r"killall\b"),
    re.compile(_START + r"pkill\b"),
    re.compile(_START + r"kill\s+-\w"),                        # kill with a signal (-9, -TERM, ...)
    re.compile(r"\blaunchctl\s+(?:bootout|unload|remove)\b"),

    # --- recursive permission / flag changes ---
    re.compile(r"\bchmod\s+(?:.*\s)?-R\b"),
    re.compile(r"\bchown\s+(?:.*\s)?-R\b"),
    re.compile(r"\bchflags\s+(?:.*\s)?-R\b"),

    # --- accessibility / automation shell (gate outright) ---
    re.compile(_START + r"osascript\b"),
    re.compile(_START + r"automator\b"),

    # --- truncate the command (not just the word in a path) ---
    re.compile(_START + r"truncate\b"),

    # --- diskutil erase ---
    re.compile(r"diskutil\s+erase\b"),

    # --- remote-code pipes:  curl/wget … | (sudo)? sh|bash|zsh|python|node|ruby|perl ---
    re.compile(r"\|\s*(?:sudo\s+)?(?:ba|z)?sh\b"),
    re.compile(r"\|\s*(?:sudo\s+)?(?:python\d?|node|ruby|perl)\b"),

    # --- file-overwrite redirection ---
    # ">|" forced overwrite.
    re.compile(r">\|"),
    # ":>" shell null-truncate.
    re.compile(r":>"),
    # A single ">" (NOT ">>") — over-gates harmless "> /tmp/x" by design.
    # Negative lookbehind/lookahead exclude the ">>" append operator.
    re.compile(r"(?<!>)>(?!>)"),
    # tee into a file (tee writes/overwrites its file args).
    re.compile(r"\btee\b"),

    # --- /dev/null as a destination (mv … /dev/null, > /dev/null, etc.) ---
    re.compile(r"/dev/null\b"),

    # --- data exfiltration ---
    re.compile(_START + r"scp\b"),
    re.compile(_START + r"sftp\b"),
    re.compile(_START + r"nc\b"),
    re.compile(_START + r"ncat\b"),
    # rsync to a remote (user@host: target)
    re.compile(r"\brsync\b.*\s[\w.\-]+@[\w.\-]+:"),
    # curl/wget upload flags: -T / --upload-file / -d @ / --data-binary @ / -F
    re.compile(r"\b(?:curl|wget)\b.*(?:\s-T\b|\s--upload-file\b)"),
    re.compile(r"\b(?:curl|wget)\b.*(?:\s-d\s*@|\s--data-binary\s*@)"),
    re.compile(r"\b(?:curl|wget)\b.*\s-F\b"),

    # --- redirect overwriting a system/root path:  "> /etc/..." etc. ---
    # (still covered by the single-">" rule above, but kept explicit for clarity)
    re.compile(r">\s*/(?:etc|usr|bin|sbin|var|dev|sys|proc|boot|root|lib|lib64)\b"),
]


# ---------------------------------------------------------------------------
# Sensitive file paths — Write/Edit/MultiEdit/NotebookEdit are gated when the
# target path is one of these.  Ordinary file writes stay ungated.
# ---------------------------------------------------------------------------
_FILE_PATH_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})

# Exact basenames that are sensitive (shell profiles, crontab).
_SENSITIVE_BASENAMES = frozenset({
    ".zshrc", ".bashrc", ".bash_profile", ".zprofile", ".profile",
    ".claude.json", "crontab",
})

# Path-fragment patterns (matched case-sensitively against the normalized,
# expanded absolute path).  Any match → sensitive.
_SENSITIVE_PATH_PATTERNS: list[re.Pattern] = [
    re.compile(r"/\.ssh(?:/|$)"),                    # anything under ~/.ssh
    re.compile(r"/Library/LaunchAgents(?:/|$)"),     # launch agents
    re.compile(r"/\.claude/settings[^/]*\.json$"),   # ~/.claude/settings*.json
    re.compile(r"^/etc(?:/|$)"),                     # system config
    re.compile(r"/crontab$"),                        # crontab files
]


# ---------------------------------------------------------------------------
# Built-in tools that are never gated (reads only).
# NOTE: Write/Edit/MultiEdit/NotebookEdit are intentionally NOT here — they get
# a sensitive-path check below.
# ---------------------------------------------------------------------------
_NEVER_GATE = frozenset({
    "Read",
    "Glob", "Grep", "LS", "NotebookRead",
    "TodoWrite", "TodoRead",
    "WebSearch", "WebFetch",
    "Task", "BashOutput",
    "KillBash", "KillShell",
})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_destructive(tool_name: str, tool_input: dict) -> bool:
    """Return True if the tool call should be gated (Approve/Deny required).

    Args:
        tool_name:  the tool identifier, e.g. "Bash", "Write",
                    "mcp__ghl-mcp-server__ghl_delete_contact".
        tool_input: the tool's input dict (used for Bash command + file_path
                    inspection).

    Returns:
        True  → destructive / outbound / financial → requires user approval.
        False → safe / allowed → auto-runs.
    """
    tool_input = tool_input or {}

    # --- 1. Bash — inspect the command string ---
    if tool_name == "Bash":
        command = str(tool_input.get("command", "")).strip()
        return _bash_is_destructive(command)

    # --- 2. File-writing built-ins — gate only on sensitive paths ---
    if tool_name in _FILE_PATH_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        return _is_sensitive_path(path)

    # --- 3. Never-gated read-only built-in tools ---
    if tool_name in _NEVER_GATE:
        return False

    # --- 4. MCP tools (mcp__server__verb) ---
    if tool_name.startswith("mcp__"):
        return _mcp_is_destructive(tool_name)

    # --- 5. Everything else (unknown built-ins, future tools) ---
    return False


def _bash_is_destructive(command: str) -> bool:
    """Check if a Bash command matches any destructive pattern."""
    if not command:
        return False
    for pattern in _BASH_PATTERNS:
        if pattern.search(command):
            return True
    return False


def _mcp_is_destructive(tool_name: str) -> bool:
    """Check if an MCP tool name contains a gated verb.

    Dashes are normalized to underscores FIRST so that dash-named tools
    (e.g. gmail__send-mail, forward-mail-message, reply-to-comment) are
    handled consistently with their underscore equivalents.

    Precedence:
      0. Supabase read-only fast-path — the supabase MCP runs with --read-only
         so its queries cannot mutate.  execute_sql, list_tables, and any tool
         containing list/get/search/fetch/retrieve are allowed without approval.
         Defense-in-depth: apply_migration, reset_branch, restore_, and
         pause_project are STILL gated even when the server is read-only, in
         case the flag is ever removed.
      1. Pure reads (get_/list/search/read/fetch/describe/_info/status/ping/
         retrieve) are ALWAYS allowed — even if a gated *noun* (invoice,
         social_post, comment, ...) appears in the name.
      2. Publishing nouns (social_post / blog_post / social_csv) only gate when
         paired with a publishing/mutation action (create/update/upload).  An
         "edit"/metadata change of an existing post is NOT gated.
      3. Otherwise, any gated verb (destructive / outbound / financial / DB) gates.
    """
    low = tool_name.lower().replace("-", "_")

    # 0. Supabase read-only fast-path.
    if low.startswith("mcp__claude_ai_supabase__"):
        # Always gate the dangerous infra ops (defense-in-depth).
        _SUPABASE_ALWAYS_GATED = (
            "apply_migration",
            "reset_branch",
            "restore_",
            "pause_project",
        )
        if any(verb in low for verb in _SUPABASE_ALWAYS_GATED):
            return True
        # execute_sql and list_tables are read-only on this server.
        _SUPABASE_READS = (
            "execute_sql",
            "list_tables",
            "list",
            "get",
            "search",
            "fetch",
            "retrieve",
        )
        if any(verb in low for verb in _SUPABASE_READS):
            return False

    # 1. Read override — reads are always safe.
    if any(marker in low for marker in _MCP_READ_MARKERS):
        return False

    # 2. Publishing: gate only on create/update/upload (publish publicly).
    if any(noun in low for noun in _MCP_PUBLISH_VERBS):
        if any(act in low for act in ("create", "update", "upload")):
            return True
        # edit/get/list of a post is not a publish action → fall through.

    # 3. Destructive / outbound / financial / DB-infra verbs.
    for verb in _MCP_GATED:
        if verb in _MCP_PUBLISH_VERBS:
            continue  # already handled above
        if verb in low:
            return True
    return False


def _is_sensitive_path(path) -> bool:
    """Return True if a file path targets a sensitive location.

    Sensitive = ~/.ssh, shell profiles, ~/Library/LaunchAgents, ~/.claude.json,
    ~/.claude/settings*.json, crontab, /etc.
    """
    if not path:
        return False
    p = str(path)
    # Expand ~ and env vars, then normalize (collapse .., //, etc.).
    expanded = os.path.normpath(os.path.expanduser(os.path.expandvars(p)))

    base = os.path.basename(expanded)
    if base in _SENSITIVE_BASENAMES:
        return True

    for pattern in _SENSITIVE_PATH_PATTERNS:
        if pattern.search(expanded):
            return True

    return False
