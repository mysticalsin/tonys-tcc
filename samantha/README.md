# Samantha · Loop

**Give Claude a voice that calls you by name, never loses the thread, and drives toward a goal.**

Samantha wraps the Claude CLI with speech recognition and ElevenLabs text-to-speech. You speak, Claude thinks with full Opus intelligence, and Samantha's voice responds — addressing you by name, auto-compacting context so the conversation never falls off a cliff, estimating before she builds, and showing a live HUD each turn. It's the AI from *Her*, hardened into a self-managing loop.

She can do everything Claude Code can — write code, create files, run commands, search the web — you just talk to her instead of typing.

This is a fork of [`ethanplusai/samantha-cli`](https://github.com/ethanplusai/samantha-cli) (MIT) with ElevenLabs voice, call-by-name, auto-compaction, goal tracking, an autonomous loop mode, an estimate-before-build gate, and a live status HUD.

---

## What's new in the Loop fork

- **ElevenLabs voice** — replaces Fish Audio. Default voice is Rachel (warm, *Her*-ish), fully configurable.
- **Calls you by name** — she addresses you as Tony (configurable) naturally, not robotically.
- **Auto-compaction** — older turns are folded into a rolling summary past a token threshold, so the thread is never lost but the prompt stays bounded. Real compaction: Samantha owns her own context assembly.
- **Goal tracking** — set a goal once; it's injected into every turn and steers the conversation.
- **Loop modes** — interactive REPL by default, or an autonomous mode that self-continues toward the goal, bounded and interruptible.
- **Estimate before build** — on a build/project request she states a rough token + time estimate *before* starting.
- **Live HUD** — one status line per turn: model · effort · git branch · context pressure · spend · throughput.

---

## Install

The easiest way: open Claude Code in your terminal and say:

> "Install Samantha for me from this repo"

Claude reads `CLAUDE.md` and walks you through it. Or do it manually.

### Text + voice-out only (no microphone)

This is all you need to type to Samantha and hear her voice. No system dependencies.

```bash
git clone https://github.com/ethanplusai/samantha-cli.git samantha-loop
cd samantha-loop
pip install -e .
```

### Add the microphone (voice input)

The mic needs `portaudio` (a system library) plus the `voice-input` extra.

```bash
brew install portaudio              # macOS
# sudo apt install portaudio19-dev  # Linux

pip install -e ".[voice-input]"     # adds SpeechRecognition + PyAudio
```

### Voice backends (TTS)

Samantha speaks through a swappable TTS backend. Pick one with `tts_provider` in
config or `--tts` for a single run; if the chosen one can't run she falls back to
ElevenLabs and tells you why.

| Backend | What it is | Setup |
|---|---|---|
| **`elevenlabs`** (default) | Cloud synthesis, fast, low latency. Costs per character. | Just provide an ElevenLabs API key (below). |
| **`miso`** | The local **MisoTTS 8B** model — runs entirely on your machine, free and private, but heavy. | Optional extra; see below. |

**ElevenLabs** is the default and needs nothing beyond a key.

**MisoTTS (local)** is opt-in and demanding. Install it:

```bash
pip install -e ".[miso]"            # documents torch + torchaudio
git clone https://huggingface.co/MisoLabs/MisoTTS
pip install -e ./MisoTTS            # exposes the `generator` module
huggingface-cli login              # for the gated Llama-3.2 tokenizer
```

- The model weights are **~35 GB** and download on first use.
- A **24 GB CUDA GPU is recommended.** CPU works but is very slow.
- **On Apple Silicon, MPS is unsupported** (the model needs float64 ops MPS lacks),
  so a Mac runs on **CPU** — usable for testing, but **not real-time**.

Select it per run or persist it:

```bash
samantha --tts miso                 # this run only
samantha config tts_provider miso   # make it the default
```

If MisoTTS isn't installed (or a device can't be found), Samantha falls back to
ElevenLabs and prints the reason. Everything imports and runs **without** torch or
MisoTTS installed — the local backend's heavy imports stay lazy.

### Store your ElevenLabs key

Samantha never stores your key in the repo. She reads it from, in order: the `ELEVENLABS_API_KEY` environment variable, then the macOS Keychain, then config. **Keychain is recommended** — store it once and forget it:

```bash
security add-generic-password -s samantha-loop -a elevenlabs -w <your-key>
```

`samantha config !` prints that exact one-liner for you. Or use the environment variable instead:

```bash
export ELEVENLABS_API_KEY=your_key_here
```

Get a key at [elevenlabs.io](https://elevenlabs.io). Text mode works with no key — you just won't hear her.

**No Anthropic API key needed.** Samantha uses `claude -p`, which runs on your existing Claude Max/Pro subscription. Zero API cost for the AI. ElevenLabs TTS is the only paid service, billed per character (a few cents per response on the creator tier).

---

## Commands

| Command | What it does |
|---|---|
| `samantha` | Start a voice conversation (mic in, voice out) |
| `samantha --text` / `-t` | Text mode (type instead of speak, still hear her voice) |
| `samantha --no-voice` / `-n` | No TTS (speak to her, read her responses) |
| `samantha --text --no-voice` | Pure text mode, no audio at all |
| `samantha --tts elevenlabs\|miso` | Pick the TTS backend for this run (overrides `tts_provider`) |
| `samantha goal "ship X"` | Set the active goal |
| `samantha goal` | Show the current goal and its progress notes |
| `samantha goal done` | Mark the goal done |
| `samantha loop "ship X"` | Autonomous mode: self-continue toward the goal |
| `samantha resume` | Continue your last Claude session with voice |
| `samantha resume SESSION_ID` | Resume a specific Claude session |
| `samantha config` | Show current settings (key shown as resolved / not set, never printed) |
| `samantha config KEY VALUE` | Set a config value |
| `samantha config !` | Print the Keychain one-liner for storing your key |

`samantha loop` accepts `--text`, `--no-voice`, and `--max-iters N` (default 8). It stops when the goal is marked done or the cap is hit, and is interruptible with `Ctrl-C`.

### During a conversation

- **Just talk naturally.** Samantha waits for you to finish before responding.
- **Say "goodbye"**, **"I'm done"**, **"gotta go"** (or type `exit` / `/q`) to end the session.
- **Say "start over"** or **"forget everything"** (or type `/clear`) to clear history.
- **Press `Ctrl+C`** to exit immediately. On exit she folds everything into a final digest and saves the session.

---

## How it works

```
Your voice → Google STT (free) → claude -p with Opus (your Max subscription)
                                          ↓
                              Haiku summarizes long responses for voice
                                          ↓
                              ElevenLabs TTS (Rachel) → your speakers
                                          ↓
                              auto-compact older turns · live HUD each turn
```

- **Brain** — Claude Opus via `claude -p` (full intelligence, tools, file access, web search).
- **Voice summary** — Claude Haiku condenses responses over ~300 characters into 2–3 spoken sentences; you still see Opus's full output in the terminal.
- **Voice** — ElevenLabs, default voice Rachel (`21m00Tcm4TlvDq8ikWAM`), model `eleven_multilingual_v2`. The SDK is used when available, with a stdlib REST fallback.
- **Speech recognition** — Google's free STT (only with the `voice-input` extra).
- **Memory** — context is assembled as persona + a rolling summary of folded-away turns + recent verbatim turns. Past `compact_threshold_tokens` (default 24,000), older turns are summarized and dropped. On exit, everything is folded into the saved session.
- **Goal** — a single active goal lives at `~/.samantha/goal.json` and is injected into the persona every turn.
- **History** — sessions saved locally at `~/.samantha/sessions/`.

### The HUD

After each turn Samantha prints one status line:

```
🤖 Opus · ⛏ high · 🌿 main · 🧠 3.2k/24.0k · 💲 API $0 (Max) +TTS ~$0.0182 · 📊 0.4k · ⚡ 38.0 tok/s
```

model · reasoning effort · git branch · context tokens vs. threshold (turns red when over) · spend (Claude API is $0 on Max; ElevenLabs TTS spend is surfaced honestly) · tokens this turn · throughput.

### Estimate before build

When you ask her to *build* something ("build me a CLI", "scaffold a FastAPI service", "refactor this module"), she runs one cheap Haiku estimate first and says the number out loud before starting:

> "Roughly 80k tokens, about 12 minutes, Tony. Starting now."

Casual mentions ("what did you build today?") don't trigger it.

---

## Configuration

Settings live in `~/.samantha/config.yaml` (outside the repo). The ElevenLabs key is resolved separately (env → Keychain → config) and is masked when displayed.

| Setting | Default | What it does |
|---|---|---|
| `user_name` | `Tony` | Who Samantha addresses by name |
| `tts_provider` | `elevenlabs` | Which voice backend speaks: `elevenlabs` (cloud) or `miso` (local) |
| `tts_voice_id` | `21m00Tcm4TlvDq8ikWAM` | ElevenLabs voice (Rachel) |
| `tts_model_id` | `eleven_multilingual_v2` | ElevenLabs model |
| `tts_tier` | `creator` | ElevenLabs tier — drives the TTS cost shown in the HUD |
| `speech_speed` | `1.0` | How fast she talks (maps to ElevenLabs `voice_settings.speed`) |
| `miso_repo_id` | `MisoLabs/MisoTTS` | Hugging Face repo for the MisoTTS weights |
| `miso_model_path` | `` (empty) | Local MisoTTS checkpoint path; overrides `miso_repo_id` when set |
| `miso_speaker` | `0` | MisoTTS speaker id |
| `miso_max_ms` | `12000` | Max synthesized audio length per response, in ms |
| `language` | `en-US` | Speech recognition language |
| `max_history` | `12` | How many exchanges she keeps before trimming |
| `compact_threshold_tokens` | `24000` | Context budget before older turns are folded into the summary |
| `compact_keep` | `6` | Recent messages kept verbatim after a compaction |
| `claude_model` | `opus` | Model for the main think |
| `summary_model` | `claude-haiku-4-5-20251001` | Cheap model for voice summaries, compaction, and estimates |

```bash
samantha config user_name Tony                       # who she calls you
samantha config tts_voice_id <id>                    # different ElevenLabs voice
samantha config speech_speed 1.1                     # speed her up
samantha config compact_threshold_tokens 32000       # bigger context budget
```

---

## Security

- **Your key never touches the repo.** Resolution order is `ELEVENLABS_API_KEY` env → macOS Keychain (`samantha-loop` / `elevenlabs`) → `~/.samantha/config.yaml`. `samantha config` reports the key as *resolved* or *not set* without ever printing it.
- `.gitignore` covers `.env`, `*.key`, and `*.mp3` / `*.wav` audio artifacts.
- Samantha runs Claude with `--dangerously-skip-permissions` so she can actually build when you ask — Claude has full read/write/run access in your terminal. Run her in directories where you're okay with that, and remember voice input can be misheard.
- All conversation history stays local.

---

## Requirements

- **Python 3.10+**
- **Claude CLI** installed and authenticated → [docs.anthropic.com](https://docs.anthropic.com/en/docs/claude-cli)
- **Claude Max or Pro subscription** (Samantha uses `claude -p`, no API key needed)
- **ElevenLabs API key** → [elevenlabs.io](https://elevenlabs.io) (for her voice; text mode works without)
- **For the mic:** `portaudio` + `pip install -e ".[voice-input]"` (otherwise use `--text`)

---

## Development

```bash
pip install -e ".[voice-input,dev]"
pytest
```

The new modules ship with offline unit tests — `memory` (compaction), `goal` (set/show/clear/persist), `estimator` (intent detection + parsing), `hud` (render + cost), and `secrets` (resolution order). They run with no mic, no Claude, and no network.

---

## License

MIT. See [LICENSE](LICENSE). This is a fork of [`ethanplusai/samantha-cli`](https://github.com/ethanplusai/samantha-cli) by Ethan Rogers, used and extended under its MIT license.

## Credits

- Inspired by *Her* (2013), directed by Spike Jonze
- Forked from [samantha-cli](https://github.com/ethanplusai/samantha-cli) by Ethan Rogers
- Built with [Claude](https://claude.ai) by Anthropic
- Voice powered by [ElevenLabs](https://elevenlabs.io)
- Terminal UI by [Rich](https://github.com/Textualize/rich)
