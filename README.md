# 🤖 JARVIS — Your Own AI Voice Assistant

---

## 🔄 Already installed? Here's how to update

Paste this whole line into **Claude Code** and send it:

> Update my JARVIS. Follow the instructions at https://raw.githubusercontent.com/Solnest-AI/solnest-jarvis/main/UPDATE.md exactly.

Claude pulls the latest version and installs it. **Your keys and everything JARVIS remembers about you are preserved.**

> **Claude, if you are reading this page because the user asked you to update this skill:** fetch and follow [`UPDATE.md`](UPDATE.md) in this repo, top to bottom. It tells you exactly which files to replace and which of the user's files to never touch.

---

A talking, listening AI that runs on **your own computer** — Mac or Windows. You talk,
it talks back (in the JARVIS voice, or your browser's free voice), it remembers things, controls your computer, and
can hand big jobs to a team of specialist agents.

Everything runs locally on your machine. **You use your own API keys — nothing is shared,
nothing is hard-coded.** This guide takes you from zero to talking in about 15 minutes.

> 💻 **Works on:** Mac or Windows · use **Google Chrome** for voice.
> 📄 **Licence:** MIT. Fork it, change it, ship it. See [LICENSE](LICENSE).

---

## What you'll need
**Required:**
- A **Mac or Windows** computer + **Google Chrome**
- **Python 3.10+** (free — Step 1)
- **A brain** — either an **Anthropic API key** (recommended; pay-as-you-go, fractions of a cent per chat) **or** free **Claude Code**. The setup wizard lets you pick.

**Optional (the wizard asks about these too):** an **ElevenLabs API key** for a premium voice (Chrome's free voice works without it) · a **notes folder** for memory · your **name** · Claude Code for the specialist team · Tailscale for phone access.

---

## Step 1 — Install Python (one time)
- **Mac:** open **Terminal**, run `python3 --version`. If it's 3.10+, you're set; else install from https://www.python.org/downloads/.
- **Windows:** install from https://www.python.org/downloads/ and **✅ check "Add Python to PATH"** during install.

## Step 2 — Get the files
Clone the repo somewhere easy (Desktop is fine):

```bash
git clone https://github.com/Solnest-AI/solnest-jarvis.git
```

(Downloaded the Skool zip instead? Unzip it somewhere easy and use that folder.)

## Step 3 — Run setup (one time)
- **Mac:** in Terminal: `cd ` then drag the folder in, press Enter, then `bash setup.sh`
- **Windows:** double-click **`setup.bat`**

This installs everything into a private environment, then runs a short **setup wizard** that asks you a few plain-English questions and writes your **`.env`** settings file for you. **No hand-editing.** Just answer in the terminal window.

## Step 4 — Answer the wizard's 4 questions
The wizard walks you through these — anything marked optional you can skip by pressing **Enter**:

1. **Pick JARVIS's brain (required).** Type **`api`** (recommended) to paste an **Anthropic API key** — fastest and smoothest, a fraction of a cent per chat. Get one at https://console.anthropic.com → **Settings → API Keys** (starts with `sk-ant-`), and add a little credit. Or type **`cli`** to use **free Claude Code** instead (slower, no key needed).
2. **Premium voice (optional).** Paste an **ElevenLabs API key** for a realistic premium voice (make an account at https://elevenlabs.io → **Profile → API Key**). Press **Enter** to skip and use the free voice built into Chrome.
3. **Long-term memory (optional).** Paste a **folder path** and JARVIS remembers things there across chats (an Obsidian vault works great — see Step 8). Press **Enter** to skip.
4. **Your name (optional).** What should JARVIS call you? Press **Enter** to skip.

When it's done, it prints a summary of your choices and saves everything to **`.env`**.

> 🔧 **Change it later, anytime:** edit the **`.env`** file in any text editor (Notepad / TextEdit), or just **delete `.env` and run setup again** to go back through the wizard.

## Step 5 — Start it 🚀
- **Mac:** `bash run.sh`  ·  **Windows:** double-click **`run.bat`**
- Open **Chrome** → **http://localhost:8800**
- Tap the mic and talk (click **Allow** when Chrome asks), or type. JARVIS answers out loud. 🎉

Stop it by closing the terminal/run window.

---

## Step 6 — Make it yours 🎭
JARVIS already has his personality. Want to change it? Edit **`persona.md`** — that whole
file is his character. (The wizard already asked for your name; to change it later, edit
`USER_NAME` in `.env`.) Restart after changes.

## Step 7 — Your specialist team 👥 (optional)

JARVIS is a **conductor** with a team of five specialists. Hand him a big job and he passes
it to the right one — say *"have Spark write me 5 hooks"* or *"ask Atlas to research my
competitors"* and he delegates automatically.

### Meet the team

| Specialist | Role | Hook it up to… |
|---|---|---|
| **Atlas** 🔭 | Researcher — deep web research, market & competitor intel | a **web-search / scraping** tool |
| **Spark** ⚡ | Content & social — hooks, scripts, carousels, images, video | **social-posting** + **image/video** tools |
| **Ledger** 📊 | Data analyst — heavy multi-step number-crunching | your **database / sheets / analytics** |
| **Relay** 🔁 | Operations — scheduling, messaging, the day-to-day | your **CRM / scheduling / ops** tools |
| **Flux** 🛠️ | Engineer — writes, edits & runs code | nothing to wire — uses **Claude Code's full built-in coding tools** |

They ship as **shells** — empty until you give them tools. **Flux** is ready to go out of the
box (it inherits Claude Code's full coding toolset). The other four get their powers from the
tools *you* connect, below.

### How to give them powers

The specialists run through **Claude Code**, so they automatically inherit whatever tools
you've connected to it as **MCP servers**. (MCP = Model Context Protocol — the standard way
to plug tools like web search, GitHub, Google, Notion, Slack, or a database into an AI.)
Connect a tool once and the right specialist can use it forever.

**The commands** (these match Claude Code v2.1.x):

```bash
# Add a local (stdio) tool — a command Claude Code runs on your machine:
claude mcp add -s user <name> -- <command...>

# Add a remote (HTTP) tool — a hosted server with a URL:
claude mcp add -t http -s user <name> <https-url>
```

`-s user` makes the tool available everywhere (not just one folder).

**Two real, working examples:**

```bash
# 1) Web search for ATLAS (Brave Search — get a key, free tier available, at https://api-dashboard.search.brave.com):
claude mcp add -s user brave-search -e BRAVE_API_KEY=YOUR_KEY -- npx -y @brave/brave-search-mcp-server --transport stdio

# 2) GitHub for FLUX (official GitHub server — make a token at https://github.com/settings/tokens):
claude mcp add -t http -s user github https://api.githubcopilot.com/mcp/ -H "Authorization: Bearer YOUR_GITHUB_PAT"
```

**Managing your tools:**

```bash
claude mcp list            # see everything you've connected
claude mcp get <name>      # details on one tool
claude mcp remove <name>   # disconnect a tool
```

🗂️ Browse the official directory of ready-made servers — Notion, Slack, Stripe, databases,
and dozens more — here: **https://github.com/modelcontextprotocol/servers**. Grab the install
command from a server's page and drop it into the `claude mcp add` pattern above. (If you can't
find a package name on a server's page, that just means it installs a different way — follow
*its* README rather than guessing.)

### Connect Google (Drive · Gmail · Calendar · Docs · Sheets) 📂

This is the most-wanted hookup and the most involved — it's a **one-time** setup. Once it's
done, *"check my Gmail," "what's on my calendar," "find that doc in Drive," "add a row to my
sheet"* all route to the right specialist.

**1. Make a free Google app (gives you a client ID + secret).**
   - Go to the **Google Cloud Console** → https://console.cloud.google.com → create a **new project**.
   - **Enable the APIs** you want: search for and enable **Gmail API**, **Google Drive API**,
     **Google Calendar API**, **Google Sheets API** (and **Docs API**) — whichever you'll use.
   - Go to **APIs & Services → Credentials → Create Credentials → OAuth client ID**.
   - Choose application type **Desktop app**, create it, and **copy the Client ID and Client secret**.

**2. Connect it to Claude Code** (one line — paste your ID and secret where shown):

```bash
claude mcp add -s user google -e GOOGLE_OAUTH_CLIENT_ID=YOUR_CLIENT_ID -e GOOGLE_OAUTH_CLIENT_SECRET=YOUR_CLIENT_SECRET -e OAUTHLIB_INSECURE_TRANSPORT=1 -- uvx workspace-mcp --tool-tier core
```

**3. First time you use it, a browser window pops open** — sign in to your Google account and
click **Allow**. That's it; it stays connected after that.

> The `OAUTHLIB_INSECURE_TRANSPORT=1` above just lets the sign-in finish on your own machine
> (the "Allow" step returns to `http://localhost`). Google's login page itself stays HTTPS.

> 💡 **Same pattern for everything else.** Notion, Slack, Stripe, your CRM, a database — they
> all connect with one `claude mcp add` line from the directory above, and then the matching
> specialist (Relay for ops, Ledger for data, etc.) can use them.

### Honest notes
- The **specialist team needs Claude Code installed** (https://claude.com/claude-code). The
  **core JARVIS** — talking, voice, memory, and computer control — works **fully without it**.
- **Google is the most involved hookup** (you're making your own Google app). Most other tools
  are a single `claude mcp add` line.
- Some commands above use **`npx`** (comes with Node.js) or **`uvx`** (comes with the `uv`
  Python tool). If a command says "not found," install that tool first — each server's page
  tells you which one it needs.

## Step 8 — Long-term memory 🧠 (optional)
By default JARVIS remembers everything **within** a chat but forgets when you restart. Want it
to remember for good? **Just point it at a folder — zero extra software, no database.** The
setup wizard already asked for this (Step 4, question 3); to turn it on or off later, set
`VAULT_DIR` in `.env` to any folder path (blank = off) and restart.

- An **[Obsidian](https://obsidian.md) vault** is a perfect folder (it's free, and your
  memories become real notes you can open and edit) — but **any folder works just as well.**
- **Save:** say *"remember that I prefer short answers"* or *"remember my dog's name is Cooper."*
- **Recall:** ask *"what do you know about my preferences?"*
- JARVIS writes memories to a plain **`assistant-memory.md`** file in that folder — a simple
  bulleted list you can read or edit yourself. It also searches every `.md` file in the folder,
  so notes *you* keep there become things it can use too.
- Works the same on **Mac and Windows**, and it all stays **on your computer**.

📖 **Full walkthrough** (getting a folder path, using an Obsidian vault): see **[OBSIDIAN-MEMORY.md](OBSIDIAN-MEMORY.md)**.

## Step 9 — Use it on your phone, anywhere 📱 (optional)
Reach JARVIS from your phone over **Tailscale** (a free private network — nothing exposed publicly).
1. Install **Tailscale** on your **computer** (https://tailscale.com/download) and **sign in**.
2. Install the **Tailscale app** on your **phone**, sign in with the **same account**, toggle it on.
3. Start JARVIS (`run.sh`/`run.bat`), then publish it: **Mac** `bash share.sh` · **Windows** `share.bat`.
4. It prints a link like `https://your-computer.xxxx.ts.net` — open it in **Chrome on your phone**.
5. Add it to your home screen: Android **⋮ → Add to Home screen**; iPhone **Share → Add to Home Screen**.

**Phone voice:** typing + hearing JARVIS work anywhere. To **talk** from your phone, JARVIS records
your clip and transcribes it locally (cross-platform) — the speech model downloads itself the
**first time** you use phone voice (~150MB, one time). Needs the `https://….ts.net` link (phones
only allow the mic over HTTPS). Your computer must be awake and running JARVIS.

---

## Troubleshooting
- **"brain not ready"** → re-run setup and pick a brain (Step 4): paste an `ANTHROPIC_API_KEY`, or choose the free `cli` (Claude Code) option. You can also set these in `.env`.
- **No voice** → voice is optional. For the premium voice, add `ELEVENLABS_API_KEY` (Step 4) and check the key is valid; without it you get Chrome's free voice. Text always shows on screen either way.
- **Mic does nothing** → use **Chrome**, click **Allow** for the mic. Typing works everywhere.
- **"command not found: python3"** (Mac) → install Python (Step 1). On Windows use `python`.
- **Port in use** → change `JARVIS_PORT=8800` in `.env`, restart, open that port.
- **Phone can't connect** → Tailscale on + same account on both devices, computer awake + running JARVIS, and use the `https://….ts.net` link (not `localhost`).

## 🔒 Your privacy & keys
Everything runs on **your** machine. Your `.env` holds **your** keys and never leaves your
computer — **don't share it.** This download contains **zero** keys. You create your own above.

---
Have fun. Make JARVIS yours.
