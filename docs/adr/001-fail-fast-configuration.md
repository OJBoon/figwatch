# ADR-001: Fail-fast configuration validation

**Status:** Accepted  
**Date:** 2026-04-18  
**Triggered by:** [#8 — UnboundLocalError when max_attempts=0](https://github.com/simonpforster/figwatch/issues/8)

## Context

FigWatch reads all configuration from environment variables at startup in `server.py:main()`. Issue #8 revealed that setting `FIGWATCH_MAX_ATTEMPTS=0` (a misconfiguration) passed startup silently but crashed the worker thread at runtime with an `UnboundLocalError`.

At the time of this decision, validation is inconsistent:

| Category | Example | Current behaviour |
|----------|---------|-------------------|
| Required secrets | `FIGMA_PAT`, `FIGWATCH_WEBHOOK_PASSCODE` | Validated, `sys.exit(1)` on missing |
| Numeric with constraints | `FIGWATCH_MAX_ATTEMPTS` | Silently clamped (post-fix) |
| Numeric without constraints | `FIGWATCH_PORT`, `FIGWATCH_WORKERS` | `int()` raises on non-numeric, but no range check |
| Enum-like | `FIGWATCH_MODEL`, `FIGWATCH_LOCALE` | No validation — invalid values pass through, fail at runtime |
| Rate limits | `FIGWATCH_GEMINI_RPM`, `FIGWATCH_ANTHROPIC_RPM` | Validated late, inside lazy-init limiter constructors |

A runtime crash from bad config is worse than a startup crash: it may happen under load, in a retry loop, or after the service has already acknowledged work.

## Decision

**All environment variable configuration must be validated at startup, before the service begins accepting traffic.** Invalid values must cause the process to exit immediately with a clear error message.

### Rules

1. **Required variables** — check non-empty, `logger.error()` + `sys.exit(1)` if missing.
2. **Numeric variables** — validate type _and_ range. Log the var name, the invalid value, and the acceptable range.
3. **Enum variables** — validate against the set of accepted values. Log the var name, the invalid value, and the valid options.
4. **Optional variables** — if unset, use the default. If set to an invalid value, treat as an error (do not silently fall back to the default).
5. **No silent clamping** — `max(1, value)` hides misconfiguration. Prefer rejecting invalid input over coercing it.
6. **All validation in one place** — `server.py:main()`, before any threads start or connections open.

### Pattern

```python
model = os.environ.get('FIGWATCH_MODEL', 'gemini-flash')
valid_models = {*GEMINI_MODELS, *CLAUDE_API_MODELS}
if model not in valid_models:
    logger.error('invalid FIGWATCH_MODEL',
                 extra={'value': model, 'valid': sorted(valid_models)})
    sys.exit(1)
```

## Consequences

- **New config vars** must include startup validation following this pattern.
- **Existing vars** without validation (`FIGWATCH_LOCALE`, `FIGWATCH_MODEL`, RPM vars) should be brought into compliance.
- **Silent clamping** (e.g. `max(1, ...)` for `max_attempts`) should be replaced with explicit rejection.
- **Late validation** (e.g. rate limiter constructors) should be moved to startup.
- **Tests** that set env vars to invalid values should expect `sys.exit(1)`, not silent fallback.
