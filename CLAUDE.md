# FigWatch

Figma comment watcher — monitors webhooks for trigger words, runs AI audits, posts replies.

## Project structure

- `figwatch/` — shared core package (providers, domain, logging, metrics)
- `server.py` — Docker/server entry point (webhook handler, worker threads)
- `macos/` — macOS menu bar app
- `tests/` — pytest suite
- `docs/` — deployment guide, ADRs

## Configuration

All config is via environment variables, read in `server.py:main()`.

**Fail-fast principle ([ADR-001](docs/adr/001-fail-fast-configuration.md)):** every env var must be validated at startup before the service accepts traffic. Invalid values → `logger.error()` + `sys.exit(1)`. No silent clamping, no deferred validation.

When adding a new env var:
1. Parse and validate in `server.py:main()`, before threads start
2. Required vars — reject if empty
3. Numeric vars — validate type and range
4. Enum vars — validate against accepted values
5. Optional vars with defaults — if explicitly set to an invalid value, reject (don't fall back to default)
