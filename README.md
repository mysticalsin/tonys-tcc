# Tony's Claude Code taskbar

A smart, color-coded status line for [Claude Code](https://claude.com/claude-code) — see your model, thinking effort, context, cost, token burn, and **how close you are to your usage limits** at a glance. Colors and warnings fire automatically as you approach a wall.

Built on [ccstatusline](https://github.com/sirmalloc/ccstatusline) with a small shell colorizer (`cc-health.sh`) that turns the raw status JSON into traffic-light signals.

![Claude Code statusline preview](docs/statusline.svg)

---

## What you're seeing

**Line 1 — context**
- `🤖 model` you're on, `💭 effort` level **colored by intensity** (low → gray, max → red), current `📁 dir`, `🌿 git branch`.

**Line 2 — this session**
- `🧠 context %` used (green → amber → red), with `⚠compact soon` before Claude Code auto-summarizes.
- `💲 cost`, `📊 total tokens`, `⚡ burn rate` (tokens/sec).

**Line 3 — your limits**
- `🔋 5h block` and `📅 7-day` windows: **% used + bar + reset clock (`↻`)**.
- A burn-rate projection: `ok`, or `⚠cap~25m` when your current pace would hit the limit **before** it resets — the actionable "you're about to run out" signal.

Colors: 🟢 `<70%` · 🟡 `70–89%` · 🔴 `≥90%`.

---

## How it works

Claude Code pipes a JSON blob (model, context, `rate_limits`, `effort`, cost…) to whatever command you set as your `statusLine`. Here:

1. `ccstatusline` lays out the widgets and computes the fast stuff (token totals, burn rate, block timing from local transcripts).
2. For the parts that need **conditional color + projection**, ccstatusline calls `cc-health.sh` as a `custom-command` widget (`preserveColors: true`). The script reads the same JSON on stdin and prints ANSI-colored segments.

```
Claude Code ──JSON──▶ ccstatusline ──JSON on stdin──▶ cc-health.sh ──ANSI──▶ your terminal
```

`cc-health.sh` has three modes: `effort`, `ctx`, `limits`.

---

## Install

```bash
npm install -g ccstatusline@2.2.19
git clone https://github.com/mysticalsin/tonys-claude-code-taskbar.git ~/dotfiles
~/dotfiles/install.sh
```

Then add the status line to your `~/.claude/settings.json` (see [`examples/statusLine.json`](examples/statusLine.json)):

```json
{
  "statusLine": { "type": "command", "command": "ccstatusline", "padding": 0, "refreshInterval": 10 }
}
```

`install.sh` symlinks the scripts + ccstatusline config into place (and backs up anything it would overwrite). Requires `jq` and Node.

---

## Files

| Path | What |
|---|---|
| `.claude/bin/cc-health.sh` | Status colorizer — reads status JSON on stdin, prints colored `effort` / `ctx` / `limits` segments |
| `.claude/bin/cc-tokens` | On-demand token/cost report across all projects (wraps [ccusage](https://github.com/ryoppippi/ccusage)) |
| `.config/ccstatusline/settings.json` | ccstatusline widget layout (uses `$HOME` paths — portable) |
| `examples/statusLine.json` | The `statusLine` snippet to add to your own settings |

> Personal Claude Code settings (hooks, plugins) are **not** included — only the status-line layer.

---

## Bonus: `cc-tokens`

```bash
cc-tokens                   # token/cost per session+project
cc-tokens daily | monthly   # totals
cc-tokens blocks --active   # current 5h block: time left + projected total + cost
```

---

## Tunables (`cc-health.sh`)

| Knob | Default | Where |
|---|---|---|
| Danger color cutoffs | `70` / `90` | `col()` |
| Compact warning | `≥85%` | `ctx` mode |
| Bar width | `5` | `bar()` |
| Block lengths | `18000`s / `604800`s | `project()` calls |
| 7-day warn horizon | `86400`s (only warn if cap <24h out) | `limits` mode 7d call |

---

## Credits

- [ccstatusline](https://github.com/sirmalloc/ccstatusline) — the status line engine
- [cc-statusline](https://github.com/chongdashu/cc-statusline) — original inspiration
- [ccusage](https://github.com/ryoppippi/ccusage) — usage reporting behind `cc-tokens`

MIT
