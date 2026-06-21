"""Claude integration via the claude CLI.

Wraps `claude -p` (headless mode) to send prompts and receive responses.
Uses Claude Max through the CLI -- zero API cost.

The Brain owns context assembly (spec 4.4): persona + a rolling summary of
folded-away turns + recent verbatim history + the current turn. When estimated
context exceeds ``compact_threshold_tokens`` it folds older turns into the
summary via a cheap Haiku call. It also runs the estimate gate (spec 4.7) on
build/project intents and exposes a per-turn ``stats`` dict for the HUD.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from samantha import config as cfg
from samantha import estimator, goal, memory
from samantha.personality import get_system_prompt


@dataclass
class Message:
    """A single conversation turn."""

    role: str  # "user" or "samantha"
    content: str


class Brain:
    """Manages conversation with Claude via the CLI.

    Maintains a rolling history window plus a compacted summary, and constructs
    prompts with the Samantha personality, the active goal, and the user's name.
    """

    def __init__(
        self,
        max_history: int = 12,
        user_name: str = "Tony",
        claude_model: str = "opus",
        summary_model: str = "claude-haiku-4-5-20251001",
        compact_threshold_tokens: int = 24000,
        compact_keep: int = 6,
    ) -> None:
        self.max_history = max_history
        self.user_name = user_name
        self.claude_model = claude_model
        self.summary_model = summary_model
        self.compact_threshold_tokens = compact_threshold_tokens
        self.compact_keep = compact_keep

        self.history: list[Message] = []
        self.summary: str = ""  # rolling digest of folded-away turns

        self._claude_path = shutil.which("claude")
        self._activity_callback = None
        self._first_sent = False
        self._sessions_dir = Path.home() / ".samantha" / "sessions"
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        self._current_session_id: str | None = None
        self._history_file = Path.home() / ".samantha" / "history.json"  # legacy
        self._continue_mode = False
        self._resume_id: str | None = None

        # Per-turn telemetry surfaced to the HUD.
        self._full_response = ""
        self.last_think_stats: dict = {}
        # Estimate line stashed by think() for the CLI to speak before working.
        self.pending_estimate_line: str | None = None

        self._new_session()

    @property
    def available(self) -> bool:
        """Check whether the claude CLI is installed and on PATH."""
        return self._claude_path is not None

    # ---------------------------------------------------------------- intent

    def _run_haiku(self, prompt: str, timeout: int = 15) -> str:
        """Run one cheap Haiku call and return stripped stdout (``""`` on fail)."""
        if not self._claude_path:
            return ""
        try:
            result = subprocess.run(
                [
                    self._claude_path,
                    "-p",
                    "--output-format",
                    "text",
                    "--model",
                    self.summary_model,
                ],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            return ""
        return result.stdout.strip()

    def _summarize(self, prompt: str) -> str:
        """Haiku summarizer passed to memory.compact (str -> str)."""
        return self._run_haiku(prompt, timeout=30)

    def maybe_estimate(self, user_input: str) -> str | None:
        """Run the estimate gate on build intents (spec 4.7).

        On a project/build intent, runs one cheap Haiku estimate and returns the
        spoken line (also stashed in ``pending_estimate_line``). Otherwise
        returns ``None``. Never raises -- estimator swallows run_fn failures.
        """
        if not estimator.is_project_intent(user_input):
            self.pending_estimate_line = None
            return None

        est = estimator.estimate(user_input, self._run_haiku)
        line = estimator.speak_line(est, self.user_name)
        self.pending_estimate_line = line
        return line

    # ----------------------------------------------------------------- think

    def think(self, user_input: str) -> str:
        """Send user input to Claude and return the response."""
        if not self.available:
            raise RuntimeError(
                "The 'claude' CLI was not found on your PATH. "
                "Install it from https://docs.anthropic.com/en/docs/claude-cli"
            )

        self.history.append(Message(role="user", content=user_input))
        prompt = self._build_prompt()

        started = time.monotonic()
        try:
            cmd = [
                self._claude_path,
                "-p",
                "--output-format",
                "text",
                "--dangerously-skip-permissions",
                "--model",
                self.claude_model,
            ]
            if self._continue_mode:
                cmd.append("--continue")
                self._continue_mode = False
            elif self._resume_id:
                cmd.extend(["--resume", self._resume_id])
                self._resume_id = None

            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            raise TimeoutError("Claude took too long to respond. Try again.")

        elapsed = max(time.monotonic() - started, 1e-6)

        if result.returncode != 0:
            error = result.stderr.strip() or "Unknown error from claude CLI"
            raise RuntimeError(f"Claude CLI error: {error}")

        full_response = result.stdout.strip()
        if not full_response:
            full_response = (
                "Hmm, I lost my train of thought for a second. What were you saying?"
            )

        # Long responses get a short spoken summary via Haiku.
        if len(full_response) > 300:
            spoken = self._run_haiku(
                "Summarize this in 2-3 natural spoken sentences. "
                "No formatting, no markdown, no bullet points. "
                "Just talk naturally like you're telling someone what happened:\n\n"
                + full_response
            )
            response = spoken or full_response
        else:
            response = full_response

        response = (
            response.replace("**", "")
            .replace("```", "")
            .replace("##", "")
            .replace("# ", "")
            .strip()
        )

        self.history.append(Message(role="samantha", content=response))
        self._full_response = full_response

        # Per-turn telemetry for the HUD.
        out_tokens = memory.estimate_tokens(full_response)
        self.last_think_stats = {
            "tokens": out_tokens,
            "tok_per_s": out_tokens / elapsed,
            "elapsed_s": elapsed,
        }

        self._trim_history()
        self._maybe_compact()
        self._save_history()

        return response

    # --------------------------------------------------------------- context

    def _build_prompt(self) -> str:
        """Assemble persona(name, goal) + summary + recent verbatim + current."""
        active_goal = goal.get_goal()
        goal_text = goal.inject_text(active_goal)

        parts = [get_system_prompt(self.user_name, goal_text), ""]

        if self.summary.strip():
            parts.append("Summary of earlier conversation:")
            parts.append(self.summary.strip())
            parts.append("")

        recent = self.history[-6:]  # last 3 exchanges verbatim
        if len(recent) > 1:
            parts.append("Recent conversation:")
            for msg in recent[:-1]:
                speaker = "User" if msg.role == "user" else "Samantha"
                parts.append(f"{speaker}: {msg.content}")
            parts.append("")

        parts.append(f"User: {self.history[-1].content}")
        parts.append("")
        parts.append(
            "Respond as Samantha. 2-3 sentences max, natural speech, NO markdown, "
            "NO formatting, NO code blocks, NO bullet points. Just talk naturally."
        )
        return "\n".join(parts)

    def context_tokens(self) -> int:
        """Estimate current assembled-context size, for the HUD/threshold."""
        persona = get_system_prompt(self.user_name, "")
        return memory.context_tokens(persona, self.summary, self.history)

    def _maybe_compact(self) -> None:
        """Fold older turns into the summary when over the token threshold."""
        tokens = self.context_tokens()
        if memory.should_compact(tokens, self.compact_threshold_tokens):
            if self._activity_callback:
                self._activity_callback("Compacting older context...")
            self.summary, self.history = memory.compact(
                self.history, self.summary, self.compact_keep, self._summarize
            )

    def compact_and_save(self) -> None:
        """Fold all remaining history into the summary and persist (loop exit)."""
        if self.history:
            self.summary, self.history = memory.compact(
                self.history, self.summary, keep=0, summarize_fn=self._summarize
            )
        self._save_history()

    # --------------------------------------------------------------- session

    def _new_session(self) -> None:
        """Start a fresh session."""
        self._current_session_id = time.strftime("%Y%m%d-%H%M%S")
        self.history = []
        self.summary = ""
        self._first_sent = False

    def load_session(self, session_id: str) -> bool:
        """Load a specific session by ID."""
        import json as _json

        session_file = self._sessions_dir / f"{session_id}.json"
        if not session_file.exists():
            return False
        try:
            data = _json.loads(session_file.read_text())
            self.history = [
                Message(role=m["role"], content=m["content"])
                for m in data.get("messages", [])
            ]
            self.summary = data.get("summary", "") or ""
            self._current_session_id = session_id
            self._first_sent = bool(self.history)
            return True
        except (KeyError, _json.JSONDecodeError, OSError):
            return False

    def list_sessions(self) -> list[dict]:
        """List all saved sessions with preview info."""
        import json as _json

        sessions = []
        for f in sorted(self._sessions_dir.glob("*.json"), reverse=True):
            try:
                data = _json.loads(f.read_text())
                messages = data.get("messages", [])
                preview = ""
                for m in messages:
                    if m["role"] == "user":
                        preview = m["content"][:60]
                        break
                sessions.append(
                    {
                        "id": f.stem,
                        "date": data.get("date", f.stem),
                        "messages": len(messages),
                        "preview": preview,
                    }
                )
            except (KeyError, _json.JSONDecodeError, OSError):
                continue
        return sessions

    def _save_history(self) -> None:
        """Persist current session (messages + rolling summary) to disk."""
        import json as _json

        if not self.history and not self.summary:
            return
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        session_file = self._sessions_dir / f"{self._current_session_id}.json"
        data = {
            "date": time.strftime("%Y-%m-%d %H:%M"),
            "summary": self.summary,
            "messages": [
                {"role": m.role, "content": m.content} for m in self.history
            ],
        }
        session_file.write_text(_json.dumps(data, indent=2))

    def _trim_history(self) -> None:
        """Keep only the most recent exchanges."""
        max_messages = self.max_history * 2
        if len(self.history) > max_messages:
            self.history = self.history[-max_messages:]

    def reset(self) -> None:
        """Clear conversation history and summary."""
        self.history.clear()
        self.summary = ""
