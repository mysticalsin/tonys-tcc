# Samantha · Loop — Design & Build Spec

**Date:** 2026-06-20
**Owner:** Tony (twalteur@amaris.com)
**Base:** fork of `ethanplusai/samantha-cli` (MIT) → `/Users/tony/samantha-loop`
**Goal:** the "Ultimate loop framework" — a voice-first Claude companion that calls Tony by name, never loses the thread (auto-compacts), drives toward goals, estimates before it builds, and shows a live HUD. ElevenLabs voice.

---

## 1. North star

The listen→think→speak loop *is* the framework. We keep samantha-cli's clean 6-module shape and harden it into a personalized, self-managing loop. Claude runs via `claude -p` (Max subscription, $0 marginal API). Voice out via ElevenLabs (verified working — tier `creator`, full `text_to_speech` permission).

## 2. What samantha-cli already gives us (verified by reading source)

- `cli.py` — Click group, `_run_assistant`, `_conversation_loop` (listen→think→speak). NL exit/clear commands.
- `brain.py` — `Brain` wraps `claude -p`, builds prompt (persona + last 6 msgs), Haiku-summarizes >300 chars, saves session JSON, trims history.
- `voice.py` — `VoiceEngine`: Google STT (SpeechRecognition+PyAudio) + **Fish Audio TTS** (`from fishaudio import FishAudio` — broken import; package is `fish-audio-sdk`). macOS `afplay` playback.
- `personality.py` — static `SYSTEM_PROMPT` (Samantha/Her persona, no name personalization, references Fish Audio).
- `config.py` — `~/.samantha/config.yaml`, `DEFAULTS` (fish_api_key, voice_model_id, speech_speed, language, max_history, listen_timeout, phrase_time_limit), env override `FISH_API_KEY`.
- `ui.py` — Rich UI, `Status` enum, `show_*`. **No HUD.**

## 3. Target architecture

```
samantha/
  cli.py          MOD  loop hardened: goal-aware, estimate gate, HUD each turn, loop modes
  brain.py        MOD  + compaction hooks, + estimator hook, name+goal in prompt, token/cost accounting
  personality.py  MOD  get_system_prompt(user_name, goal) — addresses Tony; ElevenLabs wording
  voice.py        MOD  ElevenLabs TTS (drop Fish); keychain key resolution
  config.py       MOD  new keys (user_name, elevenlabs_*, compact_threshold_tokens, claude_model, summary_model)
  ui.py           MOD  + show_hud(); keep show_* API
  hud.py          NEW  model · effort · git · context-tokens · cost · tokens · speed
  memory.py       NEW  auto-compact: rolling Haiku digest of old turns, keep recent verbatim
  goal.py         NEW  set/track/clear a goal; injected every turn; persisted ~/.samantha/goal.json
  estimator.py    NEW  pre-task token+time estimate on build/project intent
  secrets.py      NEW  key resolution: env ELEVENLABS_API_KEY → macOS Keychain → config
```

## 4. Feature specs

### 4.1 ElevenLabs TTS (replaces Fish Audio)
- `voice.py`: use official `elevenlabs` SDK. `ElevenLabs(api_key).text_to_speech.convert(voice_id, model_id, text, output_format="mp3_44100_128")` → bytes → temp mp3 → existing `_play_audio_file` (afplay/ffplay). REST fallback (verified) if SDK import fails.
- Default voice `21m00Tcm4TlvDq8ikWAM` (Rachel, warm female — verified). 27 voices available; configurable via `tts_voice_id`. Model `eleven_multilingual_v2`. `speech_speed` → `voice_settings.speed`.
- `tts_available` = key resolvable (not just config truthy).

### 4.2 Key handling (security)
- `secrets.py`: `get_elevenlabs_key()` resolution order: env `ELEVENLABS_API_KEY` → macOS Keychain (`security find-generic-password -s samantha-loop -a elevenlabs -w`) → config `elevenlabs_api_key`.
- Key NEVER committed. `.gitignore` covers `.env`, `*.mp3`. Config lives at `~/.samantha/config.yaml` (outside repo, chmod 600). `_mask_secret` already masks display.
- **Tony stores the key himself** (one `!` command, Keychain recommended). App reads it.

### 4.3 Call user by name
- `config.py`: `user_name: "Tony"`.
- `personality.py`: `get_system_prompt(user_name, goal=None)` — "You're talking to {user_name}. Use their name naturally and warmly, not every sentence." Remove "servant/companion" stays; swap Fish→ElevenLabs in the "how you work" section.
- `brain._build_prompt` passes `user_name` + active goal.

### 4.4 Auto-compact on stop (and on threshold)
- `memory.py`: `estimate_tokens(text)≈len//4`. `Brain` holds `self.summary: str`. `_build_prompt` = persona + (summary if any) + recent verbatim + current.
- After each `think`: if estimated context tokens > `compact_threshold_tokens` (default 24000), compact: Haiku-summarize `history[:-keep]` into/onto `self.summary`, drop those messages, keep last `keep` (e.g. 6) verbatim.
- On loop exit (cli `finally`): `brain.compact_and_save()` — final digest written into session JSON (`summary` field). This is mechanical — we own context assembly, so it's real compaction, not a nudge.

### 4.5 Goal tracking (/goal)
- `goal.py`: `Goal{text, created, status, notes[]}`, persisted `~/.samantha/goal.json`.
- CLI: `samantha goal "ship X"` set · `samantha goal` show · `samantha goal done` clear.
- Active goal injected into persona each turn. Drives autonomous loop stop condition.

### 4.6 Loop modes (/loop)
- Default: interactive REPL (current behavior, hardened).
- Opt-in autonomous: `samantha loop "goal"` / `--auto` — after responding, if goal set & not done, self-continue ("continue toward the goal") without waiting for input, bounded by `max_iters` (default 8) and interruptible (Ctrl-C). Stops when goal marked done or cap hit.

### 4.7 Estimate before project
- `estimator.py`: `is_project_intent(text)` (keywords: build, create, implement, make me a, set up, deploy, refactor, scaffold…). On match, BEFORE the real `think`: one cheap Haiku call returns `{tokens_est, minutes_est, steps[]}`; samantha states it first ("Roughly N tokens, ~M min, Tony — starting now."), then proceeds. Honors Tony's standing estimate-first rule.

### 4.8 HUD / statusline
- `hud.py`: `render(brain, voice, last_think_stats)` → one Rich line/panel each turn:
  `🤖 Opus · ⛏ effort · 🌿 <git branch> · 🧠 <ctx tokens>/<threshold> · 💲 API $0 (Max) +TTS ~$x · 📊 <tokens> · ⚡ <tok/s>`
- git branch from `git -C cwd rev-parse --abbrev-ref HEAD`. Context tokens from memory estimate. TTS cost = chars × ElevenLabs rate (creator tier). Honest: claude API $0 (Max sub), surface TTS spend + token counts.
- `ui.show_hud(line)` rendered after each think.

## 5. Dependencies
- Add `elevenlabs`. Remove `fish-audio-sdk`. Keep `SpeechRecognition`, `PyAudio`, `rich`, `click`, `pyyaml`. System: `portaudio` (brew), an audio player (afplay built-in on mac).
- `pyproject.toml` updated. `requests` only if REST fallback needed (SDK bundles its own http).

## 6. Build plan (all-at-once, parallel waves)

- **Wave 0 — already done:** fork scaffolded, key verified, non-secret config written.
- **Wave 1 (parallel, NEW independent modules):** `secrets.py`, `memory.py`, `goal.py`, `estimator.py`, `hud.py`. No shared-file conflicts. Each with a docstring'd interface + a unit test that runs offline (no mic/claude/network).
- **Wave 2 (single coherent integration):** rewrite `config.py`, `personality.py`, `voice.py`, `brain.py`, `cli.py`, `ui.py` to wire all modules together. One agent holds the whole picture to avoid conflicts.
- **Wave 3 (parallel):** `pyproject.toml`, `CLAUDE.md`, `README.md`, `.gitignore`.
- **Wave 4 — verification:** create venv, `pip install -e .`, import-smoke, run offline unit tests, `samantha --text` dry-run with a stubbed/echo brain, ElevenLabs TTS live test (1 short line), report real output. Voice/mic test is manual (needs Tony's mic + key in env/keychain).

## 7. Testing & verification gate
- Offline unit tests: `memory` (compaction math + Haiku-call mocked), `goal` (set/show/clear/persist), `estimator` (intent detection + parse, claude mocked), `hud` (render given fake stats), `secrets` (resolution order with monkeypatched env/keychain).
- Integration smoke: package imports, CLI `--help`, `config` shows masked, `--text` single-turn with `claude` stubbed.
- Live: one ElevenLabs TTS convert → valid mp3 (already proven once; re-confirm post-wiring).
- No "looks right" claims — show real command output.

## 8. Out of scope (YAGNI)
- No caveman plugin/overlay (Tony: "forget caveman").
- No multi-user; single user = Tony.
- No new STT provider (Google free STT stays).
- No GUI; terminal Rich UI only.

## 9. Open items for Tony (non-blocking)
1. Store the ElevenLabs key (Keychain one-liner provided) — needed for live voice; text-mode works without.
2. Voice pick: default Rachel; 27 options if he wants a different "Her" timbre.
