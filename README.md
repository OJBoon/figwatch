# FigWatch

A macOS menu bar app that watches your Figma files and responds to `@tone` and `@ux` comments with AI-powered audits — powered by Claude Code.

Drop a comment like `@tone` or `@ux` on any frame in Figma, and FigWatch replies with a detailed audit directly in the comment thread.

## Features

- **@tone** — Tone of Voice audit against locale-specific guidelines (UK, DE, FR, NL, Benelux). Flags unnatural copy, hype language, incorrect currency formatting, punctuation issues, and glossary violations. Suggests fixes inline.
- **@ux** — Nielsen's 10 Usability Heuristics evaluation. Takes a screenshot and reads the node tree of the target frame, then evaluates all 10 heuristics with severity scores and specific recommendations.
- **Auto-detect open Figma files** via Chrome DevTools Protocol — automatically relaunches Figma with CDP enabled if needed, so there's nothing to configure.
- **Immediate acknowledgment** — posts a "working on it" reply while Claude processes the audit.
- **Locale selector** — switch between UK, DE, FR, NL, and Benelux from the menu bar dropdown.
- **Auto-restart** — if the watcher crashes, FigWatch restarts it automatically.
- **macOS notifications** — get notified when audits are posted.
- **Recent files** — quickly reconnect to previously watched files.

## Install

1. Download **FigWatch.zip** from the [latest release](https://github.com/livisliving/FigWatch/releases)
2. Unzip and drag `FigWatch.app` to **Applications**
3. First launch: **right-click → Open** (one-time Gatekeeper bypass)
4. Follow the onboarding to set up Claude Code and your Figma token

## Requirements

- macOS 13+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code/getting-started)
- Figma Personal Access Token
- Figma Desktop (recommended, for file detection + screenshots)

## How it works

1. Open a file in Figma Desktop
2. Click the FigWatch icon in your menu bar and select the file
3. Leave a comment on any frame:
   - `@tone` — runs a Tone of Voice audit for the selected locale
   - `@tone de` — runs a ToV audit using German guidelines (overrides the locale selector)
   - `@ux` — runs a UX heuristic evaluation with screenshot analysis
4. FigWatch polls for new comments every 30 seconds, picks up the trigger, and posts the audit as a reply

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

- `config.json` — Figma PAT and settings
- `recent-watches.json` — recently watched files
- `.processed-comments.json` — tracks which comments have been handled

## What's new in v1.1.2

- **Watch from URL** — paste a Figma file link directly when auto-detect can't see your file. Recent Figma Desktop builds (v126+) no longer expose the remote-debugging port, which broke auto-detect for some users; this gives you a manual path that always works.
- **Fixed Figma relaunch loop** — FigWatch will no longer quit and reopen Figma every 30 seconds when CDP isn't reachable. Auto-relaunch is now one-shot per session.
- Fixed the Disconnect button overlapping long file names in the header.
- Reply language setting — `@tone` and `@ux` can now reply in Chinese (简体中文) in addition to the source locale.
- Refreshed AI model labels (Sonnet, Opus, Haiku) — version numbers dropped in the settings dropdown.

## What's new in v1.1.1

- **Settings panel** — new gear icon in the footer opens a settings dialog with Figma token management and AI model switching (Sonnet, Opus, Haiku). Model choice is persisted and auto-restarts the watcher.
- **Auto-CDP** — FigWatch now auto-relaunches Figma Desktop with CDP debugging enabled if it detects Figma is running without it — no manual `--remote-debugging-port=9222` flag needed.
- `@ux` audits now reply as plain-text Figma comments (like `@tone`), instead of generating a separate `.md` report file.

## What's new in v1.1.0

- Watcher rewritten in pure Python (no Node.js dependency)
- App bundle shrunk from 129MB to 24MB (81% smaller)
- Onboarding: Claude Login check, better error feedback
- macOS notifications on audit completion and token validation
- Empty file list shows guidance

## License

MIT
