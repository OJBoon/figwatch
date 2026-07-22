# FigWatch

An AI-powered Figma design auditor. Drop a comment like `@tone` or `@ux` on any frame, and FigWatch replies with a detailed audit directly in the comment thread. Supports Google Gemini and Anthropic Claude.

Runs as a **macOS menu bar app** or a **headless Docker server** — same core, two deployment options.

## Features

- **Multi-file watching** — watch as many Figma files as you want simultaneously
- **Configurable triggers** — `@tone` and `@ux` are built in; add your own backed by any skill file (`.md`)
- **Generic skill execution** — FigWatch introspects each skill to determine what Figma data it needs (screenshot, node tree, text nodes, variables, styles, etc.) and fetches only what's required
- **Concurrent workers** — audits run on separate worker queues; configure worker counts in Settings (macOS)
- **Immediate acknowledgment** — posts a "working on it" reply while the AI processes the audit
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
cp .env.example .env   # fill in FIGMA_PAT, FIGWATCH_WEBHOOK_PASSCODE, FIGWATCH_TEAM_ID, GOOGLE_API_KEY
docker compose up -d --build
# then register a webhook with Figma — see docs/docker.md
```

## Requirements

### macOS app
- macOS 13+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code/getting-started)
- Claude access — either:
  - a personal Claude login (`claude login`), or
  - a company / self-hosted **gateway** configured for the Claude CLI. If
    `~/.claude/settings.json` defines an `ANTHROPIC_BASE_URL` + token (e.g. an
    active [cc-switch](https://github.com/farion1231/cc-switch) profile),
    FigWatch detects it automatically, uses the gateway's own model, and skips
    the personal-login step. Onboarding offers both paths.
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
2. FigWatch detects the comment and posts an acknowledgment reply
3. The relevant frame data (screenshot, node tree, etc.) is fetched from Figma
4. The AI evaluates the skill and posts the audit as a reply in the same comment thread

| | macOS app | Docker server |
|---|---|---|
| **Detection** | Polls Figma on a configurable timer | Real-time via Figma `FILE_COMMENT` webhooks |
| **Setup** | Paste a Figma file URL, click **Watch** | Register a webhook + set environment variables |
| **Guide** | Built-in onboarding | [docs/docker.md](docs/docker.md) |

## Built-in triggers

| Trigger | Skill | What it does |
|---------|-------|-------------|
| `@tone` | `figwatch/skills/tone/skill.md` | Tone of Voice audit against locale-specific guidelines (UK, DE, FR, NL, Benelux). Flags unnatural copy, hype language, incorrect currency formatting, punctuation issues, and glossary violations. |
| `@ux` | `figwatch/skills/ux/skill.md` | Nielsen's 10 Usability Heuristics evaluation. Takes a screenshot and reads the node tree, then evaluates all 10 heuristics with severity scores and recommendations. |

## Custom triggers

Add your own triggers in **Settings → Triggers → + Add** (macOS), or mount a `custom-skills/` volume (Docker):

1. Choose a trigger keyword (e.g. `@a11y`)
2. Point it at a skill file (any `.md` file that instructs the AI what to do)
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
server.py                        headless webhook server — HTTP, passcode auth, thread pool
macos/FigWatch.py                macOS menu bar app (PyObjC) — UI, state, worker queues
  ↓ (both use the same core)
figwatch/domain.py               WorkItem, status constants, trigger config + matching
figwatch/processor.py            process_work_item — ack → run skill → post reply
figwatch/watcher.py              FigmaWatcher — polls comments, detects triggers (macOS path)
figwatch/skills.py               skill discovery, introspection, prompt building, execution
figwatch/ack_updater.py          live queue-position updates on waiting audits
figwatch/queue_stats.py          queue depth tracking for ack messages
figwatch/metrics.py              OpenTelemetry metric definitions
figwatch/logging_config.py       structured logging setup (text + JSON formats)
figwatch/log_context.py          per-audit contextual log fields
figwatch/providers/
  figma.py                       Figma REST API + data fetching (screenshot, node tree, …)
  ai/__init__.py                 AIProvider protocol + make_provider() factory
  ai/gemini.py                   GeminiProvider  (Google Generative AI)
  ai/anthropic.py                AnthropicProvider  (Anthropic Messages API)
  ai/claude_cli.py               ClaudeCLIProvider  (Claude Code CLI — macOS only)
  ai/rate_limit.py               per-provider RPM rate limiting
figwatch/handlers/__init__.py    shared utilities (strip_markdown, subprocess_env, …)
figwatch/skills/                 bundled skill definitions (.md) + reference files
```

- **Provider-agnostic business logic** — `skills.py` and `processor.py` call `provider.call(prompt, image)` with no knowledge of which AI backend is in use; adding a new provider is one file + one line in `make_provider()`
- **No hardcoded handlers** — all triggers (including built-in `@tone` and `@ux`) route through the same skill execution pipeline
- **Fast path / slow path split** — `detect_triggers()` is a single API call (<1s); `process_work_item()` runs on worker threads and can take 30–120s
- **Multi-file, multi-worker** — each watched file gets its own `FigmaWatcher` thread; work items are dispatched to shared queues processed by configurable worker pools

## What's new in v1.3.0

- **Webhook-driven server** — replaced polling with Figma `FILE_COMMENT` webhooks; audits trigger in real time with no polling delay. See the [Docker setup guide](docs/docker.md) for webhook registration.
- **Google Gemini support** — primary provider for Docker deployments (free tier available); Anthropic Claude remains fully supported
- **OpenTelemetry metrics** — export audit duration, queue depth, and webhook reliability metrics to any OTel-compatible collector. See [OpenTelemetry metrics](docs/docker.md#opentelemetry-metrics).
- **Live queue-position updates** — when audits queue behind others, ack messages update with their position as the queue drains
- **Per-provider rate limiting** — configurable RPM caps for Gemini and Anthropic prevent 429 errors at the source
- **Progressive image fallback** — screenshot attempts PNG@1x → PNG@0.5x → JPG@1x → JPG@0.5x → JPG@0.25x to stay under the 5 MB API limit
- **Domain-driven package structure** — business logic (`processor.py`, `skills.py`) separated from provider implementations (`providers/figma.py`, `providers/ai/`)
- **Concurrent webhook workers** — `ThreadPoolExecutor` replaces thread-per-request; configurable via `FIGWATCH_WORKERS`

## What's new in v1.2.0

- **Docker / server deployment** — run FigWatch as a headless server with no macOS dependency
- **Multi-file watching** — watch multiple Figma files simultaneously with live status indicators (live, processing, replied, error) per file
- **Configurable triggers** — add custom `@trigger` keywords backed by any skill file; hot-reload without restart
- **Generic skill execution** — all triggers (including built-in `@tone` and `@ux`) run through a single pipeline; skills are introspected to determine what data they need, fetched in parallel, and executed via the AI provider
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

```bash
cp .env.example .env   # fill in your values
docker compose up -d --build
```

### Tests

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT
