# Your keys — where they go

## The rule: **never paste a key into the chat.**

Anything you type into the chat is saved in the conversation forever. Put your keys in the **`.env`** file instead. Claude never reads that file — a setup script hands your keys straight to the tool, so they stay yours.

---

## What to do (2 minutes)

1. **Open the file named `.env`** in this folder. Any text editor works — Notepad, TextEdit, VS Code.
   *(Don't see it? It might be hidden. On Mac press `Cmd+Shift+.` in Finder. If there's only a `.env.template`, make a copy of it and name the copy `.env`.)*

2. **Paste each key after the `=` sign.** No quotes, no spaces:

   ```
   ANTHROPIC_API_KEY=sk-ant-abc123xyz
   ```
   not
   ```
   ANTHROPIC_API_KEY = "sk-ant-abc123xyz"
   ```

3. **Save the file.**

4. **Check it.** Run `bash setup-keys.sh` (Mac) or `powershell -File setup-keys.ps1` (Windows). It reports which keys it found without ever printing them. Don't paste anything into the chat.

5. **Start JARVIS.** Mac: `bash run.sh`. Windows: double-click `run.bat`. He reads `.env` on startup, so restart him after any change. There is nothing to restart in Claude Code. JARVIS is his own local server.

---

## The keys you need

| Key | What it's for | Required? | Where to get it |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | JARVIS's brain | Yes | https://console.anthropic.com |
| `ELEVENLABS_API_KEY` | JARVIS's premium voice | Optional. Chrome's free voice works without it | https://elevenlabs.io |
| `ELEVENLABS_VOICE_ID` | Which voice he speaks in | Only if you set the key above. Comes pre-filled with the JARVIS voice. **Do not leave it blank.** A blank voice id turns the premium voice off with no error, so JARVIS goes quiet and only the log says why | your ElevenLabs voice library |
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
