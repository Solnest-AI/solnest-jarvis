# Changelog

## 1.0.1

- **Fixed a silently voiceless JARVIS.** `.env.template` had no `ELEVENLABS_VOICE_ID`
  line, and `jarvis/voice_tts.py` turns TTS off entirely when it is blank. Anyone who
  followed `KEYS.md` (copy `.env.template` to `.env`) paid for an ElevenLabs key and got
  no voice, with only a log warning to explain it. The template now carries the voice id
  and every other setting `.env.example` has, and `setup-keys.sh` / `setup-keys.ps1` warn
  when a key is set with a blank voice id.
- **Fixed the ElevenLabs key being marked required.** `KEYS.md` said Yes; the README and
  the setup wizard both say optional, and Chrome's free voice works without it.
- **Fixed setup instructions from a different product.** `KEYS.md` and the setup-keys
  scripts told you to quit and reopen Claude Code. JARVIS is a local server started with
  `run.sh` / `run.bat`; restarting Claude Code does nothing. They also used `EXA_API_KEY`
  as the worked example, a key this project never reads.
- **Fixed README Step 2.** It told you to unzip a folder, which nobody arriving from
  GitHub has. It now says clone, with the Skool zip as the parenthetical.
- **Added a LICENSE.** MIT, matching the other Solnest repos. Without one the repo was
  all-rights-reserved by default, so nobody could legally fork it.
- **Documented `PERMISSION_MODE=bypassPermissions`** in `.env.example`, so the security
  choice it makes is visible rather than shipped silently.

## 1.0.0

- First public release.
