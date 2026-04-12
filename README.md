# FigWatch

A Figma comment watcher powered by Claude Code. Drop a comment like `@tone` or `@ux` on any frame, and FigWatch replies with a detailed audit directly in the comment thread.

Runs as a **macOS menu bar app** or a **headless Docker server** — same core, two deployment options.

## Features

- **Multi-file watching** — watch as many Figma files as you want simultaneously
- **Configurable triggers** — `@tone` and `@ux` are built in; add your own backed by any Claude skill file (`.md`)
- **Generic skill execution** — FigWatch introspects each skill to determine what Figma data it needs (screenshot, node tree, text nodes, variables, styles, etc.) and fetches only what's required
- **Concurrent workers** — audits run on separate worker queues; configure worker counts in Settings (macOS)
- **Immediate acknowledgment** — posts a "working on it" reply while Claude processes the audit
- **Locale selector** — switch between UK, DE, FR, NL, and Benelux; the locale is passed to all skills
- **macOS notifications** — get notified when audits are posted (macOS only)
- **In-app updates** — check for and install updates directly from Settings (macOS only)

## Install

### macOS app

**One-line install** (recommended):

```bash
curl -fsSL https://raw.githubusercontent.com/OJBoon/figwatch/main/install.sh | bash
```

This downloads the latest release, installs `FigWatch.app` to `/Applications`, clears the Gatekeeper quarantine, and launches it. Future updates can be done in-app via **Settings → Check for Updates → Install & Restart**.

**Manual install:**

1. Download **FigWatch.zip** from the [latest release](https://github.com/OJBoon/figwatch/releases)
2. Unzip and drag `FigWatch.app` to **Applications**
3. First launch: **right-click → Open** (one-time Gatekeeper bypass)
4. Follow the onboarding to set up Claude Code and your Figma token

### Docker / server

See [docs/docker.md](docs/docker.md) for the full setup guide. Quick start:

```bash
cp .env.example .env   # fill in FIGMA_PAT, FIGWATCH_WEBHOOK_PASSCODE, GOOGLE_API_KEY
docker compose up -d --build
# then register a webhook with Figma — see docs/docker.md
```

## Requirements

### macOS app
- macOS 13+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code/getting-started)
- Figma Personal Access Token

### Docker / server
- Docker with Docker Compose
- A publicly accessible URL (or ngrok for local testing)
- Figma Personal Access Token + Figma team ID
- AI API key — choose one:
  - [Google AI API key](https://aistudio.google.com/apikey) for Gemini (free tier available)
  - [Anthropic API key](https://console.anthropic.com/) for Claude

## How it works

1. Someone pins a comment containing a trigger word (e.g. `@ux`) to a Figma frame
2. Figma sends a webhook event to FigWatch immediately
3. FigWatch posts an acknowledgment reply, then fetches the relevant data (screenshot, node tree, etc.)
4. The AI evaluates the skill and posts the audit as a reply in the same comment thread

**macOS app:** polls Figma on a timer — click the menu bar icon, paste a Figma file URL, click **Watch**.

**Server:** event-driven via Figma webhooks — configure via environment variables and register a webhook with Figma. See [docs/docker.md](docs/docker.md) for the full setup guide.

## Built-in triggers

| Trigger | Skill | What it does |
|---------|-------|-------------|
| `@tone` | `figwatch/skills/tone/skill.md` | Tone of Voice audit against locale-specific guidelines (UK, DE, FR, NL, Benelux). Flags unnatural copy, hype language, incorrect currency formatting, punctuation issues, and glossary violations. |
| `@ux` | `figwatch/skills/ux/skill.md` | Nielsen's 10 Usability Heuristics evaluation. Takes a screenshot and reads the node tree, then evaluates all 10 heuristics with severity scores and recommendations. |

## Custom triggers

Add your own triggers in **Settings → Triggers → + Add** (macOS), or mount a `custom-skills/` volume (Docker):

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
4. `figwatch/skills/` (bundled)

## Supported locales

| Locale | Flag | Guidelines |
|--------|------|------------|
| UK | GB | English — default |
| DE | DE | German — formal (Sie), precise, no hype |
| FR | FR | French — elegant, warm (vous), guillemets |
| NL | NL | Dutch — direct, plain-speaking (je/jij) |
| Benelux | EU | Belgian Dutch + Belgian French |

## Configuration

The macOS app stores its config in `~/.figwatch/`:

| File | Purpose |
|------|---------|
| `config.json` | Figma PAT, model, locale, triggers, worker counts |
| `watched-files.json` | Files currently being watched |
| `skill-cache.json` | Cached skill introspection results |
| `.processed-comments.json` | Tracks which comments have been handled |

The Docker server is configured entirely via environment variables — see [docs/docker.md](docs/docker.md).

## Architecture

```
macos/FigWatch.py    macOS menu bar app (PyObjC) — UI, state, worker queues
server.py            headless server entry point — reads env vars, starts watchers
  ↓ (both entry points use the same core)
figwatch/watcher.py           FigmaWatcher per file — polls comments, detects triggers, dispatches WorkItems
figwatch/handlers/generic.py  resolves skills, fetches Figma data, runs Claude, posts replies
figwatch/handlers/__init__.py shared utilities (strip_markdown, subprocess_env, figma_get_retry, etc.)
figwatch/skills/              bundled skill definitions (.md) + reference files
```

- **No hardcoded handlers** — all triggers (including built-in `@tone` and `@ux`) route through the same generic skill execution pipeline
- **Fast path / slow path split** — `detect_triggers()` is a single API call (<1s); `process_work_item()` runs on worker threads and can take 30–120s
- **Multi-file, multi-worker** — each watched file gets its own `FigmaWatcher` thread; work items are dispatched to shared queues processed by configurable worker pools

## What's new in v1.2.0

- **Docker / server deployment** — run FigWatch as a headless server with no macOS dependency
- **Multi-file watching** — watch multiple Figma files simultaneously with live status indicators (live, processing, replied, error) per file
- **Configurable triggers** — add custom `@trigger` keywords backed by any skill file; hot-reload without restart
- **Generic skill execution** — all triggers (including built-in `@tone` and `@ux`) run through a single pipeline; skills are introspected to determine what data they need, fetched in parallel, and executed via Claude
- **Worker queues** — tone and UX audits run concurrently on separate worker pools; configure worker counts in Settings (1–5 each)
- **Skill introspection cache** — custom skills are analysed once via Haiku to determine compatibility and data requirements; built-in skills use pre-seeded cache data
- **Removed CDP dependency** — no more Chrome DevTools Protocol, no more auto-relaunching Figma, no more port 9222; file detection is now URL-based
- **Removed dedicated handlers** — `handlers/tone.py` and `handlers/ux.py` replaced by generic skill execution; the skill `.md` files are the single source of truth

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
