# Your keys — where they go

## The rule: **never paste a key into the chat.**

Anything you type into the chat is saved in the conversation forever. Put your keys in the **`.env`** file instead. Claude never reads that file — a setup script hands your keys straight to the tool, so they stay yours.

---

## What to do (2 minutes)

1. **Open the file named `.env`** in this folder. Any text editor works — Notepad, TextEdit, VS Code.
   *(Don't see it? It might be hidden. On Mac press `Cmd+Shift+.` in Finder. If there's only a `.env.template`, make a copy of it and name the copy `.env`.)*

2. **Paste each key after the `=` sign.** No quotes, no spaces:

   ```
   EXA_API_KEY=abc123xyz
   ```
   not
   ```
   EXA_API_KEY = "abc123xyz"
   ```

3. **Save the file.**

4. **Tell Claude "I've filled in the .env"** — and don't paste anything into the chat. Claude runs the setup script, which reads the file and connects everything.

5. **Fully quit and reopen Claude Code.** It only picks up new keys on startup.

---

## The keys you need

| Key | What it's for | Required? | Where to get it |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | JARVIS's brain | Yes | https://console.anthropic.com |
| `ELEVENLABS_API_KEY` | JARVIS's voice | Yes | https://elevenlabs.io |
| `VAULT_DIR` | Folder for JARVIS's memories (optional — a path, not a key) | Optional | any notes folder |
| `SUPABASE_ACCESS_TOKEN` | Your own Supabase (optional) | Optional | https://supabase.com/dashboard/account/tokens |
| `SUPABASE_PROJECT_REF` | Your Supabase project ref (optional) | Optional | Supabase project settings |

---

## Keeping your keys safe

- ✅ Keys live in `.env` on **your** computer. They never get uploaded.
- ❌ **Never** paste a key into a chat, a screenshot, a Skool post, or GitHub.
- ❌ **Never** commit `.env` (it's already in `.gitignore`).
- 🔄 **If you ever leak a key, rotate it.** Go to that service's dashboard and regenerate it. Takes 10 seconds and makes the old one useless.

**Already pasted a key into a chat by accident?** Rotate it now. Don't panic — just regenerate it and paste the new one into `.env` instead.
