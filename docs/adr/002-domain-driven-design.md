# ADR-002: Domain-Driven Design adoption

**Status:** Accepted  
**Date:** 2026-04-19  
**Triggered by:** Growing complexity in processor/server orchestration; domain concepts scattered across layers

## Context

FigWatch has a single bounded context — **Comment Auditing** — with a clear lifecycle: a Figma comment containing a trigger word is detected, matched to a skill, queued, processed by an AI provider, and a reply is posted back. The codebase already has DDD-adjacent constructs:

- `WorkItem` namedtuple acts as an entity carrying audit identity
- `match_trigger()` is a pure domain function
- `AIProvider` Protocol defines a port for AI infrastructure
- `QueuedItem` dataclass wraps `WorkItem` with queue metadata
- Status constants (`STATUS_LIVE`, `STATUS_DETECTED`, etc.) represent an implicit state machine

However, several concerns are mixed across layers:

| Problem | Where |
|---------|-------|
| Figma API calls (`figma_post`, `figma_delete`) called directly from `processor.py` | Domain orchestration coupled to HTTP transport |
| `WorkItem` carries infrastructure credentials (`pat`, `claude_path`) | Domain entity polluted with infra config |
| `_build_work_item` in `server.py` mixes webhook parsing with domain construction | Application and domain logic interleaved |
| No domain events — status changes are ad-hoc `on_status` callbacks | No formal event model for audit lifecycle |
| `watcher.py` and `server.py` both construct `WorkItem` with different field sets | No single authoritative factory |
| `load_trigger_config()` does file I/O inside `domain.py` | Domain module has infrastructure side effects |

The project is small (~1500 lines of core code) and must stay simple. Full DDD machinery (event sourcing, CQRS, saga orchestrators) would be over-engineering. This ADR adopts **tactical DDD patterns proportional to the project's actual complexity**.

## Decision

### 1. Bounded Context and Ubiquitous Language

FigWatch operates in a single bounded context: **Comment Auditing**.

| Domain term | Code name | Definition |
|-------------|-----------|------------|
| **Audit** | `Audit` | The aggregate root. Full lifecycle of responding to a trigger comment — from detection through AI processing to reply. Replaces `WorkItem`. |
| **Trigger** | `Trigger` | Value object: the keyword (e.g. `@ux`) and its associated skill reference. Replaces raw trigger dicts. |
| **TriggerMatch** | `TriggerMatch` | Value object: result of matching a comment against triggers — includes the trigger, skill path, and extra context text. |
| **Comment** | `Comment` | Value object: a Figma comment — ID, message text, parent ID, node ID, user handle, file key. Replaces untyped dict fields on `WorkItem`. |
| **Skill** | `Skill` | Value object: what AI prompt to run — a reference (builtin or file path) and compatibility metadata. |
| **AuditResult** | `AuditResult` | Value object: the AI provider's response text, provider name, and frame name. Replaces raw string returns. |
| **AuditStatus** | `AuditStatus` | Enum replacing string constants: `DETECTED`, `QUEUED`, `PROCESSING`, `REPLIED`, `ERROR`. |

The term "WorkItem" is retired in favour of "Audit" — it better reflects the domain ("we are auditing a design") and matches how log messages and user-facing ack messages already describe the operation.

### 2. Aggregate Boundaries

**One aggregate: `Audit`.**

The `Audit` aggregate root owns the full lifecycle state: status, comment reference, trigger match, skill, attempt count. It is the only entity. Everything else is a value object.

This is appropriate because:
- There is exactly one lifecycle to manage (comment-in, reply-out)
- No concurrent mutation conflicts between aggregates (each audit is independent)
- The project has no persistence layer — aggregates live in memory for the duration of processing

**What stays out of the aggregate:**
- Infrastructure credentials (`pat`, API keys) — passed to repositories, never stored on domain objects
- Queue metadata (enqueued_at, wait time) — stays in `QueuedItem`, which wraps an `Audit` reference
- AI provider selection (model name, claude_path) — application-layer configuration
- Locale and reply language — runtime config passed to services, not domain state

### 3. Value Objects

All value objects are `frozen=True` dataclasses (immutable).

```python
@dataclass(frozen=True)
class Trigger:
    keyword: str          # e.g. "@ux"
    skill_ref: str        # e.g. "builtin:ux" or "/path/to/skill.md"

@dataclass(frozen=True)
class TriggerMatch:
    trigger: Trigger
    extra: str            # additional context after the trigger word

@dataclass(frozen=True)
class Comment:
    comment_id: str
    message: str
    parent_id: Optional[str]
    node_id: Optional[str]
    user_handle: str
    file_key: str

@dataclass(frozen=True)
class AuditResult:
    reply_text: str
    provider_name: str
    frame_name: str
```

### 4. Audit Aggregate Root

```python
class AuditStatus(Enum):
    DETECTED = 'detected'
    QUEUED = 'queued'
    PROCESSING = 'processing'
    REPLIED = 'replied'
    ERROR = 'error'

@dataclass
class Audit:
    """Aggregate root — one per trigger comment detected."""
    audit_id: str
    comment: Comment
    trigger_match: TriggerMatch
    status: AuditStatus = AuditStatus.DETECTED
    _events: List = field(default_factory=list, repr=False)

    def queue(self):
        self.status = AuditStatus.QUEUED
        self._events.append(AuditQueued(self.audit_id))

    def start_processing(self):
        self.status = AuditStatus.PROCESSING
        self._events.append(AuditStarted(self.audit_id))

    def complete(self, result: AuditResult):
        self.status = AuditStatus.REPLIED
        self._events.append(AuditCompleted(self.audit_id, result))

    def fail(self, error: str):
        self.status = AuditStatus.ERROR
        self._events.append(AuditFailed(self.audit_id, error))

    def collect_events(self) -> list:
        events, self._events = self._events, []
        return events
```

### 5. Domain Events

Lightweight frozen dataclasses, not a full event bus. Events are collected on the aggregate and dispatched by the application layer after the operation completes. This keeps the domain pure while giving the application layer hooks for metrics, logging, and ack lifecycle.

```python
@dataclass(frozen=True)
class TriggerDetected:
    audit_id: str
    trigger_keyword: str
    file_key: str
    node_id: str

@dataclass(frozen=True)
class AuditQueued:
    audit_id: str

@dataclass(frozen=True)
class AuditStarted:
    audit_id: str

@dataclass(frozen=True)
class AuditCompleted:
    audit_id: str
    result: AuditResult

@dataclass(frozen=True)
class AuditFailed:
    audit_id: str
    error: str
```

**Dispatching pattern** (application layer, not domain):

```python
events = audit.collect_events()
for event in events:
    if isinstance(event, AuditCompleted):
        record_audit_completed(duration, 'success')
    elif isinstance(event, AuditFailed):
        record_audit_completed(duration, 'failed')
```

This replaces the ad-hoc `on_status` callback currently threaded through `WorkItem`.

### 6. Repository Pattern

Repositories abstract infrastructure behind domain-oriented interfaces. Two repositories:

```python
# figwatch/ports.py

class CommentRepository(Protocol):
    """Port for reading and writing Figma comments."""

    def post_reply(self, file_key: str, parent_comment_id: str, message: str) -> Optional[str]:
        ...

    def delete_comment(self, file_key: str, comment_id: str) -> None:
        ...

    def fetch_comments(self, file_key: str) -> list:
        ...

class DesignDataRepository(Protocol):
    """Port for fetching Figma design data needed by skills."""

    def fetch(self, required_data: list, file_key: str, node_id: str) -> tuple:
        ...
```

Implementations live in `figwatch/providers/figma.py`, wrapping existing functions. Repositories hold the PAT internally rather than passing it per-call.

**What does NOT get a repository:**
- AI providers — already behind the `AIProvider` Protocol. No change needed.
- Work queue — application infrastructure, not a domain concept. `InstrumentedQueue` stays as-is.

### 7. Layer Rules

```
┌──────────────────────────────────────────┐
│  server.py / FigWatch.app                │  ← Entry points (config, HTTP, macOS)
├──────────────────────────────────────────┤
│  figwatch/services.py                    │  ← Application services (orchestration)
│  figwatch/ack_updater.py                 │     Coordinates domain + infrastructure
│  figwatch/queue_stats.py                 │
├──────────────────────────────────────────┤
│  figwatch/domain.py                      │  ← Domain layer (pure, no I/O)
│  figwatch/ports.py                       │     Aggregates, VOs, events, pure fns,
│                                          │     repository protocols
├──────────────────────────────────────────┤
│  figwatch/providers/                     │  ← Infrastructure layer
│  figwatch/watcher.py                     │     Implements ports, talks to APIs
│  figwatch/metrics.py                     │
└──────────────────────────────────────────┘
```

**Dependency rules:**

1. **Domain imports nothing from other layers.** `domain.py` and `ports.py` have zero imports from `providers/`, `server.py`, `skills.py`, or `metrics.py`.
2. **Application layer imports domain and ports.** It receives repository implementations via constructor injection.
3. **Infrastructure implements ports.** `providers/figma.py` imports from `ports.py` to know what interface to satisfy.
4. **Entry points wire everything together.** `server.py:main()` constructs repositories and passes them into application services.
5. **Side effects live in infrastructure only.** Domain functions are pure — no HTTP calls, file I/O, or logging of business metrics.

**Import enforcement:** `domain.py` must not import from `figwatch.providers`, `figwatch.skills`, `figwatch.metrics`, `urllib`, `requests`, or `os`. Trigger config file discovery moves to the application layer.

### 8. Migration Strategy

Incremental adoption in four phases. Each phase is a standalone PR that leaves the system fully functional.

**Phase 1 — Value objects and Audit aggregate** (`domain.py` only)
- Add `AuditStatus` enum, `Trigger`, `TriggerMatch`, `Comment`, `AuditResult` frozen dataclasses
- Add `Audit` aggregate root with status transition methods and domain events
- Keep `WorkItem` namedtuple as a deprecated alias — existing code continues to work
- Move `load_trigger_config()` and `_discover_custom_triggers()` to `figwatch/trigger_config.py` (application-layer I/O)
- `match_trigger()` stays in `domain.py` — pure domain logic — but returns `TriggerMatch` instead of raw dict

**Phase 2 — Repository protocols and Figma implementation**
- Create `figwatch/ports.py` with `CommentRepository` and `DesignDataRepository` protocols
- Create `FigmaCommentRepository` and `FigmaDesignDataRepository` in `providers/figma.py` wrapping existing free functions
- Keep existing free functions during migration

**Phase 3 — Application service extraction**
- Create `figwatch/services.py` with `AuditService` taking repositories via constructor
- Move orchestration from `processor.py` into `AuditService.execute(audit)`
- `server.py` constructs `AuditService` in `main()` and passes to worker threads
- Replace `on_status` callback with domain event dispatch

**Phase 4 — Remove legacy patterns**
- Delete `WorkItem` namedtuple
- Remove free-function fallbacks from `processor.py`
- Remove `pat` threading through domain objects — repositories hold credentials
- Update `QueuedItem` to wrap `Audit` instead of `WorkItem`
- Update `watcher.py` (macOS path) to use `AuditService`

### Pattern: Before and After

**Before — `WorkItem` carries PAT and infra config:**

```python
WorkItem = namedtuple('WorkItem', [
    'file_key', 'comment_id', 'reply_to_id', 'node_id',
    'trigger', 'skill_path', 'user_handle', 'extra',
    'locale', 'model', 'reply_lang', 'pat', 'claude_path', 'on_status',
])
```

**After — `Audit` holds only domain data:**

```python
@dataclass
class Audit:
    audit_id: str
    comment: Comment
    trigger_match: TriggerMatch
    status: AuditStatus = AuditStatus.DETECTED
```

**Before — `processor.py` posts directly to Figma API:**

```python
def post_ack(item, message: str) -> Optional[str]:
    ack = figma_post(f'/files/{item.file_key}/comments', {
        'message': message,
        'comment_id': item.reply_to_id,
    }, item.pat)
    return ack.get('id')
```

**After — application service uses repository:**

```python
class AuditService:
    def __init__(self, comment_repo: CommentRepository, ...):
        self._comments = comment_repo

    def post_ack(self, audit: Audit, message: str) -> Optional[str]:
        return self._comments.post_reply(
            audit.comment.file_key,
            audit.comment.parent_id,
            message,
        )
```

## Consequences

- **New domain types** (Audit, Trigger, Comment, etc.) must be frozen dataclasses or dataclass aggregate roots. No raw dicts for domain concepts.
- **Repository protocols** must be defined in `ports.py` for any new infrastructure integration.
- **`domain.py` must remain pure** — no I/O imports, no side effects. CI can enforce this with a grep-based lint rule.
- **Testing improves** — domain logic testable without mocking HTTP calls; repositories fakeable trivially.
- **Existing macOS polling path** (`watcher.py`) must be migrated in Phase 4 — until then it continues using the legacy `WorkItem` path.
- **Small overhead** — repository indirection adds one call depth, negligible for a project doing multi-second AI API calls.
- **Aligns with ADR-001** — fail-fast config validation stays in `server.py:main()`, now wires repositories and services before accepting traffic.
