# FigWatch

A macOS menu bar app that watches your Figma files and responds to trigger comments with AI-powered audits — powered by Claude Code.

Drop a comment like `@tone` or `@ux` on any frame in Figma, and FigWatch replies with a detailed audit directly in the comment thread. Add your own skills to create custom triggers.

## Features

- **Multi-file watching** — watch as many Figma files as you want simultaneously, each with live status indicators in the popover.
- **Configurable triggers** — `@tone` and `@ux` are built in. Add your own triggers backed by any Claude skill file (`.md`).
- **Generic skill execution** — any skill that can produce a plain-text comment reply works. FigWatch introspects the skill to determine what Figma data it needs (screenshot, node tree, text nodes, variables, styles, etc.) and fetches only what's required.
- **Concurrent workers** — tone and UX audits run on separate worker queues. Configure the number of concurrent workers per queue in Settings.
- **Immediate acknowledgment** — posts a "working on it" reply while Claude processes the audit.
- **Locale selector** — switch between UK, DE, FR, NL, and Benelux from the menu bar dropdown. The locale is passed to all skills.
- **macOS notifications** — get notified when audits are posted.
- **In-app updates** — check for and install updates directly from Settings.

## Install

**One-line install** (recommended):

```bash
curl -fsSL https://raw.githubusercontent.com/OJBoon/figwatch/main/install.sh | bash
```

This downloads the latest release, installs `FigWatch.app` to `/Applications`, clears the Gatekeeper quarantine, and launches it. After that, all future updates can be done in-app via **Settings → Check for Updates → Install & Restart**.

**Manual install:**

1. Download **FigWatch.zip** from the [latest release](https://github.com/OJBoon/figwatch/releases)
2. Unzip and drag `FigWatch.app` to **Applications**
3. First launch: **right-click → Open** (one-time Gatekeeper bypass)
4. Follow the onboarding to set up Claude Code and your Figma token

## Requirements

- macOS 13+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code/getting-started)
- Figma Personal Access Token

## How it works

1. Click the FigWatch icon in your menu bar
2. Paste a Figma file URL and click **Watch** (or use **+ Add file** to watch more)
3. Leave a comment on any frame:
   - `@tone` — runs a Tone of Voice audit for the selected locale
   - `@tone de` — runs a ToV audit using German guidelines (overrides the locale selector)
   - `@ux` — runs a UX heuristic evaluation with screenshot analysis
   - `@yourtrigger` — runs whatever custom skill you've configured
4. FigWatch polls for new comments every 30 seconds, picks up the trigger, and posts the audit as a reply

## Built-in triggers

| Trigger | Skill | What it does |
|---------|-------|-------------|
| `@tone` | `skills/tone/skill.md` | Tone of Voice audit against locale-specific guidelines (UK, DE, FR, NL, Benelux). Flags unnatural copy, hype language, incorrect currency formatting, punctuation issues, and glossary violations. |
| `@ux` | `skills/ux/skill.md` | Nielsen's 10 Usability Heuristics evaluation. Takes a screenshot and reads the node tree, then evaluates all 10 heuristics with severity scores and recommendations. |

## Custom triggers

Add your own triggers in **Settings → Triggers → + Add**:

1. Choose a trigger keyword (e.g. `@a11y`)
2. Point it at a skill file (any `.md` file that instructs Claude what to do)
3. FigWatch introspects the skill to determine what Figma data it needs
4. Hot-reloads on all active watchers — no restart required

Skills can request any combination of:
- **Frame-scoped:** `screenshot`, `node_tree`, `text_nodes`, `prototype_flows`, `dev_resources`, `annotations`
- **File-scoped:** `variables_local`, `variables_published`, `styles`, `components`, `file_structure`

Skill files are searched in:
1. `~/.claude/skills/`
2. `.claude/skills/` (cwd)
3. `~/.figwatch/skills/`
4. The app bundle's `Resources/skills/`

## Supported locales

| Locale | Flag | Guidelines |
|--------|------|------------|
| UK | GB | English — default |
| DE | DE | German — formal (Sie), precise, no hype |
| FR | FR | French — elegant, warm (vous), guillemets |
| NL | NL | Dutch — direct, plain-speaking (je/jij) |
| Benelux | EU | Belgian Dutch + Belgian French |

## Configuration

FigWatch stores its config in `~/.figwatch/`:

| File | Purpose |
|------|---------|
| `config.json` | Figma PAT, model, locale, triggers, worker counts |
| `watched-files.json` | Files currently being watched |
| `skill-cache.json` | Cached skill introspection results |
| `.processed-comments.json` | Tracks which comments have been handled |

## Architecture (v1.2)

```
FigWatch.py          Menu bar app (PyObjC) — UI, state, worker queues
  ↓
watcher.py           FigmaWatcher per file — polls comments, detects triggers, dispatches WorkItems
  ↓
handlers/generic.py  Resolves skills, fetches Figma data, runs Claude, posts replies
  ↓
handlers/__init__.py  Shared utilities (strip_markdown, subprocess_env, figma_get_retry, etc.)
  ↓
skills/              Bundled skill definitions (.md) + reference files
```

- **No hardcoded handlers** — all triggers (including built-in `@tone` and `@ux`) route through the same generic skill execution pipeline.
- **Fast path / slow path split** — `detect_triggers()` is a single API call (<1s). `process_work_item()` runs on worker threads and can take 30-120s.
- **Multi-file, multi-worker** — each watched file gets its own `FigmaWatcher` thread. Work items are dispatched to shared queues processed by configurable worker pools.

## What's new in v1.2.0

- **Multi-file watching** — watch multiple Figma files simultaneously with live status indicators (live, processing, replied, error) per file. No more CDP/Chrome DevTools dependency — just paste a URL.
- **Configurable triggers** — add custom `@trigger` keywords backed by any skill file. Hot-reload without restart.
- **Generic skill execution** — all triggers (including built-in `@tone` and `@ux`) run through a single pipeline. Skills are introspected to determine what data they need, fetched in parallel, and executed via Claude.
- **Worker queues** — tone and UX audits run concurrently on separate worker pools. Configure worker counts in Settings (1-5 each).
- **Skill introspection cache** — custom skills are analysed once via Haiku to determine compatibility and data requirements. Built-in skills use pre-seeded cache data.
- **Removed CDP dependency** — no more Chrome DevTools Protocol, no more auto-relaunching Figma, no more port 9222. File detection is now URL-based.
- **Removed dedicated handlers** — `handlers/tone.py` and `handlers/ux.py` replaced by generic skill execution. The skill `.md` files are the single source of truth.

<details>
<summary>Previous releases</summary>

### v1.1.5

- Fix "Claude not installed" false positive — claude CLI path re-resolved on every dep check.
- Onboarding checklist stays put until setup is actually done — parses JSON `loggedIn` field.
- Fix `@ux` hanging — passes `--add-dir /tmp` so Claude can read screenshot/tree files.
- Surface Figma API errors instead of generic "not found" messages.
- Strip stale `.pyc` from `lib/python39.zip`.

### v1.1.4

- Real in-app auto-update — Install & Restart button downloads, swaps, and relaunches.

### v1.1.3

- Fix "Unable to generate audit" on Apple Silicon — augmented PATH for subprocess calls.

### v1.1.2

- Check for Updates button. Watch from URL fallback. Fixed Figma relaunch loop. Reply language setting (Chinese). Refreshed model labels.

### v1.1.1

- Settings panel. Auto-CDP relaunch. `@ux` replies as plain-text comments.

### v1.1.0

- Watcher rewritten in pure Python. App bundle shrunk 81%. Onboarding improvements.

</details>

## Development

### Prerequisites

- Python 3.11
- Docker (for server deployment)

### macOS app

```bash
make install   # install build dependencies (once)
make build     # build macos/dist/FigWatch.app
make clean     # remove build artefacts
```

### Docker / server

Copy `.env.example` to `.env`, fill in your values, then run:

```bash
docker compose up -d --build
```

## License

MIT
