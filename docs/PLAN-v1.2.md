# FigWatch v1.2 — Implementation Plan

## Goal
User types `@trigger` comment in Figma → Claude responds. Zero interaction required after initial setup.

---

## Pre-implementation cleanup

Before writing any new code, clean up the debug additions made during the v1.1.5 investigation session:

- Remove `_dbg()` and `_DBGLOG` from `FigWatch.py`
- Remove CDP debug logging injected into `_get_open_files()`
- Remove Quartz ctypes window detection test from `applicationDidFinishLaunching_`
- These exist in `/Applications/FigWatch.app/Contents/Resources/FigWatch.py` (installed copy) but NOT in the repo copy — verify repo copy is clean before starting

---

## Implementation steps (in dependency order)

### Step 1 — `watcher.py`: WorkItem + trigger routing

**Add:**
- `import queue` at top
- `WorkItem` namedtuple: `(file_key, comment_id, reply_to_id, node_id, trigger, skill_path, user_handle, extra, locale, model, reply_lang, pat, claude_path, on_status)`
- `load_trigger_config()` — reads `triggers` key from `~/.figwatch/config.json`, falls back to `DEFAULT_TRIGGERS`
- `DEFAULT_TRIGGERS = [{"trigger": "@tone", "skill": "builtin:tone"}, {"trigger": "@ux", "skill": "builtin:ux"}]`
- `match_trigger(message, trigger_config)` — replaces `match_handler()`, returns `{trigger, skill, extra}` or `None`
- `detect_triggers(file_key, pat, processed_ids, trigger_config, *, log, on_status=None)` — fast (1 API call, <1s), fetches comments, finds matches, marks processed, returns `list[WorkItem]`
- `process_work_item(item: WorkItem)` — slow path: post ack → resolve skill path → call `handlers/generic.py` → post reply → call `on_status` callbacks

**Remove:**
- `HANDLERS` dict
- `match_handler()`
- `list_triggers()`
- `from handlers.tone import tone_handler`
- `from handlers.ux import ux_handler`
- `poll_once()` — replaced entirely

**Update `FigmaWatcher`:**
- New constructor params: `trigger_config=None`, `dispatch=None`, `on_poll=None`, `on_status=None`
- Remove `on_reply` (keep as deprecated alias → `on_status('replied', ...)`)
- `_run()` loop: call `detect_triggers()` → push each `WorkItem` via `self._dispatch(item)` → call `on_poll()` → sleep. Never blocks on Claude.
- Add `reload_trigger_config(trigger_config)` — hot-reload triggers without restart
- Add `initial_delay` param (default 0) — sleep before first poll (for staggered starts)

**Update CLI mode (`if __name__ == '__main__'`):**
- Update to use `detect_triggers` + `process_work_item` instead of old `poll_once`
- Or simplify to just print "use the app" — CLI mode is rarely used

---

### Step 2 — Create `handlers/generic.py`

New file. Replaces the Python-specific logic in `tone.py` and `ux.py`.

**Functions:**

`_skill_cache_path()` → `~/.figwatch/skill-cache.json`

`_load_skill_cache()` / `_save_skill_cache(cache)` — JSON cache keyed by `"{path}:{mtime}"`

`_subprocess_env()` — returns augmented PATH env dict. Extracted from `ux.py` (currently duplicated in both handlers).

`_strip_markdown(text)` — extracted from `ux.py` (currently duplicated in both handlers).

`_figma_get(path, pat, retries=1)` — retry-capable version from `ux.py` (includes 429 + `Retry-After` handling). This becomes the canonical version — `watcher.py`'s `figma_get` should also be updated to use retry logic.

`_resolve_builtin_skill(skill_ref)` — maps `"builtin:tone"` → `[bundle]/skills/tone/skill.md` etc.

`_find_skills()` — scans for `.md` skill files in:
1. `~/.claude/skills/`
2. `.claude/skills/` (cwd)
3. `~/.figwatch/skills/`
4. `[bundle]/Resources/skills/`
Returns list of `{"path": str, "name": str, "builtin": bool}`

`introspect_skill(skill_path, claude_path)` — Phase 1:
1. Check cache by `"{path}:{mtime}"`
2. If miss: run `claude --print -p {introspection_prompt} --model haiku` (fast, text-only)
3. Parse JSON response: `{"comment_compatible": bool, "incompatible_reason": str|null, "required_data": [...]}`
4. Save to cache, return result
5. On any failure: return safe default `{"comment_compatible": True, "required_data": ["screenshot", "node_tree"]}`

Introspection prompt enumerates all available data points:
```
Frame-scoped: screenshot, node_tree, text_nodes, prototype_flows, dev_resources, annotations
File-scoped: variables_local, variables_published, styles, components, file_structure
```

`fetch_figma_data(required_data, file_key, node_id, pat)` — fetches only declared data:

| Data point | Source |
|---|---|
| `screenshot` | `GET /images/{key}?ids={id}&scale=2&format=png` + download |
| `node_tree` | `GET /files/{key}/nodes?ids={id}&depth=100` → written to `/tmp/figwatch-tree-{id}.json` |
| `text_nodes` | Derived from `node_tree` via `extract_text_from_node()` |
| `prototype_flows` | Derived from `node_tree` (reactions field) |
| `dev_resources` | `GET /files/{key}/dev_resources?node_ids={id}` |
| `annotations` | Derived from `node_tree` |
| `variables_local` | `GET /files/{key}/variables/local` |
| `variables_published` | `GET /files/{key}/variables/published` |
| `styles` | `GET /files/{key}/styles` |
| `components` | `GET /files/{key}/components` |
| `file_structure` | `GET /files/{key}?depth=2` |

Returns `dict[data_type → value]` where value is a file path (screenshot/tree) or parsed dict/list.

`execute_skill(skill_path, data, frame_name, extra, claude_path, model, reply_lang)` — Phase 2:
1. Read skill `.md`
2. Build prompt: skill content + data descriptions/paths
3. Assemble `--add-dir` for any `/tmp` paths in data
4. Run `claude -p {prompt} --print --allowedTools Read --add-dir /tmp --model {model}`
5. Strip markdown, return formatted reply string
6. Write debug log to `/tmp/figwatch-generic-debug.log`

`execute_builtin_tone(item, data)` — thin wrapper preserving existing `tone_handler` calling convention for `builtin:tone`. Avoids cold-start introspection on first launch.

`execute_builtin_ux(item, data)` — same for `builtin:ux`.

---

### Step 3 — `FigWatch.py`: persistence layer

Add near top:
```python
WATCHED_PATH = os.path.join(CONFIG_DIR, "watched-files.json")
SKILL_CACHE_PATH = os.path.join(CONFIG_DIR, "skill-cache.json")
```

Add functions:
- `_load_watched()` → list of `{"key": str, "name": str, "figjam": bool}`, returns `[]` on error
- `_save_watched(files)` — no length cap (unlike recents)
- `_add_watched(key, name, figjam=False)` — upsert by key, save
- `_remove_watched(key)` — remove by key, save

**Migration (first v1.2 launch):** In `applicationDidFinishLaunching_`, after loading config, check if `watched-files.json` is missing but `recent-watches.json` exists — if so, copy recents → watched and save. One-time migration, silent.

---

### Step 4 — `FigWatch.py`: state + initialisation rewrite

**New `_state` dict:**
```python
{
    "pat": None, "user": None, "locale": "uk",
    "model": "sonnet", "reply_lang": "en",
    "watchers": {},        # file_key → FigmaWatcher
    "file_statuses": {},   # file_key → {status, trigger, user, last_poll, error, name}
    "work_queue_tone": queue.Queue(),
    "work_queue_ux": queue.Queue(),
    "workers": [],
    "trigger_config": [],
    "installing_claude": False,
    "force_onboarding": False,
    "deps": None,
}
```

Add `import queue, time` to `FigWatch.py` imports.

**Replace `_bg_init` with `_app_init`:**
```
_app_init:
  → validate PAT
  → load trigger config
  → run migration (recents → watched)
  → _start_workers()
  → for each watched file: _start_watcher(f, initial_delay)
  → refresh popover on main thread
```

**Add `_start_workers()`** — launches one daemon thread per queue calling `_worker_loop(q)`.

**Add `_worker_loop(q)`** — `while True: item = q.get(); process_work_item(item) if item else break`.

**Add `_start_watcher(f, initial_delay=0)`:**
- Stops existing watcher for key if present
- Initialises `file_statuses[key] = {"status": "live", ...}`
- Creates `on_status` dispatcher routing events to `file_statuses` + `_schedule_refresh()` + `_update_icon_state()`
- Creates `dispatch(item)` routing `@tone` → `work_queue_tone`, everything else → `work_queue_ux`
- Constructs and starts `FigmaWatcher` with `initial_delay`, `dispatch`, `on_status`

**Add `_stop_watcher(key)`** — stops watcher, removes from `watchers` + `file_statuses`.

**Add `_stop_all_watchers()`** — iterates all watchers, stops each, drains queues with `None` sentinels.

**Add `_update_icon_state()`** — if any `file_statuses` entry has `status` in `("processing", "detected")` → active icon, else idle. Must run on main thread.

**Add `_schedule_refresh()`** — debounced (100ms) popover rebuild, main thread only.

**Add `_revertStatusToLive_(timer)`** — called by 4s NSTimer after reply; reads `timer.userInfo()` as file key.

**Stagger starts:** `offset = (30 / n) * i` per file when `n > 1`.

---

### Step 5 — `FigWatch.py`: rewrite `build_popover_view`

**Remove:** "Open in Figma" section, "Recent" section, file-click-to-watch behaviour, single-watcher status block.

**Empty state** (no watched files):
```
No files being watched.
Paste a Figma URL to get started.
[figma.com/design/…          Watch]
```
Inline `NSTextField` + Watch button → `doAddFileInline_` action.

**File list** (one row per `file_statuses` entry):

Row height: 30px normal, 48px when `status == "processing"` (sub-row shows `@trigger · username`).

Status indicators:
| Status | Display |
|---|---|
| `live` | `● live` green |
| `detected` | `⚡ @trigger` amber |
| `processing` | `⟳ Claude` amber + sub-row |
| `replied` | `✓ replied` green |
| `error` | `⚠ error` red, tappable → `doShowError_:` |

× button on right edge → `doRemoveFile_:` → `_stop_watcher(key)` + `_remove_watched(key)`.

**Footer of file list:**
```
+ Add file…
```
Button → `doAddFile_` (renamed from `doUrl_`) → URL alert → `_add_watched` + `_start_watcher`.

**Header:** `"FigWatch  ·  N watching  ⚙"` or just `"FigWatch  ⚙"` when empty.

**`check_deps()` update:**
- Remove `open_files` parameter
- Remove `"figma"` key
- Remove Figma Desktop row from `build_onboarding_view`
- Onboarding now checks only: Claude installed, Claude logged in, Figma PAT

---

### Step 6 — `FigWatch.py`: Settings panel — Triggers section

Add to existing `doSettings_` accessory view, below Reply Language:

```
Triggers                          + Add
─────────────────────────────────────────
@tone   tone-reviewer (built-in)    ✕
@ux     figma-ux-eval (built-in)    ✕
@a11y   accessibility               ✕
        ✅ Skill is compatible
```

**+ Add flow:**
1. Alert with trigger keyword field + skill path field + "Browse…" `NSOpenPanel` button
2. On confirm: run `introspect_skill` async (background thread), show spinner badge → replace with ✅ or ⚠️ result
3. Append to config, `_save_trigger_config()`, call `w.reload_trigger_config()` on all watchers — no restart

**Worker count setting** (add to Settings panel):
```
Max concurrent workers
  Tone: [2 ▾]   UX: [1 ▾]
  (range 1–5 each)
```
On save: drain and rebuild worker pools. Default tone=2, ux=1.

**`_find_skills()`** — used to populate skill picker dropdown. Scans 4 directories.

**`_save_trigger_config(trigger_config)`** — writes `"triggers"` key to `~/.figwatch/config.json`.

---

### Step 7 — `FigWatch.py`: remove all CDP dead code

Delete:
- `_cdp_relaunch_attempted` global
- `_relaunch_figma_with_cdp()`
- `_is_figma_running()`
- `_get_open_files()`
- `_bg_init()`
- `startCdpTimer_`
- `cdpTick_`
- `_refresh_cdp()`
- `_check_watcher_health()`
- `_check_daemon()`
- `_is_watching()` (replaced by `len(self._state["watchers"]) > 0`)
- `fileClick_`
- `recentClick_`
- `doOpenFigma_`
- `_do_start()` (single-watcher version)
- `_do_stop()` (single-watcher version)
- `FIGMA_CLI_PATH` constant
- Debug code added during debugging session (`_dbg`, `_DBGLOG`, Quartz test)

---

### Step 8 — Version bump + login item check

- `VERSION = "1.2.0"` in `FigWatch.py`
- Verify FigWatch registers as a login item (check `Info.plist` `LSUIElement` + `SMLoginItemSetEnabled` or `LaunchAgent`). If not already handled, add login item registration in `applicationDidFinishLaunching_` using `SMAppService` (macOS 13+).

---

### Step 9 — `lib/python3.9/handlers/` sync

During development, `Resources/handlers/` is the source of truth. After creating `generic.py`:
- Manually copy to `lib/python3.9/handlers/generic.py` for the installed dev build
- Ensure `setup.py` (at repo root) lists `handlers` package so py2app picks up `generic.py` at release build time
- Do NOT compile `.pyc` manually — Olivia's fix already strips them from `lib/python39.zip`

---

## Architectural risks

| Risk | Mitigation |
|---|---|
| `NSTimer` userInfo — use `timer.userInfo()` not direct arg in `_revertStatusToLive_` | Document in code comment |
| `file_statuses` written from background threads | CPython GIL protects simple dict writes; acceptable for status display |
| Introspection cold-start (~2-3s on first custom skill add) | Run async in background thread, show spinner in Settings UI |
| `lib/python3.9/handlers/generic.py` not in py2app bundle | Add to `setup.py` packages list before release build |
| Worker count > available Claude API quota | Configurable in settings, default conservative (tone=2, ux=1) |
| `recent-watches.json` migration on first launch | Silent one-way copy, keep original file in place as backup |

---

## Files summary

| File | Action |
|---|---|
| `watcher.py` | Major refactor — split poll_once, add WorkItem, remove hardcoded HANDLERS |
| `handlers/generic.py` | Create new |
| `handlers/tone.py` | Keep — used as builtin:tone fallback via `execute_builtin_tone` |
| `handlers/ux.py` | Keep — used as builtin:ux fallback via `execute_builtin_ux` |
| `FigWatch.py` | Major refactor — new state, new UI, remove CDP, add trigger settings |
| `lib/python3.9/handlers/generic.py` | Create (copy of Resources version) for dev installs |
| `~/.figwatch/watched-files.json` | New runtime file |
| `~/.figwatch/skill-cache.json` | New runtime file |
