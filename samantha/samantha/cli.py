"""CLI entry point for Samantha.

Provides the main `samantha` command plus subcommands: `config`, `resume`,
`goal`, and `loop` (autonomous mode).
"""

from __future__ import annotations

import os
import sys

import click
from rich.console import Console

from samantha import __version__
from samantha import config as cfg
from samantha import goal as goalmod
from samantha import hud, secrets
from samantha import tts as ttsmod
from samantha.brain import Brain
from samantha.ui import UI, Status
from samantha.voice import VoiceEngine, TTSError

# Human-readable labels for the active TTS backend, shown in the brain banner.
_BACKEND_LABELS = {
    "elevenlabs": "ElevenLabs (TTS)",
    "miso": "MisoTTS local (TTS)",
}


@click.group(invoke_without_command=True)
@click.option("--text", "-t", is_flag=True, help="Text-only mode (no microphone).")
@click.option("--no-voice", "-n", is_flag=True, help="Disable TTS output.")
@click.option(
    "--tts",
    type=click.Choice(["elevenlabs", "miso"]),
    default=None,
    help="TTS backend for this run (overrides config tts_provider).",
)
@click.version_option(version=__version__, prog_name="samantha")
@click.pass_context
def main(ctx: click.Context, text: bool, no_voice: bool, tts: str | None) -> None:
    """Samantha -- Give Claude a voice. Inspired by Her."""
    if ctx.invoked_subcommand is not None:
        return

    _run_assistant(text_mode=text, no_voice=no_voice, provider=tts)


def _build_brain(settings: dict) -> Brain:
    """Construct a Brain wired with the user's config."""
    return Brain(
        max_history=settings["max_history"],
        user_name=settings["user_name"],
        claude_model=settings["claude_model"],
        summary_model=settings["summary_model"],
        compact_threshold_tokens=settings["compact_threshold_tokens"],
        compact_keep=settings["compact_keep"],
    )


@main.command()
@click.argument("session_id", required=False)
@click.option(
    "--tts",
    type=click.Choice(["elevenlabs", "miso"]),
    default=None,
    help="TTS backend for this run (overrides config tts_provider).",
)
def resume(session_id: str | None, tts: str | None) -> None:
    """Resume a past conversation. Uses Claude's session history."""
    console = Console()
    settings = cfg.load()
    brain = _build_brain(settings)

    if session_id:
        brain._resume_id = session_id
        console.print(f"  Resuming session {session_id}...", style="green")
    else:
        brain._continue_mode = True
        console.print("  Continuing last session...", style="green")

    console.print()
    _run_assistant(text_mode=False, no_voice=False, brain=brain, provider=tts)


@main.command("goal")
@click.argument("text", nargs=-1)
def goal_cmd(text: tuple[str, ...]) -> None:
    """Set, show, or finish the active goal.

    \b
    Examples:
        samantha goal "ship the loop framework"   # set
        samantha goal                             # show
        samantha goal done                        # mark done
    """
    console = Console()
    joined = " ".join(text).strip()

    if not joined:
        g = goalmod.get_goal()
        if g is None:
            console.print("  [dim]No goal set.[/dim]")
            console.print('  [dim]Set one: samantha goal "ship X"[/dim]')
            return
        status = "done" if g.is_done else "active"
        console.print(f"\n  [bold magenta]Goal[/bold magenta] [dim]({status})[/dim]")
        console.print(f"  {g.text}")
        if g.notes:
            console.print("  [dim]Progress:[/dim]")
            for note in g.notes:
                console.print(f"    [dim]- {note}[/dim]")
        console.print()
        return

    if joined.lower() == "done":
        g = goalmod.mark_done()
        if g is None:
            console.print("  [dim]No goal to finish.[/dim]")
        else:
            console.print(f"  [green]Goal marked done:[/green] {g.text}")
        return

    g = goalmod.set_goal(joined)
    console.print(f"  [green]Goal set:[/green] {g.text}")


@main.command("loop")
@click.argument("text", nargs=-1, required=True)
@click.option("--text", "-t", "text_mode", is_flag=True, help="Text-only mode.")
@click.option("--no-voice", "-n", is_flag=True, help="Disable TTS output.")
@click.option(
    "--tts",
    type=click.Choice(["elevenlabs", "miso"]),
    default=None,
    help="TTS backend for this run (overrides config tts_provider).",
)
@click.option("--max-iters", default=8, show_default=True, help="Max autonomous turns.")
def loop_cmd(
    text: tuple[str, ...],
    text_mode: bool,
    no_voice: bool,
    tts: str | None,
    max_iters: int,
) -> None:
    """Autonomous mode: set a goal and let Samantha self-continue toward it.

    Bounded by --max-iters and interruptible with Ctrl-C. Stops when the goal is
    marked done or the iteration cap is hit.
    """
    goal_text = " ".join(text).strip()
    if not goal_text:
        Console().print("  [red]Give the loop a goal.[/red]")
        sys.exit(1)
    goalmod.set_goal(goal_text)
    _run_autonomous(
        goal_text,
        text_mode=text_mode,
        no_voice=no_voice,
        max_iters=max_iters,
        provider=tts,
    )


@main.command("config")
@click.argument("key", required=False)
@click.argument("value", required=False)
def config(key: str | None, value: str | None) -> None:
    """View or set configuration values.

    \b
    Examples:
        samantha config                       # Show all config
        samantha config user_name             # Show one value
        samantha config tts_voice_id <id>     # Set a value
        samantha config !                     # Print the Keychain key one-liner
    """
    console = Console()

    # The `!` command surfaces the Keychain one-liner for storing the key.
    if key == "!":
        console.print("\n  [bold magenta]Store your ElevenLabs key in the macOS Keychain:[/bold magenta]\n")
        console.print(f"  [cyan]{secrets.store_key_command()}[/cyan]\n")
        console.print("  [dim]Replace <your-key> with your real key, then run it once.[/dim]")
        console.print("  [dim]Samantha reads it back automatically.[/dim]\n")
        return

    if key is None:
        current = cfg.load()
        console.print("\n  [bold magenta]Samantha Configuration[/bold magenta]")
        console.print(f"  [dim]Config file: {cfg.CONFIG_FILE}[/dim]\n")
        for k, v in current.items():
            console.print(f"  [cyan]{k}[/cyan] = {_mask_secret(k, v)}")
        # Show the resolved key state without printing the key itself.
        resolved = secrets.get_elevenlabs_key()
        state = "resolved" if resolved else "not set"
        console.print(f"  [dim]elevenlabs key: {state} (env/keychain/config)[/dim]")
        _show_tts_backends(console, current)
        console.print()
        return

    if value is None:
        current = cfg.load()
        if key in current:
            console.print(f"  [cyan]{key}[/cyan] = {_mask_secret(key, current[key])}")
        else:
            console.print(f"  [red]Unknown key:[/red] {key}")
            console.print(f"  [dim]Available: {', '.join(cfg.DEFAULTS.keys())}[/dim]")
        return

    if key not in cfg.DEFAULTS:
        console.print(f"  [red]Unknown key:[/red] {key}")
        console.print(f"  [dim]Available: {', '.join(cfg.DEFAULTS.keys())}[/dim]")
        return

    default = cfg.DEFAULTS[key]
    if isinstance(default, bool):
        value = str(value).lower() in ("1", "true", "yes", "on")
    elif isinstance(default, int):
        value = int(value)
    elif isinstance(default, float):
        value = float(value)

    cfg.set_key(key, value)
    console.print(f"  [green]Set[/green] [cyan]{key}[/cyan] = {_mask_secret(key, value)}")


def _mask_secret(key: str, value) -> str:
    """Mask sensitive config values for display."""
    if "key" in key.lower() and isinstance(value, str) and len(value) > 8:
        return value[:4] + "..." + value[-4:]
    return str(value)


# --------------------------------------------------------------------------- #
# Shared setup
# --------------------------------------------------------------------------- #

def _make_voice(
    settings: dict, no_voice: bool, provider: str | None = None
) -> VoiceEngine:
    """Construct a VoiceEngine wired to the configured (or overridden) backend.

    ``provider`` overrides ``settings["tts_provider"]`` for this run (the
    ``--tts`` flag). ``--no-voice`` forces TTS off by disabling key resolution,
    so the engine reports ``tts_available == False`` regardless of backend.
    """
    tts_provider = provider or settings.get("tts_provider", "elevenlabs")
    backend_settings = dict(settings)
    if no_voice:
        # Force the ElevenLabs key off; Miso also reports unavailable so the
        # factory yields no backend and the loop runs text-out only.
        backend_settings["resolve_key"] = False
    return VoiceEngine(
        provider=tts_provider,
        settings=backend_settings,
        language=settings["language"],
        listen_timeout=settings["listen_timeout"],
        phrase_time_limit=settings["phrase_time_limit"],
    )


def _backend_label(name: str) -> str:
    """Banner label for an active backend name (falls back to a generic one)."""
    return _BACKEND_LABELS.get(name, f"{name} (TTS)" if name else "no voice")


def _show_tts_backends(console: Console, settings: dict) -> None:
    """Print the TTS provider and per-backend availability for `config`.

    Probes each backend without synthesizing (construction is cheap and never
    raises), so this stays fast and offline.
    """
    console.print(
        f"  [dim]tts: provider={settings.get('tts_provider', 'elevenlabs')}; "
        "backends ->[/dim]"
    )
    for name in ("elevenlabs", "miso"):
        backend = ttsmod._build(name, settings)
        if backend is not None and backend.available:
            console.print(f"    [green]{name}: available[/green]")
        else:
            reason = ttsmod._reason(backend, name)
            console.print(
                f"    [yellow]{name}: unavailable[/yellow] [dim]({reason})[/dim]"
            )


def _hud_stats(brain: Brain, voice: VoiceEngine, settings: dict) -> dict:
    """Assemble the per-turn stats dict for hud.render()."""
    return {
        "model": settings["claude_model"].capitalize(),
        "effort": "high",
        "branch": hud.git_branch(os.getcwd()),
        "ctx_tokens": brain.context_tokens(),
        "ctx_threshold": settings["compact_threshold_tokens"],
        "tts_chars": voice.chars_spoken,
        "tts_tier": settings["tts_tier"],
        "tokens": brain.last_think_stats.get("tokens", 0),
        "tok_per_s": brain.last_think_stats.get("tok_per_s", 0.0),
    }


def _announce_voice(
    ui: UI, voice: VoiceEngine, settings: dict, no_voice: bool
) -> None:
    """Surface the active backend, any fallback note, and a CPU-slow warning."""
    # The factory's note explains a fallback (or why there is no voice at all).
    if voice.backend_note:
        ui.show_info(voice.backend_note)

    if not no_voice and not voice.tts_available:
        ui.show_info(
            "No voice backend available. Running without voice output.\n"
            "         ElevenLabs: store a key with  samantha config !  "
            "(Keychain one-liner).\n"
            "         MisoTTS: install the local model (see README)."
        )

    # Miso flags CPU-only hosts as slow only after the model loads; warn early
    # using the same human text so the user isn't surprised by latency.
    warning = getattr(voice.backend, "slow_warning", "")
    if voice.backend_name == "miso" and not warning:
        warning = (
            "MisoTTS runs locally; on a machine without a CUDA GPU it falls back "
            "to CPU and synthesis will be very slow."
        )
    if warning:
        ui.show_info(warning)

    label = _backend_label(voice.backend_name) if voice.tts_available else "no voice"
    brain_model = settings["claude_model"].capitalize()
    ui.show_info(
        f"Brain: {brain_model} (thinking) -> Haiku (voice summary) -> {label}"
    )


def _run_assistant(
    text_mode: bool = False,
    no_voice: bool = False,
    brain: Brain | None = None,
    provider: str | None = None,
) -> None:
    """Main interactive conversation loop."""
    ui = UI()
    settings = cfg.load()

    if brain is None:
        brain = _build_brain(settings)
    if not brain.available:
        ui.show_error(
            "The 'claude' CLI is not installed or not on your PATH.\n"
            "         Install it: https://docs.anthropic.com/en/docs/claude-cli"
        )
        sys.exit(1)

    voice = _make_voice(settings, no_voice, provider)

    if not text_mode and not voice.stt_available:
        ui.show_error(
            "SpeechRecognition or PyAudio not installed. "
            "Falling back to text mode.\n"
            "         Install: pip install SpeechRecognition PyAudio"
        )
        text_mode = True

    brain._activity_callback = lambda msg: ui.show_info(f"  {msg}")
    _announce_voice(ui, voice, settings, no_voice)

    ui.show_welcome()

    try:
        _conversation_loop(ui, brain, voice, settings, text_mode)
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        try:
            brain.compact_and_save()
        except Exception:
            pass
        voice.cleanup()
        ui.show_goodbye()


def _respond(
    ui: UI,
    brain: Brain,
    voice: VoiceEngine,
    settings: dict,
    user_input: str,
    text_mode: bool,
) -> str:
    """Estimate gate -> think -> show -> speak -> HUD. Returns the response."""
    # Estimate gate (spec 4.7): state the estimate before building.
    est_line = brain.maybe_estimate(user_input)
    if est_line:
        ui.show_samantha(est_line)
        if voice.tts_available and not text_mode:
            try:
                path = voice.generate_audio(est_line)
                if path:
                    voice.play_audio(path)
            except TTSError:
                pass

    ui.show_status(Status.THINKING)
    response = brain.think(user_input)
    ui.clear_status()

    full = getattr(brain, "_full_response", response)
    if full != response and len(full) > len(response):
        from rich.panel import Panel
        from rich.text import Text

        ui.console.print(
            Panel(
                Text(full, style="dim"),
                title="[dim]Claude[/]",
                border_style="dim",
                padding=(0, 1),
            )
        )

    if voice.tts_available and not text_mode:
        ui.show_status(Status.SPEAKING)
        try:
            import threading

            audio_path = voice.generate_audio(response)
            if audio_path:
                player = threading.Thread(
                    target=voice.play_audio, args=(audio_path,), daemon=True
                )
                player.start()
                ui.clear_status()
                ui.show_samantha(response)
                player.join()
            else:
                ui.clear_status()
                ui.show_samantha(response)
        except TTSError as e:
            ui.clear_status()
            ui.show_samantha(response)
            ui.show_info(f"Voice output failed: {e}")
    else:
        ui.show_samantha(response)

    ui.show_hud(hud.render(_hud_stats(brain, voice, settings)))
    return response


def _conversation_loop(
    ui: UI,
    brain: Brain,
    voice: VoiceEngine,
    settings: dict,
    text_mode: bool,
) -> None:
    """Run the listen-think-speak loop until interrupted."""
    while True:
        if text_mode:
            try:
                user_input = ui.console.input("  [bold cyan]You:[/bold cyan] ").strip()
            except EOFError:
                break
            if not user_input:
                continue
        else:
            ui.show_status(Status.LISTENING)
            try:
                user_input = voice.listen()
            except KeyboardInterrupt:
                break
            except RuntimeError as e:
                ui.clear_status()
                ui.show_error(str(e))
                ui.show_info("Switching to text mode.")
                text_mode = True
                continue

            ui.clear_status()
            if user_input is None:
                continue
            ui.show_user(user_input)

        cmd = user_input.strip().lower()

        if cmd in ("exit", "quit", "bye", "goodbye", "stop", "/exit", "/q"):
            break
        exit_phrases = [
            "gotta go", "got to go", "i'm out", "i'm done", "wrap up",
            "talk later", "see you later", "see ya", "good night",
            "signing off", "peace out", "catch you later", "bye samantha",
            "bye bye", "that's all", "we're done", "samantha exit",
            "samantha quit", "samantha bye",
        ]
        if any(cmd == phrase for phrase in exit_phrases):
            break

        if any(
            phrase in cmd
            for phrase in [
                "forget everything", "start over", "clear the conversation",
                "fresh start", "new conversation", "reset",
            ]
        ) or cmd in ("/clear", "/c"):
            brain.reset()
            brain._first_sent = False
            brain._save_history()
            ui.show_info("Conversation cleared.")
            continue

        try:
            _respond(ui, brain, voice, settings, user_input, text_mode)
        except (RuntimeError, TimeoutError) as e:
            ui.clear_status()
            ui.show_error(str(e))
            continue


def _run_autonomous(
    goal_text: str,
    text_mode: bool = False,
    no_voice: bool = False,
    max_iters: int = 8,
    provider: str | None = None,
) -> None:
    """Autonomous loop (spec 4.6): self-continue toward the goal, bounded."""
    ui = UI()
    settings = cfg.load()

    brain = _build_brain(settings)
    if not brain.available:
        ui.show_error("The 'claude' CLI is not installed or not on your PATH.")
        sys.exit(1)

    voice = _make_voice(settings, no_voice, provider)
    brain._activity_callback = lambda msg: ui.show_info(f"  {msg}")
    _announce_voice(ui, voice, settings, no_voice)
    ui.show_welcome()
    ui.show_info(f"Autonomous loop toward: {goal_text} (max {max_iters} turns)")

    turn_input = f"Let's work toward this goal: {goal_text}. Where do we start?"
    try:
        for i in range(max_iters):
            g = goalmod.get_goal()
            if g is None or g.is_done:
                ui.show_info("Goal complete. Stopping.")
                break
            ui.show_user(f"[turn {i + 1}/{max_iters}] {turn_input}")
            try:
                _respond(ui, brain, voice, settings, turn_input, text_mode)
            except (RuntimeError, TimeoutError) as e:
                ui.show_error(str(e))
                break
            turn_input = (
                "Continue toward the goal. Take the next concrete step. "
                "If the goal is fully done, say so plainly."
            )
        else:
            ui.show_info("Reached the iteration cap. Stopping.")
    except (KeyboardInterrupt, EOFError):
        ui.show_info("Interrupted.")
    finally:
        try:
            brain.compact_and_save()
        except Exception:
            pass
        voice.cleanup()
        ui.show_goodbye()


if __name__ == "__main__":
    main()
