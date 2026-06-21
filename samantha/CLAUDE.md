# Samantha · Loop

## What This Is
A voice wrapper around Claude Code, hardened into a self-managing loop. The user speaks, Claude thinks (full Opus), and Samantha responds with an ElevenLabs voice — calling the user by name, auto-compacting context so the thread is never lost, tracking a goal, estimating before she builds, and printing a live HUD each turn. Same Claude brain, same tools, same Max subscription. Persona inspired by the AI from *Her* (2013).

Fork of [`ethanplusai/samantha-cli`](https://github.com/ethanplusai/samantha-cli) (MIT), extended with ElevenLabs voice, call-by-name, auto-compaction, goal tracking, a loop mode, an estimate gate, and a HUD.

## API Keys Needed
- **ElevenLabs API key** — for Samantha's voice (TTS). Get one at https://elevenlabs.io. Text mode works without it.
- **NO Anthropic API key needed** — this uses `claude -p` (Claude CLI) on the user's Claude Max/Pro subscription. Zero API cost for the AI.

## First-Time Setup
Walk the user through this step by step on first run:

1. **Check Claude CLI is installed**: run `which claude`. If not found → install Claude Code from https://claude.ai/download
2. **Check Python**: need 3.10+. Run `python3 --version`.
3. **Install the package**:
   - Text + voice-out only (no mic, no system deps): `pip install -e .`
   - With microphone: `brew install portaudio` (macOS) or `sudo apt install portaudio19-dev` (Linux), then `pip install -e ".[voice-input]"`
4. **Store the ElevenLabs key** (Keychain recommended — run `samantha config !` to print this exact line):
   ```bash
   security add-generic-password -s samantha-loop -a elevenlabs -w <your-key>
   ```
   Or use the environment variable: `export ELEVENLABS_API_KEY=your_key`
5. **Test it**: run `samantha --text` first to verify Claude + TTS work without needing a mic.
6. **Go voice**: run `samantha` for full voice mode (requires the `voice-input` extra).

## How to Check if Already Set Up
- Run `samantha config` — shows current config and reports the ElevenLabs key as `resolved` or `not set` (it never prints the key).
- Config file lives at `~/.samantha/config.yaml`.
- If `claude` CLI is not installed → tell them to install Claude Code first.
- **They do NOT need an Anthropic API key** — `claude -p` uses their existing subscription.

## Key Resolution (important)
The ElevenLabs key is resolved by `samantha/secrets.py` in this order — first non-empty wins:
1. Environment variable `ELEVENLABS_API_KEY`
2. macOS Keychain — `security find-generic-password -s samantha-loop -a elevenlabs -w`
3. `~/.samantha/config.yaml` value `elevenlabs_api_key` (lowest priority fallback)

The key is **never** committed. `.gitignore` covers `.env`, `*.key`, `*.mp3`, `*.wav`. TTS is considered available only when a key actually resolves — not merely because a config field is truthy. The user stores the key themselves; the app only reads it.

## How It Works
```
Voice → SpeechRecognition (Google free STT) → claude -p (Opus, Max sub)
                                                   ↓
                                  Haiku summarizes long responses for voice
                                                   ↓
                                  ElevenLabs TTS (Rachel) → afplay/ffplay
                                                   ↓
                                  auto-compact older turns · HUD each turn
```

## Module Map
- `samantha/cli.py` — Click CLI. Bare `samantha` = interactive loop; subcommands `goal`, `loop`, `resume`, `config`. Runs the estimate gate → think → show → speak → HUD per turn; compacts and saves on exit.
- `samantha/brain.py` — wraps `claude -p`. Owns context assembly (persona + rolling summary + recent verbatim + current turn), name + active goal in the prompt, Haiku voice-summary of long responses, auto-compaction, estimate hook, and per-turn token/throughput stats.
- `samantha/voice.py` — `VoiceEngine`: Google STT (mic) + audio playback (macOS `afplay` / Linux `ffplay`/`mpv`/`aplay`). TTS is **delegated** to a pluggable backend from `tts.resolve_backend(provider, settings)` — it no longer hardcodes ElevenLabs. `generate_audio(text)` calls `backend.synth`; `tts_available` is `backend is not None and backend.available`; `chars_spoken` reads off the backend; `backend_note` carries any fallback explanation.
- `samantha/tts/` — pluggable TTS package. `__init__.py` defines the `TTSBackend` protocol, `TTSUnavailable`, and the `get_backend` / `resolve_backend` factory (unknown/unavailable → falls back to ElevenLabs → `None`). `elevenlabs_backend.py` = cloud MP3 (SDK + stdlib REST fallback; key via `secrets`). `miso_backend.py` = local MisoTTS 8B WAV, **all torch/torchaudio/`generator` imports lazy**.
- `samantha/personality.py` — `get_system_prompt(user_name, goal)`: addresses the user by name, carries the active goal, describes the pipeline in **provider-neutral** terms (she may mention "my voice" without naming a vendor).
- `samantha/config.py` — `~/.samantha/config.yaml`, `DEFAULTS`. Does NOT read the key from env (that's `secrets`'s job, kept in one place).
- `samantha/secrets.py` — ElevenLabs key resolution (env → Keychain → config) + `store_key_command()` for the `config !` one-liner.
- `samantha/memory.py` — pure, side-effect-free compaction math. `estimate_tokens`, `context_tokens`, `should_compact`, `compact` (summarizer is injected so it's mockable).
- `samantha/goal.py` — single active goal at `~/.samantha/goal.json`. Set/show/clear/mark-done/add-note; `inject_text` renders it into the persona.
- `samantha/estimator.py` — `is_project_intent` (keyword classifier), `estimate` (one injected Haiku call, robust JSON parsing + heuristic fallback), `speak_line` (the spoken estimate).
- `samantha/hud.py` — pure `render(stats)` → one Rich line. `git_branch` (fails soft) and `tts_cost` (per-tier ElevenLabs rates).
- `samantha/ui.py` — Rich terminal UI. `Status` enum + `show_*`, including `show_hud`.

## Key Details
- **TTS backends** (pluggable): `tts_provider` picks the voice. `elevenlabs` (default, cloud, fast, paid per char) or `miso` (local MisoTTS 8B — free/private but ~35 GB weights, wants a 24 GB CUDA GPU; on Apple Silicon MPS is unsupported so it falls back to CPU and is very slow/not real-time). Override per run with `samantha --tts elevenlabs|miso`. Unknown/unavailable backend falls back to ElevenLabs, then to text-only; the fallback note is surfaced via `ui.show_info`. The brain banner names the active backend ("ElevenLabs (TTS)" / "MisoTTS local (TTS)"). Everything imports without torch/MisoTTS installed — miso imports stay lazy. Install the local backend with `pip install -e ".[miso]"` plus the MisoTTS repo (`pip install -e <MisoTTS>`) and `huggingface-cli login` for the gated Llama-3.2 tokenizer.
- **Voice**: ElevenLabs default `21m00Tcm4TlvDq8ikWAM` (Rachel, warm female), model `eleven_multilingual_v2`. Configurable via `tts_voice_id` / `tts_model_id`. Miso tuning: `miso_repo_id`, `miso_model_path`, `miso_speaker`, `miso_max_ms`.
- **Speech speed**: `1.0` (maps to ElevenLabs `voice_settings.speed`).
- **Brain**: `claude -p` with `--model opus` and `--dangerously-skip-permissions`, fed via stdin; uses the Max subscription (zero API cost).
- **Voice summary**: responses over ~300 chars get a 2–3 sentence Haiku summary for speaking; the full Opus output still prints to the terminal.
- **Auto-compaction**: past `compact_threshold_tokens` (24,000), older turns fold into a rolling summary; `compact_keep` (6) recent turns stay verbatim. On exit, everything folds into the saved session.
- **Goal**: lives at `~/.samantha/goal.json`, injected into the persona every turn, and is the stop condition for `samantha loop`.
- **Estimate gate**: build/project intents trigger a spoken estimate before work begins.
- **HUD**: printed after each turn — model · effort · git branch · context tokens/threshold · spend (API $0 on Max + honest TTS cost) · tokens · tok/s.
- **History**: sessions at `~/.samantha/sessions/`; Google STT (free) and ElevenLabs TTS (paid, per-character) are the only network calls.

## Commands
- `samantha` — full voice mode (mic in, voice out)
- `samantha --text` / `-t` — text in, voice out
- `samantha --no-voice` / `-n` — voice in, text out (TTS off)
- `samantha --text --no-voice` — pure text, no audio
- `samantha --tts elevenlabs|miso` — choose the TTS backend for this run (overrides `tts_provider`)
- `samantha goal "ship X"` / `samantha goal` / `samantha goal done` — set / show / finish the goal
- `samantha loop "ship X"` — autonomous mode (`--max-iters N`, default 8; `--text`, `--no-voice`; Ctrl-C to stop)
- `samantha resume [SESSION_ID]` — continue last (or a specific) Claude session
- `samantha config` / `samantha config KEY VALUE` — view / set settings
- `samantha config !` — print the Keychain key-storing one-liner

## Tests
Offline unit tests (no mic / Claude / network): `pytest`. Install dev deps with `pip install -e ".[voice-input,dev]"`. Coverage: `memory`, `goal`, `estimator`, `hud`, `secrets`.

## Conventions
- Keep the 11-module shape; new behavior goes in its own module (`memory`, `goal`, `estimator`, `hud`, `secrets`) and is wired in `brain`/`cli`.
- `memory.py`, `hud.py`, and the parsing in `estimator.py` stay pure — no I/O — so they're trivially testable; inject the model call.
- Never read or print the ElevenLabs key directly; go through `secrets`, and mask on display.
- Don't reintroduce Fish Audio. TTS goes through the `samantha.tts` backend factory — add a new voice engine as a backend implementing the `TTSBackend` protocol, don't hardcode synthesis in `voice.py`.
- Keep `samantha.tts` importable without torch/MisoTTS installed: every heavy backend dependency stays lazily imported inside methods.
