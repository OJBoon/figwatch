#!/usr/bin/env python3
"""FigWatch — macOS menu bar app for watching Figma comments."""

import os
import sys

# Allow running directly from the macos/ directory (dev mode) without installing the package.
# When built with py2app the figwatch package is bundled; this is only needed for development.
_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

import json
import queue
import re
import ssl
import subprocess
import threading
import time
import urllib.request

import certifi

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

import objc
from AppKit import *
from Foundation import *
from PyObjCTools import AppHelper

import figwatch.handlers as handlers
from figwatch.domain import (
    STATUS_LIVE,
    AuditStatus,
)

# String status values for UI state tracking
STATUS_DETECTED = AuditStatus.DETECTED.value
STATUS_PROCESSING = AuditStatus.PROCESSING.value
STATUS_REPLIED = AuditStatus.REPLIED.value
STATUS_ERROR = AuditStatus.ERROR.value

# ── Config ──────────────────────────────────────────────────────────

import contextlib

from figwatch import __version__ as VERSION
from figwatch.gateway import gateway_info

RELEASES_API = "https://api.github.com/repos/OJBoon/figwatch/releases/latest"
RELEASES_URL = "https://github.com/OJBoon/figwatch/releases/latest"

HOME = os.path.expanduser("~")
CONFIG_DIR = os.path.join(HOME, ".figwatch")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
RECENTS_PATH = os.path.join(CONFIG_DIR, "recent-watches.json")
WATCHED_PATH = os.path.join(CONFIG_DIR, "watched-files.json")
FIGMA_SETTINGS_PATH = os.path.join(HOME, "Library", "Application Support", "Figma", "settings.json")

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))

# Resolve claude CLI path — evaluated lazily so it picks up installs that
# happened after FigWatch launched, and so it can search more locations.
_CLAUDE_COMMON_PATHS = [
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    os.path.expanduser("~/.local/bin/claude"),
    os.path.expanduser("~/.volta/bin/claude"),
    os.path.expanduser("~/.bun/bin/claude"),
    os.path.expanduser("~/.npm-global/bin/claude"),
    "/opt/local/bin/claude",
]

_claude_cache = {"path": None, "ts": 0}
_CLAUDE_CACHE_TTL = 30  # seconds


def _resolve_claude_path():
    """Find the claude CLI, with a 30s TTL cache to avoid repeated fs probing."""
    now = time.monotonic()
    if _claude_cache["path"] and (now - _claude_cache["ts"]) < _CLAUDE_CACHE_TTL:
        return _claude_cache["path"]
    for p in _CLAUDE_COMMON_PATHS:
        if os.path.exists(p):
            _claude_cache["path"] = p
            _claude_cache["ts"] = now
            return p
    import shutil
    augmented = ":".join([
        "/opt/homebrew/bin",
        "/usr/local/bin",
        os.path.expanduser("~/.local/bin"),
        os.path.expanduser("~/.volta/bin"),
        os.path.expanduser("~/.bun/bin"),
        os.environ.get("PATH", "/usr/bin:/bin"),
    ])
    found = shutil.which("claude", path=augmented) or "claude"
    _claude_cache["path"] = found
    _claude_cache["ts"] = now
    return found


def _skill_claude_path():
    """Which backend skills should use.

    When a gateway is active (cc-switch / env / a manual ~/.figwatch entry),
    return 'api' so audits run through the Anthropic Messages API directly —
    no ``claude`` CLI needed. Otherwise resolve the personal-login CLI path.
    """
    return "api" if gateway_info() else _resolve_claude_path()


W = 320
PAD = 12
ROW_H = 28
MAX_FILE_ROWS = 12  # scroll if more than this
POPOVER_CORNER_RADIUS = 16  # match macOS Tahoe native menu/popover rounding
# Insets between the settings glass edge and its content (NSAlert used to supply
# these; our own panel must add them or the rows run flush to the glass edge).
SETTINGS_MARGIN_X = 20
SETTINGS_MARGIN_Y = 16

# Auto-watch poll interval choices (seconds), offered in Settings. 15s is the
# floor — shorter risks Figma rate limits when many files are watched.
INTERVAL_PRESETS = [15, 30, 45, 60]
DEFAULT_INTERVAL = 30

# Lightweight mention that engages the bot for a conversational follow-up in a
# thread it has already audited (see figwatch/watcher.py detect_triggers).
FOLLOWUP_MENTION = "@figwatch"

# macOS 26 (Tahoe) Liquid Glass container; None on older macOS (we fall back to
# NSVisualEffectView). Looked up dynamically so the app still runs pre-Tahoe.
try:
    _GLASS_CLS = objc.lookUpClass("NSGlassEffectView")
except Exception:
    _GLASS_CLS = None


def _load_config():
    for path in [CONFIG_PATH, os.path.join(HOME, ".figma-ds-cli", "config.json")]:
        try:
            with open(path) as f:
                config = json.load(f)
                if config.get("figmaPat"):
                    return config
        except Exception:
            pass
    return {}

def _save_config(c):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(c, f, indent=2)


# ── Watched files persistence ──────────────────────────────────────

def _load_watched():
    """Load watched files list. Returns list of {"key": str, "name": str, "figjam": bool}."""
    try:
        with open(WATCHED_PATH) as f:
            return json.load(f)
    except Exception:
        return []

def _save_watched(files):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(WATCHED_PATH, "w") as f:
        json.dump(files, f, indent=2)

def _add_watched(key, name, figjam=False):
    """Upsert a file into the watched list."""
    files = [f for f in _load_watched() if f["key"] != key]
    files.insert(0, {"key": key, "name": name, "figjam": figjam})
    _save_watched(files)

def _remove_watched(key):
    """Remove a file from the watched list."""
    files = [f for f in _load_watched() if f["key"] != key]
    _save_watched(files)


# ── Recents (kept for migration) ──────────────────────────────────

def _load_recents():
    try:
        with open(RECENTS_PATH) as f:
            return json.load(f)
    except Exception:
        return []


# ── Figma file detection ──────────────────────────────────────────

def _scan_figma_files():
    """Read Figma Desktop's settings.json to find open files.

    Returns list of {"key": str, "name": str, "figjam": bool}.
    """
    try:
        with open(FIGMA_SETTINGS_PATH, encoding='utf-8') as f:
            settings = json.load(f)
    except Exception:
        return []

    files = []
    seen = set()
    for window in settings.get("windows", []):
        for tab in window.get("tabs", []):
            path = tab.get("path", "")
            title = tab.get("title", "")
            editor = tab.get("editorType", "")
            m = re.search(r"/(?:file|design|board)/([a-zA-Z0-9]+)", path)
            if m and m.group(1) not in seen:
                key = m.group(1)
                seen.add(key)
                files.append({
                    "key": key,
                    "name": title or key,
                    "figjam": editor == "figjam" or "/board/" in path,
                })
    return files


# ── Helpers ────────────────────────────────────────────────────────

def _extract_key(s):
    m = re.search(r"figma\.com/(?:design|file|board)/([a-zA-Z0-9]+)", s)
    if m:
        return m.group(1)
    s = s.strip()
    return s if re.match(r"^[a-zA-Z0-9]{10,}$", s) else None

def _figma_get(path, pat):
    try:
        req = urllib.request.Request(f"https://api.figma.com/v1{path}", headers={"X-Figma-Token": pat})
        with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as r:
            return json.loads(r.read())
    except Exception:
        return None

def _parse_version(s):
    try:
        return tuple(int(x) for x in s.lstrip("v").strip().split(".")[:3])
    except Exception:
        return (0, 0, 0)


def _fetch_latest_release():
    try:
        req = urllib.request.Request(RELEASES_API, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "FigWatch",
        })
        with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as r:
            d = json.loads(r.read())
        zip_url = next(
            (a.get("browser_download_url") for a in d.get("assets", [])
             if a.get("name", "").lower().endswith(".zip")),
            None,
        )
        return {
            "tag": d.get("tag_name", ""),
            "name": d.get("name", ""),
            "url": d.get("html_url", RELEASES_URL),
            "body": d.get("body", ""),
            "zip_url": zip_url,
            "published_at": d.get("published_at", ""),
        }
    except Exception:
        return None


def _fallback_reply(kind, lang, trigger):
    """Localized ack / error text posted to Figma. The AI audit text is localized
    by the skill; these fixed status and fallback messages are localized here too
    so they honour the reply-language setting (`en` / `cn`)."""
    cn = lang == "cn"
    if kind == "ack":
        if cn:
            return f"⏳ 已收到 {trigger} 审查请求，正在处理…"
        return f"⏳ {trigger} audit received — working on it…"
    if kind == "too_large":
        if cn:
            return (
                f"⚠️ 无法截取该画面的截图，它可能太大或太复杂，Figma 无法一次性渲染。"
                f"如果这是完整的用户旅程，请在其中某个具体页面上触发 @{trigger}，我会单独审查。"
                f"\n\n— FigWatch"
            )
        return (
            f"⚠️ I couldn't capture a screenshot of this frame — it may be too large or "
            f"complex for Figma to render as a single image. For a big user journey, "
            f"trigger @{trigger} on a specific screen inside it and I'll review that.\n\n— FigWatch"
        )
    if cn:
        return (
            f"⚠️ 暂时无法完成 {trigger} 审查。该画面的设计数据可能暂不可用，或服务繁忙。"
            f"请重新触发以重试。\n\n— FigWatch"
        )
    return (
        f"⚠️ Couldn't complete the {trigger} audit right now. This frame's design data "
        f"may be unavailable, or the service was busy. Re-trigger to retry.\n\n— FigWatch"
    )


def _validate_token(pat):
    """Return (handle, None) on success or (None, error_string) on failure."""
    try:
        req = urllib.request.Request(
            "https://api.figma.com/v1/me",
            headers={"X-Figma-Token": pat},
        )
        with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as r:
            d = json.loads(r.read())
        handle = d.get("handle")
        if handle:
            return handle, None
        return None, f"Unexpected API response: {d}"
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        return None, f"Network error: {e.reason}"
    except Exception as e:
        return None, str(e)


def _all_required(deps):
    """Whether every required dependency is satisfied, for the active auth mode.

    In gateway mode the audit runs via the Anthropic Messages API (no CLI), so
    the `claude` CLI is NOT required — only the Figma PAT and an active gateway.
    Personal-login mode still needs the CLI + login.
    """
    if deps.get("claude_auth", {}).get("mode") == "gateway":
        return deps["claude_auth"]["ok"] and deps["pat"]["ok"]
    return deps["claude"]["ok"] and deps["claude_auth"]["ok"] and deps["pat"]["ok"]


def check_deps():
    """Check all dependencies. Returns dict of status per dep."""
    deps = {}

    claude_path = _resolve_claude_path()
    claude_ok = claude_path != "claude" and os.path.exists(claude_path)
    deps["claude"] = {"ok": claude_ok, "path": claude_path if claude_ok else None}

    # Gateway-first: if a company/custom gateway is configured for the Claude CLI
    # (e.g. an active cc-switch profile writes ANTHROPIC_BASE_URL + token into
    # ~/.claude/settings.json), auth is satisfied by the gateway token. The
    # OAuth-only `claude auth status` check reports loggedIn:false under token
    # auth, so we must not fall through to it. Personal `claude login` remains
    # the fallback when no gateway is configured.
    gw = gateway_info()
    if gw:
        deps["claude_auth"] = {"ok": True, "mode": "gateway", "host": gw["host"]}
    elif claude_ok:
        try:
            result = subprocess.run(
                [claude_path, 'auth', 'status', '--json'],
                capture_output=True, timeout=5,
                env=handlers.subprocess_env(),
            )
            stdout = result.stdout.decode('utf-8', errors='replace').strip()
            logged_in = False
            if stdout:
                try:
                    parsed = json.loads(stdout)
                    logged_in = bool(parsed.get("loggedIn"))
                except Exception:
                    low = stdout.lower()
                    logged_in = result.returncode == 0 and "not logged" not in low and "not authenticated" not in low
            deps["claude_auth"] = {"ok": logged_in, "mode": "personal"}
        except Exception:
            deps["claude_auth"] = {"ok": False, "mode": "personal"}
    else:
        deps["claude_auth"] = {"ok": False, "mode": "personal"}

    config = _load_config()
    deps["pat"] = {"ok": bool(config.get("figmaPat"))}

    deps["all_required"] = _all_required(deps)
    return deps


# ── Key Panel (borderless but accepts key/mouse) ──────────────────

class KeyPanel(NSPanel):
    """Borderless panel that can become key window (for button clicks)."""
    def canBecomeKeyWindow(self):
        return True


# ── Flipped View (Y=0 at top) ──────────────────────────────────────

class _UpdateCancelled(Exception):
    """Raised inside the download loop when the user hits Cancel."""


class FlippedView(NSView):
    def isFlipped(self): return True
    def drawRect_(self, rect): pass  # don't fill background


# ── Hover Row ───────────────────────────────────────────────────────

class HoverRow(NSButton):
    def initWithFrame_(self, frame):
        self = objc.super(HoverRow, self).initWithFrame_(frame)
        if self:
            self.setBordered_(False)
            self.setTitle_("")
            self.setWantsLayer_(True)
            self.layer().setCornerRadius_(6)
        return self

    def updateTrackingAreas(self):
        objc.super(HoverRow, self).updateTrackingAreas()
        for ta in list(self.trackingAreas()): self.removeTrackingArea_(ta)
        ta = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(),
            NSTrackingMouseEnteredAndExited | NSTrackingActiveAlways | NSTrackingInVisibleRect,
            self, None)
        self.addTrackingArea_(ta)

    def mouseEntered_(self, event):
        self.layer().setBackgroundColor_(
            NSColor.labelColor().colorWithAlphaComponent_(0.08).CGColor())
        objc.super(HoverRow, self).mouseEntered_(event)

    def mouseExited_(self, event):
        self.layer().setBackgroundColor_(None)
        objc.super(HoverRow, self).mouseExited_(event)

    def mouseDown_(self, event):
        self.layer().setBackgroundColor_(
            NSColor.labelColor().colorWithAlphaComponent_(0.15).CGColor())
        objc.super(HoverRow, self).mouseDown_(event)

    def mouseUp_(self, event):
        self.layer().setBackgroundColor_(None)
        if self.target() and self.action():
            NSApp.sendAction_to_from_(self.action(), self.target(), self)
        objc.super(HoverRow, self).mouseUp_(event)


# ── UI Builder ──────────────────────────────────────────────────────

def _skill_display_name(skill_path):
    """Human name for a custom skill path.

    Custom skills live at <dir>/<name>/skill.md, so when the file is the
    conventional skill.md/SKILL.md the parent folder is the skill's name —
    os.path.basename(path) would just say "skill.md". For a loose .md file
    (not the conventional name) use its own filename stem instead.
    """
    fname = os.path.basename(skill_path)
    stem, _ = os.path.splitext(fname)
    if fname.lower() in ("skill.md", "skill.markdown"):
        parent = os.path.basename(os.path.dirname(skill_path))
        if parent:
            return parent
    return stem


def _label(text, size=13, weight=NSFontWeightRegular, color=None, mono=False):
    l = NSTextField.alloc().init()
    l.setStringValue_(text)
    l.setBezeled_(False); l.setEditable_(False)
    l.setSelectable_(False); l.setDrawsBackground_(False)
    if mono:
        l.setFont_(NSFont.monospacedSystemFontOfSize_weight_(size, weight))
    else:
        l.setFont_(NSFont.systemFontOfSize_weight_(size, weight))
    if color: l.setTextColor_(color)
    l.setLineBreakMode_(NSLineBreakByTruncatingTail)
    l.sizeToFit()
    return l


def _dynamic_color(light, dark):
    """Appearance-adaptive NSColor: resolves per effectiveAppearance at draw time.

    Lets a single color read well in both light and dark mode (e.g. a warm accent
    that would be too pale on white but is fine on a dark glass). Falls back to the
    light color on very old macOS where dynamic providers are unavailable.
    """
    def _provider(appearance):
        name = appearance.bestMatchFromAppearancesWithNames_(
            [NSAppearanceNameAqua, NSAppearanceNameDarkAqua])
        return dark if name == NSAppearanceNameDarkAqua else light
    try:
        return NSColor.colorWithName_dynamicProvider_(None, _provider)
    except Exception:
        return light


def _sf_symbol(name, size=13, color=None):
    img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, name)
    if not img: return None
    iv = NSImageView.alloc().initWithFrame_(NSMakeRect(0, 0, size + 4, size + 4))
    cfg = NSImageSymbolConfiguration.configurationWithPointSize_weight_(size, 0.0)
    img = img.imageWithSymbolConfiguration_(cfg)
    if color:
        iv.setContentTintColor_(color)
    iv.setImage_(img)
    iv.setImageScaling_(2)  # NSImageScaleProportionallyUpOrDown
    return iv


def _emoji_icon(emoji, size=64):
    """Render an emoji as an NSImage (used for the Settings dialog icon)."""
    img = NSImage.alloc().initWithSize_(NSMakeSize(size, size))
    img.lockFocus()
    attrs = {NSFontAttributeName: NSFont.systemFontOfSize_(int(size * 0.78))}
    text = NSAttributedString.alloc().initWithString_attributes_(emoji, attrs)
    ts = text.size()
    text.drawAtPoint_(NSMakePoint((size - ts.width) / 2.0, (size - ts.height) / 2.0))
    img.unlockFocus()
    return img


def _rounded_mask_image(radius):
    """Resizable rounded-rect mask for NSVisualEffectView.maskImage. capInsets
    pin the corners while the 1pt centre stretches, so one image masks any panel
    size with clean antialiased corners — and a correctly rounded drop shadow.
    (Masking a vibrancy view's own layer instead leaves aliased corners and a
    square shadow.)"""
    edge = radius * 2 + 1
    img = NSImage.alloc().initWithSize_(NSMakeSize(edge, edge))
    img.setCapInsets_(NSEdgeInsetsMake(radius, radius, radius, radius))
    img.setResizingMode_(1)  # NSImageResizingModeStretch
    img.lockFocus()
    NSColor.blackColor().set()
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        NSMakeRect(0, 0, edge, edge), radius, radius).fill()
    img.unlockFocus()
    return img


def _make_glass_view(w, h, radius=POPOVER_CORNER_RADIUS):
    """Liquid-glass content view matching the popover, for any panel.

    Returns (view, is_container). On macOS Tahoe (26+) this is a real
    NSGlassEffectView, which hosts a single content view and draws its own
    rounded shadow; older macOS gets the menu-material NSVisualEffectView with
    a resizable rounded mask. Shared by the popover and the update HUD so both
    read as the same material.
    """
    if _GLASS_CLS is not None:
        glass = _GLASS_CLS.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        glass.setCornerRadius_(radius)
        return glass, True
    glass = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
    glass.setMaterial_(5)  # NSVisualEffectMaterialMenu
    glass.setState_(1)
    glass.setBlendingMode_(0)
    glass.setMaskImage_(_rounded_mask_image(radius))
    return glass, False

    """Build the onboarding/setup checklist. Returns (view, height)."""
    cw = W - PAD * 2
    y = PAD

    root = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, W, 800))

    title = _label("FigWatch Setup", size=15, weight=NSFontWeightBold)
    title.setFrameOrigin_((PAD + 6, y))
    root.addSubview_(title)
    y += 24

    subtitle = _label("Let\u2019s get everything ready.", size=12, color=NSColor.secondaryLabelColor())
    subtitle.setFrameOrigin_((PAD + 6, y))
    root.addSubview_(subtitle)
    y += 24

    auth = deps["claude_auth"]
    if auth["ok"] and auth.get("mode") == "gateway":
        auth_name = "Claude Access"
        auth_desc = f"Connected via gateway ({auth.get('host') or 'gateway'})"
    elif auth["ok"]:
        auth_name = "Claude Login"
        auth_desc = "Signed in to your Claude account"
    else:
        auth_name = "Claude Access"
        auth_desc = "Gateway profile or personal Claude"

    gateway_active = auth["ok"] and auth.get("mode") == "gateway"

    items = []
    if not gateway_active:
        # The `claude` CLI is only needed for the personal-login path; an active
        # gateway drives the audits via the Anthropic API with no CLI.
        items.append({
            "key": "claude",
            "name": "Claude Code",
            "desc": "Powers the AI audits (personal login)",
            "ok": deps["claude"]["ok"],
            "installing": app._state.get("installing_claude"),
            "action": b"doInstallClaude:",
        })
    items.append({
        "key": "claude_auth",
        "name": auth_name,
        "desc": auth_desc,
        "ok": auth["ok"],
        "installing": False,
        "action": b"doClaudeAuth:",
        # When not signed in, offer all three paths: company gateway (cc-switch),
        # manual gateway entry, and personal Claude login.
        "actions": [
            ("Gateway", b"doConnectGateway:"),
            ("Manual", b"doEnterGateway:"),
            ("Personal", b"doClaudeAuth:"),
        ],
    })
    items.append({
        "key": "pat",
        "name": "Figma Token",
        "desc": "Connects to your Figma files",
        "ok": deps["pat"]["ok"],
        "installing": False,
        "action": b"doToken:",
    })

    for item in items:
        has_actions = bool(item.get("actions")) and not item["ok"] and not item["installing"]
        # A two-button auth row is taller so the buttons get their own line and
        # never overlap the (long) description text.
        row_h = 68 if has_actions else 44
        row = NSView.alloc().initWithFrame_(NSMakeRect(PAD, y, cw, row_h))

        # desc sits above name (matching the other rows)
        name_y, desc_y = (30, 46) if has_actions else (6, 24)

        if item["installing"]:
            icon = _label("\u23F3", size=14)
            icon.setFrameOrigin_((6, name_y + 6))
        elif item["ok"]:
            icon = _sf_symbol("checkmark.circle.fill", size=14, color=NSColor.systemGreenColor())
            if icon: icon.setFrameOrigin_((4, name_y + 4))
        else:
            icon = _sf_symbol("xmark.circle.fill", size=14, color=NSColor.systemRedColor())
            if icon: icon.setFrameOrigin_((4, name_y + 4))

        if icon:
            row.addSubview_(icon)

        # Available text width: full width when the buttons sit on their own line
        # (has_actions) or the row is complete (ok, no trailing control); otherwise
        # reserve the right side for the Install/Set Up button or "Installing…".
        text_w = (cw - 32) if (has_actions or item["ok"]) else (cw - 108)

        nl = _label(item["name"], size=13, weight=NSFontWeightMedium)
        nl.setFrameSize_(NSMakeSize(text_w, nl.frame().size.height))
        nl.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
        nl.setFrameOrigin_((28, name_y))
        row.addSubview_(nl)

        dl = _label(item["desc"], size=11, color=NSColor.secondaryLabelColor())
        dl.setFrameSize_(NSMakeSize(text_w, dl.frame().size.height))
        dl.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
        dl.setFrameOrigin_((28, desc_y))
        row.addSubview_(dl)

        if has_actions:
            # Buttons on their own line along the bottom, right-aligned.
            bw = 76
            bx = cw - 8
            for title, selector in reversed(item["actions"]):
                bx -= bw
                b = NSButton.alloc().initWithFrame_(NSMakeRect(bx, 8, bw, 24))
                b.setTitle_(title)
                b.setBezelStyle_(NSBezelStyleRecessed)
                b.setControlSize_(1)
                b.setFont_(NSFont.systemFontOfSize_weight_(11, NSFontWeightMedium))
                b.setTarget_(app)
                b.setAction_(selector)
                row.addSubview_(b)
                bx -= 8
        elif not item["ok"] and not item["installing"]:
            btn_title = "Set Up" if item["key"] == "pat" else "Install"
            btn = NSButton.alloc().initWithFrame_(NSMakeRect(cw - 70, name_y + 2, 62, 24))
            btn.setTitle_(btn_title)
            btn.setBezelStyle_(NSBezelStyleRecessed)
            btn.setControlSize_(1)
            btn.setFont_(NSFont.systemFontOfSize_weight_(11, NSFontWeightMedium))
            btn.setTarget_(app)
            btn.setAction_(item["action"])
            row.addSubview_(btn)
        elif item["installing"]:
            il = _label("Installing\u2026", size=11, color=NSColor.secondaryLabelColor())
            il.setFrameOrigin_((cw - 76, 12))
            row.addSubview_(il)

        root.addSubview_(row)
        y += row_h

    y += 4
    sep = NSBox.alloc().initWithFrame_(NSMakeRect(PAD + 4, y, cw - 8, 1))
    sep.setBoxType_(2)
    root.addSubview_(sep)
    y += 12

    footer = NSView.alloc().initWithFrame_(NSMakeRect(PAD, y, cw, 28))

    check_btn = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 90, 24))
    check_btn.setTitle_("Check Again")
    check_btn.setBezelStyle_(NSBezelStyleRecessed)
    check_btn.setControlSize_(1)
    check_btn.setFont_(NSFont.systemFontOfSize_weight_(11, NSFontWeightMedium))
    check_btn.setTarget_(app)
    check_btn.setAction_(b"doCheckDeps:")
    footer.addSubview_(check_btn)

    if deps["all_required"]:
        cont_btn = NSButton.alloc().initWithFrame_(NSMakeRect(cw - 80, 0, 80, 24))
        cont_btn.setTitle_("Continue \u2192")
        cont_btn.setBezelStyle_(NSBezelStyleRecessed)
        cont_btn.setControlSize_(1)
        cont_btn.setFont_(NSFont.systemFontOfSize_weight_(11, NSFontWeightMedium))
        cont_btn.setTarget_(app)
        cont_btn.setAction_(b"doContinueSetup:")
        footer.addSubview_(cont_btn)

    quit_btn = NSButton.alloc().initWithFrame_(NSMakeRect(cw - 38, 0, 38, 24))
    quit_btn.setBordered_(False)
    quit_btn.setTitle_("Quit")
    quit_btn.setFont_(NSFont.systemFontOfSize_weight_(11, NSFontWeightRegular))
    quit_btn.setContentTintColor_(NSColor.tertiaryLabelColor())
    quit_btn.setTarget_(app)
    quit_btn.setAction_(b"doQuit:")

    if not deps["all_required"]:
        footer.addSubview_(quit_btn)

    root.addSubview_(footer)
    y += 28 + PAD

    root.setFrameSize_(NSMakeSize(W, y))
    return root, y


def build_popover_view(app):
    """Build the main panel content — glass effect, file list, countdown."""
    cw = W - PAD * 2
    y = PAD + 4

    root = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, W, 800))
    file_statuses = app._state.get("file_statuses", {})
    watched = app._state.get("watched", [])

    # ── Header ─────────────────────────────────────────────────
    title = _label("FigWatch", size=15, weight=NSFontWeightBold)
    title.setFrameOrigin_((PAD + 4, y))
    root.addSubview_(title)

    quit_btn = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + cw - 24, y + 1, 20, 20))
    quit_btn.setBordered_(False); quit_btn.setTitle_("")
    qi = _sf_symbol("power", size=12, color=NSColor.tertiaryLabelColor())
    if qi: quit_btn.setImage_(qi.image())
    quit_btn.setTarget_(app); quit_btn.setAction_(b"doPowerMenu:")
    root.addSubview_(quit_btn)
    y += 24

    trigger_config = app._state.get("trigger_config", [])
    triggers_str = " \u00B7 ".join(t.get("trigger", "") for t in trigger_config) if trigger_config else "@tone \u00B7 @ux"
    subline = _label(triggers_str, size=11, color=NSColor.secondaryLabelColor())
    subline.setFrameSize_(NSMakeSize(cw, subline.frame().size.height))
    subline.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
    subline.setFrameOrigin_((PAD + 4, y))
    root.addSubview_(subline)
    y += 20

    sep0 = NSBox.alloc().initWithFrame_(NSMakeRect(PAD, y, cw, 1))
    sep0.setBoxType_(2)
    root.addSubview_(sep0)
    y += 12

    # ── Update banner ──────────────────────────────────────────
    # A gentle "there's a newer version" reminder. Set by the background
    # update check (_check_for_update); clicking it opens the install dialog.
    update = app._state.get("update_available")
    if update:
        banner_h = 32
        accent = NSColor.controlAccentColor()
        banner = NSView.alloc().initWithFrame_(NSMakeRect(PAD - 2, y, cw + 4, banner_h))
        banner.setWantsLayer_(True)
        banner.layer().setCornerRadius_(8)
        banner.layer().setBackgroundColor_(accent.colorWithAlphaComponent_(0.12).CGColor())

        dl = _sf_symbol("arrow.down.circle.fill", size=15, color=accent)
        if dl:
            dl.setFrameOrigin_((10, (banner_h - 19) // 2))
            banner.addSubview_(dl)

        tag = update.get("tag", "")
        msg = f"Update available · {tag}" if tag else "Update available"
        txt = _label(msg, size=12, weight=NSFontWeightMedium, color=accent)
        txt.setFrameSize_(NSMakeSize(cw - 34 - 20, 16))
        txt.setFrameOrigin_((34, (banner_h - 16) // 2))
        banner.addSubview_(txt)

        chev = _sf_symbol("chevron.right", size=11, color=accent)
        if chev:
            chev.setFrameOrigin_((cw + 4 - 22, (banner_h - 15) // 2))
            banner.addSubview_(chev)

        overlay = HoverRow.alloc().initWithFrame_(NSMakeRect(0, 0, cw + 4, banner_h))
        overlay.setTarget_(app); overlay.setAction_(b"doUpdateBanner:")
        banner.addSubview_(overlay)

        root.addSubview_(banner)
        y += banner_h + 8
    # ── File list or empty state ───────────────────────────────
    if not watched:
        no_files = _label("No Figma files detected", size=12, color=NSColor.secondaryLabelColor())
        no_files.setFrameOrigin_((PAD + 4, y))
        root.addSubview_(no_files)
        y += 20

        hint = _label("Open a file in Figma Desktop to start watching.", size=11, color=NSColor.tertiaryLabelColor())
        hint.setFrameOrigin_((PAD + 4, y))
        root.addSubview_(hint)
        y += 24

    else:
        n = len(watched)
        section_title = f"Watching {n} file{'s' if n != 1 else ''}"
        sh = _label(section_title, size=11, weight=NSFontWeightMedium, color=NSColor.secondaryLabelColor())
        sh.setFrameOrigin_((PAD + 4, y))
        root.addSubview_(sh)
        y += 20

        list_y = 0
        list_container = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, cw + 4, 2000))

        for i, f in enumerate(watched):
            key = f["key"]
            status_info = file_statuses.get(key, {})
            status = status_info.get("status", STATUS_LIVE)

            row = NSView.alloc().initWithFrame_(NSMakeRect(0, list_y, cw + 4, ROW_H))
            vc = (ROW_H - 12) // 2

            if status == STATUS_LIVE:
                # Center the bullet in the same 15pt-wide slot the SF-symbol status
                # icons occupy (frame x=4, size 11 \u2192 15pt box) so the status column
                # is optically flush regardless of which glyph a row shows.
                dot = _label("\u25CF", size=8, color=NSColor.systemGreenColor())
                dot.setAlignment_(NSTextAlignmentCenter)
                dot.setFrameSize_(NSMakeSize(15, dot.frame().size.height))
                dot.setFrameOrigin_((4, vc + 2))
                row.addSubview_(dot)
            elif status == STATUS_REPLIED:
                icon = _sf_symbol("checkmark.circle.fill", size=11, color=NSColor.systemGreenColor())
                if icon:
                    icon.setFrameOrigin_((4, vc))
                    row.addSubview_(icon)
            elif status == STATUS_PROCESSING:
                icon = _sf_symbol("arrow.triangle.2.circlepath", size=11, color=NSColor.systemOrangeColor())
                if icon:
                    icon.setFrameOrigin_((4, vc))
                    icon.setWantsLayer_(True)
                    spin = __import__('Quartz').CABasicAnimation.animationWithKeyPath_("transform.rotation.z")
                    spin.setFromValue_(0)
                    spin.setToValue_(-6.28318)
                    spin.setDuration_(1.5)
                    spin.setRepeatCount_(1e9)
                    icon.layer().addAnimation_forKey_(spin, "spin")
                    row.addSubview_(icon)
            elif status == STATUS_DETECTED:
                icon = _sf_symbol("bolt.fill", size=11, color=NSColor.systemOrangeColor())
                if icon:
                    icon.setFrameOrigin_((4, vc))
                    row.addSubview_(icon)
            elif status == STATUS_ERROR:
                icon = _sf_symbol("exclamationmark.triangle.fill", size=11, color=NSColor.systemRedColor())
                if icon:
                    icon.setFrameOrigin_((4, vc))
                    row.addSubview_(icon)

            name_x = 24
            has_badge = status in (STATUS_PROCESSING, STATUS_REPLIED, STATUS_ERROR)
            badge_w = 96 if has_badge else 0
            name_y = (ROW_H - 16) // 2
            nl = _label(f["name"], size=12)
            nl.setFrameSize_(NSMakeSize(cw - name_x - badge_w, 16))
            nl.setFrameOrigin_((name_x, name_y))
            nl.cell().setLineBreakMode_(5)
            row.addSubview_(nl)

            if status == STATUS_PROCESSING:
                trigger = status_info.get("trigger", "")
                user = status_info.get("user", "")
                badge_text = f"{trigger} \u00B7 {user}" if user else trigger
                # systemOrange is fine on the dark glass but only ~2.2:1 on the
                # light glass; use a darker amber in light mode, keep orange in dark.
                badge = _label(badge_text, size=10, color=_dynamic_color(
                    NSColor.colorWithCalibratedRed_green_blue_alpha_(0.72, 0.35, 0.0, 1.0),
                    NSColor.systemOrangeColor()))
                badge.sizeToFit()
                bw = min(badge.frame().size.width, 92)
                badge.setFrameSize_(NSMakeSize(bw, 16))
                badge.setFrameOrigin_((cw - bw, name_y + 1))
                badge.cell().setLineBreakMode_(5)
                row.addSubview_(badge)
            elif status == STATUS_REPLIED:
                badge = _label("\u2713 replied", size=10, color=NSColor.secondaryLabelColor())
                badge.sizeToFit()
                badge.setFrameOrigin_((cw - badge.frame().size.width, name_y + 1))
                row.addSubview_(badge)
            elif status == STATUS_ERROR:
                badge = _label("\u26A0 error", size=10, color=NSColor.systemRedColor())
                badge.sizeToFit()
                badge.setFrameOrigin_((cw - badge.frame().size.width, name_y + 1))
                row.addSubview_(badge)
                err_btn = HoverRow.alloc().initWithFrame_(NSMakeRect(0, 0, cw + 4, ROW_H))
                err_btn.setTag_(i)
                err_btn.setTarget_(app); err_btn.setAction_(b"doShowError:")
                row.addSubview_(err_btn)

            list_container.addSubview_(row)
            list_y += ROW_H

        list_container.setFrameSize_(NSMakeSize(cw + 4, list_y))

        max_list_h = MAX_FILE_ROWS * ROW_H
        visible_h = min(list_y, max_list_h)

        if list_y > max_list_h:
            scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(PAD - 2, y, cw + 4, visible_h))
            scroll.setDocumentView_(list_container)
            scroll.setHasVerticalScroller_(True)
            scroll.setDrawsBackground_(False)
            scroll.setBorderType_(0)
            root.addSubview_(scroll)
        else:
            list_container.setFrameOrigin_(NSMakePoint(PAD - 2, y))
            root.addSubview_(list_container)

        y += visible_h

    # ── Footer ─────────────────────────────────────────────────
    y += 8
    sep = NSBox.alloc().initWithFrame_(NSMakeRect(PAD, y, cw, 1))
    sep.setBoxType_(2)
    root.addSubview_(sep)
    y += 8

    poll_interval = app._state.get("poll_interval", DEFAULT_INTERVAL)
    last_polls = [s.get("last_poll") for s in file_statuses.values() if s.get("last_poll")]
    if last_polls:
        most_recent = max(last_polls)
        elapsed = time.time() - most_recent
        remaining = max(0, int(poll_interval - elapsed))
        countdown_text = f"Checking for comments in {remaining}s" if remaining > 0 else "Checking now\u2026"
    elif watched:
        countdown_text = "Starting\u2026"
    else:
        countdown_text = ""

    footer_h = 24
    footer = NSView.alloc().initWithFrame_(NSMakeRect(PAD, y, cw, footer_h))

    ct = _label(countdown_text or " ", size=11, color=NSColor.tertiaryLabelColor())
    ct.setFrameOrigin_((4, (footer_h - 16) // 2))
    ct.setFrameSize_(NSMakeSize(cw - 32, 16))
    footer.addSubview_(ct)
    app._state["_countdown_label"] = ct

    gear = NSButton.alloc().initWithFrame_(NSMakeRect(cw - 24, (footer_h - 20) // 2, 20, 20))
    gear.setBordered_(False); gear.setTitle_("")
    gi = _sf_symbol("gearshape.fill", size=12, color=NSColor.tertiaryLabelColor())
    if gi: gear.setImage_(gi.image())
    gear.setTarget_(app); gear.setAction_(b"doSettings:")
    footer.addSubview_(gear)

    root.addSubview_(footer)
    y += footer_h + PAD - 2

    root.setFrameSize_(NSMakeSize(W, y))
    return root, y


# ── App ─────────────────────────────────────────────────────────────

class FigWatch(NSObject):
    statusItem = objc.ivar()
    _state = {}

    def applicationDidFinishLaunching_(self, notif):
        self._watcher_log_file = None
        self._tick_count = 0
        self._state = {
            "pat": None, "user": None, "locale": "uk",
            "model": "sonnet", "reply_lang": "en",
            "watched": [],
            "watchers": {},
            "file_statuses": {},
            "work_queue_tone": queue.Queue(),
            "work_queue_ux": queue.Queue(),
            "workers": [],
            "trigger_config": [],
            "installing_claude": False,
            "force_onboarding": False,
            "deps": None,
            "excluded_keys": [],
            "update_available": None,
        }
        config = _load_config()
        self._state["excluded_keys"] = config.get("excludedFiles", [])
        self._state["pat"] = config.get("figmaPat")
        self._state["locale"] = config.get("watchLocale", "uk")
        self._state["model"] = config.get("aiModel", "sonnet")
        self._state["reply_lang"] = config.get("replyLang", "en")
        iv = config.get("watchInterval", DEFAULT_INTERVAL)
        self._state["poll_interval"] = iv if iv in INTERVAL_PRESETS else DEFAULT_INTERVAL
        self._state["thread_followups"] = config.get("threadFollowups", True)

        menubar = NSMenu.alloc().init()
        edit_menu = NSMenu.alloc().initWithTitle_("Edit")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Cut", "cut:", "x")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Copy", "copy:", "c")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Paste", "paste:", "v")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Select All", "selectAll:", "a")
        edit_item = NSMenuItem.alloc().init()
        edit_item.setSubmenu_(edit_menu)
        menubar.addItem_(edit_item)
        NSApp.setMainMenu_(menubar)

        self.statusItem = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        btn = self.statusItem.button()
        self._set_icon(False)
        btn.setTarget_(self); btn.setAction_(b"toggle:")

        self._panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, 400),
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered, False)
        self._panel.setLevel_(NSPopUpMenuWindowLevel)
        self._panel.setFloatingPanel_(True)
        self._panel.setHasShadow_(True)
        self._panel.setOpaque_(False)
        self._panel.setBackgroundColor_(NSColor.clearColor())
        # macOS Tahoe (26+) Liquid Glass matches Control Center exactly. Use the
        # real NSGlassEffectView when available — it renders the glass and its own
        # rounded shadow, and cornerRadius shapes it natively. Fall back to the
        # menu material + rounded-rect maskImage on older macOS (class absent).
        glass, self._glass_is_container = _make_glass_view(W, 400)
        self._panel.setContentView_(glass)

        self._register_login_item()

        if self._state["pat"]:
            threading.Thread(target=self._app_init, daemon=True).start()

        # Confirm a just-completed self-update (the download/quit/relaunch is
        # otherwise silent). The marker is written right before the update quit.
        pending = config.get("pendingUpdate")
        if pending:
            c = _load_config()
            c.pop("pendingUpdate", None)
            _save_config(c)
            # Stays until the user clicks Done — no auto-hide to race the button.
            self._show_update_hud(f"Updated to {pending} ✓", spinning=False,
                                  button="Done", button_action=b"hudDismiss:")

    # ── Logging ─────────────────────────────────────────────────

    def _write_log(self, msg):
        if self._watcher_log_file is None:
            self._watcher_log_file = open("/tmp/fw-watcher.log", "a", encoding="utf-8")
        ts = time.strftime("%H:%M:%S")
        self._watcher_log_file.write(f"[{ts}] {msg}\n")
        self._watcher_log_file.flush()

    def _register_login_item(self):
        """Register FigWatch as a login item via SMAppService (macOS 13+)."""
        try:
            from ServiceManagement import SMAppService
            service = SMAppService.mainAppService()
            if service.status() != 1:
                service.registerAndReturnError_(None)
        except Exception:
            pass

    # ── Init ───────────────────────────────────────────────────

    def _app_init(self):
        """Background init: validate PAT, load config, start watchers."""
        self._state["user"], _ = _validate_token(self._state["pat"])

        from figwatch.trigger_config import load_trigger_config
        self._state["trigger_config"] = load_trigger_config()

        if not os.path.exists(WATCHED_PATH) and os.path.exists(RECENTS_PATH):
            recents = _load_recents()
            if recents:
                _save_watched(recents)

        self._check_for_update()

        self._start_workers()
        self._sync_watchers()
        threading.Thread(target=self._sync_loop, daemon=True).start()

    def _check_for_update(self):
        """Fetch the latest release and flag the popover banner if it's newer.

        Runs off the main thread (called from _app_init at launch and from
        _sync_loop every few hours). Also seeds installedReleaseTimestamp on
        first run so update bookkeeping happens even when already up to date.
        """
        rel = _fetch_latest_release()
        if not rel:
            return
        config = _load_config()
        if not config.get("installedReleaseTimestamp") and rel.get("published_at"):
            config["installedReleaseTimestamp"] = rel["published_at"]
            _save_config(config)
        newer = _parse_version(rel.get("tag", "")) > _parse_version(VERSION)
        self._state["update_available"] = rel if newer else None
        if newer and self._state.get("popover_open"):
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                b"rebuildPopoverFromThread:", None, False)

    def _sync_loop(self):
        last_update_check = time.time()
        while True:
            time.sleep(10)
            try:
                self._sync_watchers()
                if time.time() - last_update_check > 6 * 3600:
                    last_update_check = time.time()
                    self._check_for_update()
            except Exception as e:
                self._write_log(f'\u26a0\ufe0f Sync error: {e}')

    def _sync_watchers(self):
        detected = _scan_figma_files()
        manual = self._state.get("_manual_watched", _load_watched())
        self._state["_manual_watched"] = manual
        excluded = set(self._state.get("excluded_keys", []))

        merged = {}
        for f in manual:
            merged[f["key"]] = f
        for f in detected:
            if f["key"] not in merged and f["key"] not in excluded:
                merged[f["key"]] = f

        desired_keys = set(merged.keys())
        current_keys = set(self._state.get("watchers", {}).keys())

        new_keys = desired_keys - current_keys
        if new_keys:
            n = len(new_keys)
            for i, key in enumerate(new_keys):
                delay = (self._state.get("poll_interval", DEFAULT_INTERVAL) / n) * i if n > 1 else 0
                self._start_watcher(merged[key], initial_delay=delay)

        manual_keys = {f["key"] for f in manual}
        stale_keys = current_keys - desired_keys
        for key in stale_keys:
            if key not in manual_keys:
                self._stop_watcher(key)

        ordered = list(manual)
        seen = {f["key"] for f in manual}
        for f in detected:
            if f["key"] not in seen and f["key"] not in excluded:
                ordered.append(f)
                seen.add(f["key"])
        self._state["watched"] = ordered

        if new_keys or stale_keys:
            self._schedule_refresh()

    def _start_workers(self):
        config = _load_config()
        tone_count = config.get("workersTone", 2)
        ux_count = config.get("workersUx", 1)
        self._state["_tone_worker_count"] = tone_count
        self._state["_ux_worker_count"] = ux_count

        for _ in range(tone_count):
            t = threading.Thread(target=self._worker_loop, args=(self._state["work_queue_tone"],), daemon=True)
            t.start()
            self._state["workers"].append(t)

        for _ in range(ux_count):
            t = threading.Thread(target=self._worker_loop, args=(self._state["work_queue_ux"],), daemon=True)
            t.start()
            self._state["workers"].append(t)

    def _set_file_status(self, key, status, *, trigger=None, user=None, error=None,
                         revert_after=None):
        """Drive a watched file's row status from the worker thread, then refresh
        the popover + menu-bar icon. Pass revert_after to fall back to LIVE after
        a delay (the brief REPLIED flash once an audit succeeds)."""
        fs = self._state["file_statuses"].get(key)
        if fs is None:
            return
        fs["status"] = status
        if trigger is not None:
            fs["trigger"] = trigger
        if user is not None:
            fs["user"] = user
        fs["error"] = error
        self._schedule_refresh()
        self._update_icon_state()
        if revert_after is not None:
            def _revert():
                cur = self._state.get("file_statuses", {}).get(key)
                if cur is not None and cur.get("status") == status:
                    cur["status"] = STATUS_LIVE
                    self._schedule_refresh()
                    self._update_icon_state()
            threading.Timer(revert_after, _revert).start()

    def _worker_loop(self, q):
        seen = set()
        while True:
            item = q.get()
            if item is None:
                break
            audit, ack_id = item
            comment_id = audit.comment.comment_id
            svc = self._state.get("audit_service")
            if comment_id in seen:
                # The ack was already posted at dispatch; delete it so a skipped
                # duplicate doesn't leave a dangling "working on it\u2026" reply.
                if ack_id and svc:
                    with contextlib.suppress(Exception):
                        svc.delete_ack(audit, ack_id)
                continue
            seen.add(comment_id)
            if not svc:
                continue
            trigger_name = audit.trigger_match.trigger.keyword.lstrip('@')
            node_id = audit.comment.node_id
            file_key = audit.comment.file_key
            self._write_log(f"\u2699\ufe0f  Starting {trigger_name} audit on {file_key} node {node_id}")
            self._set_file_status(
                file_key, STATUS_PROCESSING,
                trigger=audit.trigger_match.trigger.keyword,
                user=audit.comment.user_handle,
            )
            try:
                response = svc.execute(audit)
                self._write_log(f"\u2705 Audit complete ({len(response)} chars)")
                if ack_id:
                    svc.delete_ack(audit, ack_id)
                svc.post_reply(audit, response)
                self._write_log("\u2709\ufe0f  Reply posted")
                self._set_file_status(file_key, STATUS_REPLIED, revert_after=4.0)
            except Exception as e:
                self._write_log(f"\u26a0\ufe0f Audit failed: {e}")
                self._set_file_status(file_key, STATUS_ERROR, error=str(e))
                # Post a short reply instead of going silent: the reviewer gets
                # feedback, and because the trigger now has a reply it won't be
                # re-audited on the next poll (avoids a bad frame clogging the worker).
                # A screenshot-capture miss is usually a too-large multi-screen frame;
                # everything else gets the generic "busy, re-trigger" reply.
                err = str(e)
                kind = "too_large" if ("required data" in err and "screenshot" in err) else "generic"
                reply = _fallback_reply(kind, self._state.get("reply_lang", "en"), trigger_name)
                with contextlib.suppress(Exception):
                    if ack_id:
                        svc.delete_ack(audit, ack_id)
                    svc.post_reply(audit, reply)
                    self._write_log("✉️  Error reply posted")

    def _build_audit_service(self):
        """Construct AuditService for macOS polling path."""
        from figwatch.providers.figma import (
            FigmaCommentRepository,
            FigmaDesignDataRepository,
        )
        from figwatch.services import AuditConfig, AuditService

        pat = self._state["pat"]
        return AuditService(
            comment_repo=FigmaCommentRepository(pat),
            design_repo=FigmaDesignDataRepository(pat),
            config=AuditConfig(
                model=self._state.get("model", "sonnet"),
                claude_path=_skill_claude_path(),
                reply_lang=self._state.get("reply_lang", "en"),
                locale=self._state.get("locale", "uk"),
            ),
            trigger_config=self._state["trigger_config"],
        )

    def _start_watcher(self, f, initial_delay=0):
        from figwatch.watcher import FigmaWatcher
        key = f["key"]

        if key in self._state["watchers"]:
            self._stop_watcher(key)

        self._state["file_statuses"][key] = {
            "status": STATUS_LIVE, "trigger": "", "user": "",
            "last_poll": None, "error": None,
        }

        # Lazily construct audit service
        if "audit_service" not in self._state:
            self._state["audit_service"] = self._build_audit_service()

        def dispatch(audit):
            trigger_kw = audit.trigger_match.trigger.keyword
            trigger_name = trigger_kw.lstrip("@")
            # Post the ack the instant the poll detects the trigger, on the poll
            # thread -- NOT when a worker dequeues it. Otherwise a busy worker
            # (audits take 30-120s) makes the "working on it" reply wait behind
            # the whole queue. The worker later replaces/deletes it via ack_id.
            svc = self._state.get("audit_service")
            ack_id = None
            if svc:
                try:
                    ack_id = svc.post_ack(
                        audit,
                        _fallback_reply("ack", self._state.get("reply_lang", "en"), trigger_name),
                    )
                    self._write_log("✉️  Ack posted")
                except Exception as e:
                    self._write_log(f"⚠️ Ack failed: {e}")
            q_key = "work_queue_tone" if trigger_kw == "@tone" else "work_queue_ux"
            self._state[q_key].put((audit, ack_id))

        def on_poll():
            if key in self._state["file_statuses"]:
                self._state["file_statuses"][key]["last_poll"] = time.time()

        w = FigmaWatcher(
            key, self._state["pat"],
            audit_service=self._state["audit_service"],
            log=self._write_log,
            trigger_config=self._state["trigger_config"],
            dispatch=dispatch,
            on_poll=on_poll,
            initial_delay=initial_delay,
            interval=self._state.get("poll_interval", DEFAULT_INTERVAL),
            follow_up_mention=(FOLLOWUP_MENTION
                               if self._state.get("thread_followups", True) else None),
        )
        w.start()
        self._state["watchers"][key] = w
        self._update_icon_state()

    def _stop_watcher(self, key):
        w = self._state["watchers"].pop(key, None)
        if w:
            w.stop()
        self._state["file_statuses"].pop(key, None)
        self._update_icon_state()

    def _stop_all_watchers(self):
        for key in list(self._state["watchers"].keys()):
            w = self._state["watchers"].pop(key)
            w.stop()
        self._state["file_statuses"].clear()

        for _ in range(self._state.get("_tone_worker_count", 0)):
            self._state["work_queue_tone"].put(None)
        for _ in range(self._state.get("_ux_worker_count", 0)):
            self._state["work_queue_ux"].put(None)
        self._state["workers"].clear()
        self._update_icon_state()

    def _update_icon_state(self):
        active = any(
            s.get("status") in (STATUS_PROCESSING, STATUS_DETECTED)
            for s in self._state.get("file_statuses", {}).values()
        )
        if not active:
            active = len(self._state.get("watchers", {})) > 0
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            b"setIconActive:", active, False)

    def _schedule_refresh(self):
        pass

    def _refresh_popover(self):
        pass

    # ── Icon ───────────────────────────────────────────────────

    @objc.typedSelector(b"v@:@")
    def setIconActive_(self, active):
        self._set_icon(bool(active))

    def _set_icon(self, active):
        btn = self.statusItem.button()
        # Official menu-bar icon: the SF Symbol speech bubble (filled when active).
        # A template image tints itself for light/dark automatically.
        name = "bubble.left.fill" if active else "bubble.left"
        base = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, "FigWatch")
        if base:
            base.setTemplate_(True)

        if not base:
            btn.setTitle_("\u25C9" if active else "\u25CB")
            return

        for subview in list(btn.subviews()):
            if getattr(subview, '_isFigWatchDot', False):
                subview.removeFromSuperview()

        if not active:
            btn.setImage_(base)
            return

        btn.setImage_(base)
        dotSize = 6
        btnBounds = btn.bounds()
        dotX = btnBounds.size.width - dotSize - 1
        dotY = btnBounds.size.height - dotSize - 1
        dotView = NSView.alloc().initWithFrame_(NSMakeRect(dotX, dotY, dotSize, dotSize))
        dotView.setWantsLayer_(True)
        dotView.layer().setBackgroundColor_(NSColor.systemGreenColor().CGColor())
        dotView.layer().setCornerRadius_(dotSize / 2)
        dotView._isFigWatchDot = True
        btn.addSubview_(dotView)

    # ── Popover ────────────────────────────────────────────────

    def _build_current_view(self):
        cached = self._state.get("deps")
        if cached and cached["all_required"] and not self._state.get("force_onboarding"):
            return build_popover_view(self)
        deps = check_deps()
        self._state["deps"] = deps
        if deps["all_required"] and not self._state.get("force_onboarding"):
            return build_popover_view(self)
        else:
            return build_onboarding_view(self, deps)

    @objc.typedSelector(b"v@:@")
    def toggle_(self, sender):
        try:
            if self._state.get("popover_open"):
                self._close_popover()
                return
            self._state["popover_open"] = True
            self._rebuild_popover()
            btn_screen = sender.window().convertRectToScreen_(sender.frame())
            panel_size = self._panel.frame().size
            x = btn_screen.origin.x + btn_screen.size.width - panel_size.width
            y = btn_screen.origin.y - panel_size.height - 4
            self._panel.setFrameOrigin_(NSMakePoint(x, y))
            self._panel.orderFrontRegardless()
            self._remove_event_monitor()
            self._state["event_monitor"] = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                NSLeftMouseDownMask | NSRightMouseDownMask,
                lambda event: self._close_popover()
            )
            self._start_refresh_timer()
        except Exception:
            pass

    @objc.typedSelector(b"v@:@")
    def rebuildPopoverFromThread_(self, _sender):
        if self._state.get("popover_open"):
            self._rebuild_popover()

    def _rebuild_popover(self):
        view, h = self._build_current_view()
        content = self._panel.contentView()
        view.setFrame_(NSMakeRect(0, 0, W, h))
        view.setDrawsBackground_(False) if hasattr(view, 'setDrawsBackground_') else None
        view.setWantsLayer_(True)
        view.layer().setBackgroundColor_(None)
        if self._glass_is_container:
            content.setContentView_(view)  # NSGlassEffectView hosts a single content view
        else:
            for sub in list(content.subviews()):
                sub.removeFromSuperview()
            content.addSubview_(view)
        old_frame = self._panel.frame()
        new_y = old_frame.origin.y + old_frame.size.height - h
        self._panel.setFrame_display_(NSMakeRect(old_frame.origin.x, new_y, W, h), True)

    def _data_snapshot(self):
        statuses = tuple(
            (k, s.get("status"), s.get("trigger"), s.get("user"))
            for k, s in sorted(self._state.get("file_statuses", {}).items())
        )
        watched_keys = tuple(f["key"] for f in self._state.get("watched", []))
        update = self._state.get("update_available")
        update_tag = update.get("tag") if update else None
        return (watched_keys, statuses, update_tag)

    def _update_countdown_label(self):
        label = self._state.get("_countdown_label")
        if not label:
            return
        file_statuses = self._state.get("file_statuses", {})
        last_polls = [s.get("last_poll") for s in file_statuses.values() if s.get("last_poll")]
        if last_polls:
            remaining = max(0, int(30 - (time.time() - max(last_polls))))
            text = f"Checking for comments in {remaining}s" if remaining > 0 else "Checking now\u2026"
        elif self._state.get("watched"):
            text = "Starting\u2026"
        else:
            text = ""
        with contextlib.suppress(Exception):
            label.setStringValue_(text)

    def _start_refresh_timer(self):
        self._state["_last_data_snap"] = self._data_snapshot()
        def _loop():
            while self._state.get("popover_open"):
                time.sleep(1)
                if not self._state.get("popover_open"):
                    break
                try:
                    snap = self._data_snapshot()
                    if snap != self._state.get("_last_data_snap"):
                        self._state["_last_data_snap"] = snap
                        self._rebuild_popover()
                    else:
                        self._update_countdown_label()
                except Exception:
                    pass
        threading.Thread(target=_loop, daemon=True).start()

    def _stop_refresh_timer(self):
        pass

    def _close_popover(self):
        self._state["popover_open"] = False
        self._stop_refresh_timer()
        with contextlib.suppress(Exception):
            self._panel.orderOut_(None)
        self._remove_event_monitor()

    def _remove_event_monitor(self):
        monitor = self._state.get("event_monitor")
        if monitor:
            with contextlib.suppress(Exception):
                NSEvent.removeMonitor_(monitor)
            self._state["event_monitor"] = None

    # ── File Actions ───────────────────────────────────────────

    @objc.typedSelector(b"v@:@")
    def doAddFileInline_(self, sender):
        root = sender.superview()
        url_field = None
        for subview in root.subviews():
            if isinstance(subview, NSTextField) and subview.tag() == 9000:
                url_field = subview
                break
        if not url_field:
            return
        url_str = url_field.stringValue().strip()
        key = _extract_key(url_str)
        if key:
            self._resolve_and_watch(key)

    @objc.typedSelector(b"v@:@")
    def doAddFile_(self, sender):
        self.popover.close()
        NSApp.activateIgnoringOtherApps_(True)
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Watch a Figma File")
        alert.setInformativeText_("Paste a Figma file URL to start watching.")
        alert.addButtonWithTitle_("Watch"); alert.addButtonWithTitle_("Cancel")
        inp = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 400, 24))
        inp.setPlaceholderString_("https://www.figma.com/design/...")
        alert.setAccessoryView_(inp)
        alert.window().setInitialFirstResponder_(inp)
        if alert.runModal() == NSAlertFirstButtonReturn:
            key = _extract_key(inp.stringValue().strip())
            if key:
                self._resolve_and_watch(key)

    def _resolve_and_watch(self, key):
        def resolve():
            d = _figma_get(f"/files/{key}?depth=1", self._state["pat"])
            name = d.get("name", key) if d else key
            _add_watched(key, name, figjam=False)
            excluded = self._state.get("excluded_keys", [])
            if key in excluded:
                excluded.remove(key)
                self._save_excluded()
            self._sync_watchers()
        threading.Thread(target=resolve, daemon=True).start()

    @objc.typedSelector(b"v@:@")
    def doRemoveFile_(self, sender):
        idx = sender.tag()
        watched = self._state.get("watched", [])
        if idx < len(watched):
            key = watched[idx]["key"]
            self._stop_watcher(key)
            _remove_watched(key)
            excluded = self._state.setdefault("excluded_keys", [])
            if key not in excluded:
                excluded.append(key)
                self._save_excluded()
            self._state["watched"] = [f for f in watched if f["key"] != key]
            self._refresh_popover()

    def _save_excluded(self):
        c = _load_config()
        c["excludedFiles"] = self._state.get("excluded_keys", [])
        _save_config(c)

    @objc.typedSelector(b"v@:@")
    def doShowError_(self, sender):
        idx = sender.tag()
        watched = self._state.get("watched", [])
        if idx < len(watched):
            key = watched[idx]["key"]
            fs = self._state.get("file_statuses", {}).get(key, {})
            error = fs.get("error", "Unknown error")
            self._close_popover()
            NSApp.activateIgnoringOtherApps_(True)
            alert = NSAlert.alloc().init()
            alert.setMessageText_("Watcher Error")
            alert.setInformativeText_(error)
            alert.addButtonWithTitle_("OK")
            alert.runModal()

    # ── Token / Settings ───────────────────────────────────────

    @objc.typedSelector(b"v@:@")
    def doToken_(self, sender):
        self._close_popover()
        NSApp.activateIgnoringOtherApps_(True)
        while True:
            alert = NSAlert.alloc().init()
            alert.setMessageText_("Connect to Figma")
            alert.setInformativeText_("Paste your Personal Access Token.")
            alert.addButtonWithTitle_("Connect")
            alert.addButtonWithTitle_("Cancel")

            # Accessory: the Figma settings path and a docs link as two tight,
            # left-aligned lines (one text view keeps their left edges identical),
            # then the token input below. NSAlert pins the accessory 4px left of
            # its message/informative text, so inset the content by LEFT=4 to line
            # it up with "Connect to Figma" / "Paste\u2026" (verified via render test).
            acc_w = 360
            LEFT = 4
            cw = acc_w - LEFT * 2
            acc = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, acc_w, 80))
            font = NSFont.systemFontOfSize_(NSFont.systemFontSize())

            info = NSTextView.alloc().initWithFrame_(NSMakeRect(LEFT, 0, cw, 40))
            info.setDrawsBackground_(False)
            info.setEditable_(False)
            info.setSelectable_(True)
            info.setTextContainerInset_(NSMakeSize(0, 0))
            info.textContainer().setLineFragmentPadding_(0)
            body = NSMutableAttributedString.alloc().initWithString_attributes_(
                "Figma \u2192 Settings \u2192 Security \u2192 Personal Access Tokens\n",
                {
                    NSFontAttributeName: font,
                    NSForegroundColorAttributeName: NSColor.secondaryLabelColor(),
                })
            body.appendAttributedString_(
                NSAttributedString.alloc().initWithString_attributes_(
                    "see details here \u2197",
                    {
                        NSFontAttributeName: font,
                        NSForegroundColorAttributeName: NSColor.linkColor(),
                        NSLinkAttributeName: NSURL.URLWithString_(
                            "https://developers.figma.com/docs/rest-api/"
                            "personal-access-tokens/"),
                        NSUnderlineStyleAttributeName: 1,
                    }))
            info.textStorage().setAttributedString_(body)
            lm = info.layoutManager(); tc = info.textContainer()
            lm.ensureLayoutForTextContainer_(tc)
            text_h = lm.usedRectForTextContainer_(tc).size.height
            info.setFrame_(NSMakeRect(LEFT, 0, cw, text_h))
            acc.addSubview_(info)

            inp = NSTextField.alloc().initWithFrame_(NSMakeRect(LEFT, text_h + 10, cw, 24))
            inp.setPlaceholderString_("figd_...")
            if self._state["pat"]: inp.setStringValue_(self._state["pat"])
            acc.addSubview_(inp)
            acc.setFrameSize_(NSMakeSize(acc_w, text_h + 34))

            alert.setAccessoryView_(acc); alert.window().setInitialFirstResponder_(inp)
            r = alert.runModal()
            if r == NSAlertFirstButtonReturn:
                tok = inp.stringValue().strip()
                if not tok:
                    continue
                name, error = _validate_token(tok)
                if name:
                    self._state["pat"] = tok; self._state["user"] = name
                    c = _load_config(); c["figmaPat"] = tok; _save_config(c)
                    cached = self._state.get("deps")
                    if cached:
                        cached["pat"] = {"ok": True}
                        cached["all_required"] = _all_required(cached)
                    if not self._state.get("workers"):
                        threading.Thread(target=self._app_init, daemon=True).start()
                    self._rebuild_popover()
                    self._show_toast(f"Connected as {name} ✓")
                    break
                else:
                    err = NSAlert.alloc().init()
                    err.setMessageText_("Connection Failed")
                    err.setInformativeText_(
                        f"Could not validate token with Figma API.\n\n{error}")
                    err.addButtonWithTitle_("Try Again")
                    err.addButtonWithTitle_("Cancel")
                    if err.runModal() != NSAlertFirstButtonReturn:
                        break
            else:
                break

    @objc.typedSelector(b"v@:@")
    def doSettings_(self, sender):
        self._close_popover()
        acc, height = self._build_settings_content()
        self._present_settings(acc, height)

    def _build_settings_content(self):
        """Build the Settings content view (glass-panel body). Returns (view, height)."""
        SW = 420
        acc = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, SW, 900))
        y = 0
        config = _load_config()

        def _pill(title, action, width=SW, height=28):
            btn = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
            btn.setBordered_(False)
            # Explicit labelColor title — a borderless layer-backed button's
            # default title color does NOT adapt to dark mode (renders dark-on-
            # dark). labelColor resolves per-appearance at draw time.
            btn.setAttributedTitle_(
                NSAttributedString.alloc().initWithString_attributes_(
                    title,
                    {
                        NSForegroundColorAttributeName: NSColor.labelColor(),
                        NSFontAttributeName: NSFont.systemFontOfSize_weight_(
                            12, NSFontWeightMedium),
                    }))
            btn.setWantsLayer_(True)
            # Raised fill + hairline border so the chip reads as a tappable button
            # in both light and dark (labelColor@6% was ~invisible on white).
            btn.layer().setBackgroundColor_(
                NSColor.labelColor().colorWithAlphaComponent_(0.10).CGColor())
            btn.layer().setCornerRadius_(height // 2)
            btn.layer().setBorderWidth_(1)
            btn.layer().setBorderColor_(NSColor.separatorColor().CGColor())
            btn.setTarget_(self); btn.setAction_(action)
            return btn

        def _sep():
            nonlocal y
            y += 12
            s = NSBox.alloc().initWithFrame_(NSMakeRect(0, y, SW, 1))
            s.setBoxType_(2)
            acc.addSubview_(s)
            y += 12

        def _section(title, icon_name, trailing_btn=None):
            nonlocal y
            if y > 0:
                _sep()
            sh = 24
            h = NSView.alloc().initWithFrame_(NSMakeRect(0, y, SW, sh))
            icon = _sf_symbol(icon_name, size=12, color=NSColor.secondaryLabelColor())
            if icon:
                icon.setFrameOrigin_((0, (sh - icon.frame().size.height) / 2))
                h.addSubview_(icon)
            lbl = _label(title, size=12, weight=NSFontWeightSemibold)
            lbl.sizeToFit()
            lbl.setFrameOrigin_((20, (sh - lbl.frame().size.height) / 2))
            h.addSubview_(lbl)
            if trailing_btn:
                tf = trailing_btn.frame()
                trailing_btn.setFrameOrigin_((SW - tf.size.width, (sh - tf.size.height) / 2))
                h.addSubview_(trailing_btn)
            acc.addSubview_(h)
            y += sh + 4

        def _row(label_text, control):
            nonlocal y
            lbl = _label(label_text, size=13)
            lbl.setFrameOrigin_((0, y + 3))
            acc.addSubview_(lbl)
            cf = control.frame()
            control.setFrameOrigin_((SW - cf.size.width, y))
            acc.addSubview_(control)
            y += 28

        # ── Header (own panel supplies its own title; NSAlert used to) ──
        hicon = _sf_symbol("tv", size=15, color=NSColor.secondaryLabelColor())
        if hicon:
            hicon.setFrameOrigin_((0, y))
            acc.addSubview_(hicon)
        htitle = _label("FigWatch Settings", size=15, weight=NSFontWeightBold)
        htitle.sizeToFit()
        htitle.setFrameOrigin_((24, y + 1))
        acc.addSubview_(htitle)
        y += 26

        # ── Triggers ──────────────────────────────────────────
        _section("Triggers", "bolt.fill")

        trigger_config = self._state.get("trigger_config", [])
        intro_results = self._state.get("introspection_results", {})

        for i, tc in enumerate(trigger_config):
            trigger_word = tc.get("trigger", "")
            skill_name = tc.get("skill", "")
            builtin = skill_name.startswith("builtin:")
            if builtin:
                display = f"{skill_name.replace('builtin:', '')} (built-in)"
            else:
                # A custom skill path is <dir>/<name>/skill.md — basename() would
                # just say "skill.md", so show the skill's folder name instead.
                display = _skill_display_name(skill_name)

            rh = 28
            row = NSView.alloc().initWithFrame_(NSMakeRect(0, y, SW, rh))

            intro = intro_results.get(skill_name)
            is_ok = builtin or (intro and intro.get("comment_compatible"))
            ck = _sf_symbol("checkmark.circle.fill" if is_ok else "exclamationmark.circle.fill",
                            size=12, color=NSColor.secondaryLabelColor())
            if ck:
                ck.setFrameOrigin_((0, (rh - ck.frame().size.height) / 2))
                row.addSubview_(ck)

            tw = _label(trigger_word, size=13, weight=NSFontWeightMedium, mono=True)
            tw.sizeToFit()
            # Clamp so a long keyword truncates instead of overrunning the skill
            # name at x=100 (leaves a 4pt gap: 20 + 76 = 96).
            tw.setFrameSize_(NSMakeSize(min(tw.frame().size.width, 76), tw.frame().size.height))
            tw.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
            tw.setFrameOrigin_((20, (rh - tw.frame().size.height) / 2))
            row.addSubview_(tw)

            sn = _label(display, size=11, color=NSColor.secondaryLabelColor())
            sn.sizeToFit()
            sn.setFrameOrigin_((100, (rh - sn.frame().size.height) / 2))
            sn.setFrameSize_(NSMakeSize(SW - 128, sn.frame().size.height))
            sn.cell().setLineBreakMode_(5)
            row.addSubview_(sn)

            if not builtin:
                rm = NSButton.alloc().initWithFrame_(NSMakeRect(SW - 20, (rh - 18) // 2, 18, 18))
                rm.setBordered_(False); rm.setTitle_("")
                rm_icon = _sf_symbol("xmark.circle", size=12, color=NSColor.tertiaryLabelColor())
                if rm_icon: rm.setImage_(rm_icon.image())
                rm.setTag_(i)
                rm.setTarget_(self); rm.setAction_(b"doRemoveTrigger:")
                row.addSubview_(rm)

            acc.addSubview_(row)
            y += 28

        y += 12
        add_lbl = _label("Add your own skill", size=11, color=NSColor.secondaryLabelColor())
        add_lbl.setFrameOrigin_((0, y))
        acc.addSubview_(add_lbl)
        y += 20

        kw_field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, y, 80, 24))
        kw_field.setPlaceholderString_("@keyword")
        kw_field.setFont_(NSFont.monospacedSystemFontOfSize_weight_(12, NSFontWeightRegular))
        acc.addSubview_(kw_field)

        from figwatch.skills import find_skills as _find_skills
        available = _find_skills()
        sk_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(88, y, SW - 88, 24), False)
        sk_popup.setFont_(NSFont.systemFontOfSize_(11))
        skill_paths = []
        for s in available:
            label = s["name"] + (" (built-in)" if s["builtin"] else "")
            sk_popup.addItemWithTitle_(label)
            skill_paths.append(s["path"])
        sk_popup.addItemWithTitle_("Browse for file\u2026")
        sk_popup.setTarget_(self)
        sk_popup.setAction_(b"doSkillPopupChanged:")
        acc.addSubview_(sk_popup)
        y += 32

        add_btn = _pill("Add", b"doAddTriggerInline:", width=SW, height=24)
        add_btn.setFont_(NSFont.systemFontOfSize_weight_(11, NSFontWeightMedium))
        add_btn.setFrameOrigin_((0, y))
        acc.addSubview_(add_btn)
        y += 32

        self._add_trigger_kw = kw_field
        self._add_trigger_sk = sk_popup
        self._add_trigger_paths = skill_paths
        self._add_trigger_btn = add_btn

        # ── Connection ────────────────────────────────────────
        tok_hdr_btn = _pill("Change Token\u2026", b"doToken:", width=120, height=24)
        tok_hdr_btn.setFont_(NSFont.systemFontOfSize_weight_(11, NSFontWeightMedium))
        _section("Connection", "link", trailing_btn=tok_hdr_btn)

        user = self._state.get("user")
        if user and self._state.get("pat"):
            conn_icon = _sf_symbol("checkmark.circle.fill", size=12, color=NSColor.secondaryLabelColor())
            conn_text = f"Connected as {user}"
        else:
            conn_icon = _sf_symbol("xmark.circle", size=12, color=NSColor.secondaryLabelColor())
            conn_text = "Not connected"

        crh = 20
        conn_row = NSView.alloc().initWithFrame_(NSMakeRect(0, y, SW, crh))
        if conn_icon:
            conn_icon.setFrameOrigin_((0, (crh - conn_icon.frame().size.height) / 2))
            conn_row.addSubview_(conn_icon)
        conn_lbl = _label(conn_text, size=13)
        conn_lbl.sizeToFit()
        conn_lbl.setFrameSize_(NSMakeSize(min(conn_lbl.frame().size.width, SW - 20), conn_lbl.frame().size.height))
        conn_lbl.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
        conn_lbl.setFrameOrigin_((20, (crh - conn_lbl.frame().size.height) / 2))
        conn_row.addSubview_(conn_lbl)
        acc.addSubview_(conn_row)
        y += 24

        tok_input = NSTextField.alloc().initWithFrame_(NSMakeRect(0, -100, 0, 0))
        if self._state.get("pat"):
            tok_input.setStringValue_(self._state["pat"])
        acc.addSubview_(tok_input)

        # ── AI ────────────────────────────────────────────────
        gw = gateway_info()
        conn_btn = _pill("Switch…" if gw else "Sign in…", b"doSwitchAI:", width=92, height=24)
        conn_btn.setFont_(NSFont.systemFontOfSize_weight_(11, NSFontWeightMedium))
        _section("AI", "cpu", trailing_btn=conn_btn)

        # Claude access status — company gateway (cc-switch) vs personal login.
        # Mirror the Connection row: the icon must reflect real auth state, not a
        # hard-coded checkmark. Authed = a gateway is configured OR personal login
        # succeeded (cached deps from the last check).
        ai_ok = bool(gw) or bool(self._state.get("deps", {}) and
                                  self._state.get("deps", {}).get("claude_auth", {}).get("ok"))
        claude_text = f"Gateway · {gw['host']}" if gw else "Personal Claude login"
        crh = 20
        cr = NSView.alloc().initWithFrame_(NSMakeRect(0, y, SW, crh))
        cic = _sf_symbol("checkmark.circle.fill" if ai_ok else "xmark.circle",
                         size=12, color=NSColor.secondaryLabelColor())
        if cic:
            cic.setFrameOrigin_((0, (crh - cic.frame().size.height) / 2))
            cr.addSubview_(cic)
        cl = _label(claude_text, size=13)
        cl.sizeToFit()
        cl.setFrameSize_(NSMakeSize(min(cl.frame().size.width, SW - 20), cl.frame().size.height))
        cl.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
        cl.setFrameOrigin_((20, (crh - cl.frame().size.height) / 2))
        cr.addSubview_(cl)
        acc.addSubview_(cr)
        y += 24

        if gw:
            # The model is dictated by the gateway (cc-switch); show it read-only
            # so the picker can't imply an override that the gateway would reject.
            model_popup = None
            mval = _label((gw.get("model") or "gateway default") + "  (from gateway)",
                          size=12, color=NSColor.secondaryLabelColor())
            mval.sizeToFit()
            # Cap so a long gateway model id truncates instead of running off the
            # left edge (the row right-aligns the value at SW - width).
            mval.setFrameSize_(NSMakeSize(min(mval.frame().size.width, SW - 64), mval.frame().size.height))
            mval.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
            _row("Model", mval)
        else:
            model_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 180, 24), False)
            model_popup.addItemWithTitle_("Sonnet (recommended)")
            model_popup.addItemWithTitle_("Opus (most capable)")
            model_popup.addItemWithTitle_("Haiku (cheapest)")
            model_map = {"sonnet": 0, "opus": 1, "haiku": 2}
            model_popup.selectItemAtIndex_(model_map.get(self._state.get("model", "sonnet"), 0))
            _row("Model", model_popup)

        lang_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 180, 24), False)
        lang_popup.addItemWithTitle_("English")
        lang_popup.addItemWithTitle_("\u4e2d\u6587 (Chinese)")
        lang_map = {"en": 0, "cn": 1}
        lang_popup.selectItemAtIndex_(lang_map.get(self._state.get("reply_lang", "en"), 0))
        _row("Reply language", lang_popup)

        tone_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 80, 24), False)
        for n in range(1, 6): tone_popup.addItemWithTitle_(str(n))
        tone_popup.selectItemAtIndex_(config.get("workersTone", 2) - 1)
        _row("Tone workers", tone_popup)

        ux_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 80, 24), False)
        for n in range(1, 6): ux_popup.addItemWithTitle_(str(n))
        ux_popup.selectItemAtIndex_(config.get("workersUx", 1) - 1)
        _row("UX workers", ux_popup)

        interval_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 120, 24), False)
        for s in INTERVAL_PRESETS: interval_popup.addItemWithTitle_(f"{s} seconds")
        # poll_interval is coerced into INTERVAL_PRESETS at config load, so it is
        # always a valid member here.
        interval_popup.selectItemAtIndex_(
            INTERVAL_PRESETS.index(self._state.get("poll_interval", DEFAULT_INTERVAL)))
        _row("Check interval", interval_popup)

        followups_switch = NSSwitch.alloc().initWithFrame_(NSMakeRect(0, 0, 40, 20))
        followups_switch.setState_(1 if self._state.get("thread_followups", True) else 0)
        _row("Reply follow-ups (@figwatch)", followups_switch)

        # ── About ─────────────────────────────────────────────
        upd_hdr_btn = _pill("Check for Updates", b"doCheckUpdate:", width=140, height=24)
        upd_hdr_btn.setFont_(NSFont.systemFontOfSize_weight_(11, NSFontWeightMedium))
        _section("About", "info.circle", trailing_btn=upd_hdr_btn)

        ver = _label(f"FigWatch v{VERSION}", size=12, color=NSColor.secondaryLabelColor())
        ver.setFrameOrigin_((0, y))
        acc.addSubview_(ver)
        y += 20

        # Controls the Save handler reads after the panel is shown (they used to
        # be closure locals consumed inline by the NSAlert modal).
        self._settings_ctl = {
            "model": model_popup,      # None in gateway mode (model is fixed)
            "lang": lang_popup,
            "tone": tone_popup,
            "ux": ux_popup,
            "interval": interval_popup,
            "followups": followups_switch,
        }

        # ── Footer: Save / Cancel ─────────────────────────────
        _sep()
        cancel_btn = _pill("Cancel", b"doSettingsCancel:", width=90, height=28)
        cancel_btn.setFrameOrigin_((SW - 90 * 2 - 8, y))
        acc.addSubview_(cancel_btn)
        save_btn = _pill("Save", b"doSettingsSave:", width=90, height=28)
        save_btn.setFrameOrigin_((SW - 90, y))
        acc.addSubview_(save_btn)
        y += 28 + PAD

        acc.setFrameSize_(NSMakeSize(SW, y))
        return acc, y

    def _present_settings(self, acc, height):
        """Show the Settings content in our own liquid-glass panel."""
        pw = acc.frame().size.width + SETTINGS_MARGIN_X * 2
        ph = height + SETTINGS_MARGIN_Y * 2
        panel = getattr(self, "_settings_panel", None)
        if panel is None:
            panel = KeyPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, pw, ph),
                NSWindowStyleMaskBorderless,
                NSBackingStoreBuffered, False)
            panel.setLevel_(NSFloatingWindowLevel)
            panel.setReleasedWhenClosed_(False)
            panel.setHasShadow_(True)
            panel.setOpaque_(False)
            panel.setBackgroundColor_(NSColor.clearColor())
            glass, self._settings_glass_container = _make_glass_view(pw, ph)
            panel.setContentView_(glass)
            self._settings_panel = panel
        self._host_settings_content(acc, height)
        panel.center()
        NSApp.activateIgnoringOtherApps_(True)
        panel.makeKeyAndOrderFront_(None)

    def _host_settings_content(self, acc, height):
        """Install (or swap) the content inside the settings glass + resize.

        The content view is inset by SETTINGS_MARGIN_* inside a wrapper that
        fills the glass, so rows don't run flush to the rounded edge (NSAlert
        used to supply this padding).
        """
        pw = acc.frame().size.width + SETTINGS_MARGIN_X * 2
        ph = height + SETTINGS_MARGIN_Y * 2
        glass = self._settings_panel.contentView()
        wrapper = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, pw, ph))
        acc.setFrameOrigin_((SETTINGS_MARGIN_X, SETTINGS_MARGIN_Y))
        wrapper.addSubview_(acc)
        if self._settings_glass_container:
            glass.setContentView_(wrapper)  # NSGlassEffectView hosts a single view
        else:
            for sub in list(glass.subviews()):
                sub.removeFromSuperview()
            glass.setFrameSize_(NSMakeSize(pw, ph))
            glass.addSubview_(wrapper)
        f = self._settings_panel.frame()
        # Keep the top-left corner fixed as height changes (rows added/removed).
        new_y = f.origin.y + f.size.height - ph
        self._settings_panel.setFrame_display_(
            NSMakeRect(f.origin.x, new_y, pw, ph), True)

    def _refresh_settings(self):
        """Rebuild the settings body in place (trigger added/removed)."""
        if getattr(self, "_settings_panel", None) is None:
            return
        acc, height = self._build_settings_content()
        self._host_settings_content(acc, height)

    @objc.typedSelector(b"v@:@")
    def doSettingsCancel_(self, sender):
        if getattr(self, "_settings_panel", None) is not None:
            self._settings_panel.orderOut_(None)

    @objc.typedSelector(b"v@:@")
    def doSettingsSave_(self, sender):
        ctl = getattr(self, "_settings_ctl", None)
        if not ctl:
            return
        rmap = {0: "sonnet", 1: "opus", 2: "haiku"}
        model_popup = ctl["model"]
        if model_popup is not None:
            new_model = rmap.get(model_popup.indexOfSelectedItem(), "sonnet")
        else:
            # Gateway mode: model is fixed by cc-switch, leave it unchanged.
            new_model = self._state.get("model", "sonnet")
        lrmap = {0: "en", 1: "cn"}
        new_lang = lrmap.get(ctl["lang"].indexOfSelectedItem(), "en")
        new_tone_workers = ctl["tone"].indexOfSelectedItem() + 1
        new_ux_workers = ctl["ux"].indexOfSelectedItem() + 1
        new_interval = INTERVAL_PRESETS[ctl["interval"].indexOfSelectedItem()]
        new_followups = bool(ctl["followups"].state())

        needs_restart = False
        c = _load_config()
        if new_model != self._state.get("model"):
            self._state["model"] = new_model
            c["aiModel"] = new_model
            needs_restart = True
        if new_lang != self._state.get("reply_lang"):
            self._state["reply_lang"] = new_lang
            c["replyLang"] = new_lang
            needs_restart = True
        if new_tone_workers != c.get("workersTone", 2) or new_ux_workers != c.get("workersUx", 1):
            c["workersTone"] = new_tone_workers
            c["workersUx"] = new_ux_workers
            needs_restart = True
        if new_interval != self._state.get("poll_interval"):
            self._state["poll_interval"] = new_interval
            c["watchInterval"] = new_interval
            needs_restart = True
        if new_followups != self._state.get("thread_followups", True):
            self._state["thread_followups"] = new_followups
            c["threadFollowups"] = new_followups
            needs_restart = True  # watchers pick up follow_up_mention on restart
        _save_config(c)

        if needs_restart and self._state.get("watchers"):
            self._stop_all_watchers()
            self._start_workers()
            watched = self._state.get("watched", [])
            n = len(watched)
            for i, f in enumerate(watched):
                delay = (self._state.get("poll_interval", DEFAULT_INTERVAL) / n) * i if n > 1 else 0
                self._start_watcher(f, initial_delay=delay)

        if getattr(self, "_settings_panel", None) is not None:
            self._settings_panel.orderOut_(None)

    # ── Trigger management ─────────────────────────────────────

    @objc.typedSelector(b"v@:@")
    def doSkillPopupChanged_(self, sender):
        # "Browse for file…" is the last item (index == number of real skills).
        # Selecting it opens the macOS file picker immediately; the chosen file is
        # added to the popup and selected, so "Add" then commits it. Activate the
        # app first — a menu-bar (agent) app isn't frontmost, so without this the
        # open panel opens behind everything or not at all.
        if sender.indexOfSelectedItem() != len(self._add_trigger_paths):
            return
        panel = NSOpenPanel.alloc().init()
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(False)
        panel.setAllowedFileTypes_(["md"])
        NSApp.activateIgnoringOtherApps_(True)
        if panel.runModal() == NSModalResponseOK and panel.URLs():
            path = panel.URLs()[0].path()
            idx = len(self._add_trigger_paths)
            sender.insertItemWithTitle_atIndex_(os.path.basename(path), idx)
            self._add_trigger_paths.append(path)
            sender.selectItemAtIndex_(idx)
        else:
            sender.selectItemAtIndex_(0)

    @objc.typedSelector(b"v@:@")
    def doAddTriggerInline_(self, sender):
        keyword = self._add_trigger_kw.stringValue().strip()
        sel = self._add_trigger_sk.indexOfSelectedItem()
        paths = self._add_trigger_paths

        if sel < len(paths):
            skill_path = paths[sel]
        else:
            # Fallback: "Browse for file…" still selected (e.g. picker cancelled).
            NSApp.activateIgnoringOtherApps_(True)
            panel = NSOpenPanel.alloc().init()
            panel.setCanChooseFiles_(True)
            panel.setCanChooseDirectories_(False)
            panel.setAllowedFileTypes_(["md"])
            if panel.runModal() == NSModalResponseOK:
                skill_path = panel.URLs()[0].path()
            else:
                return

        if not keyword:
            self._flash_add_button("Enter a keyword first")
            return
        if not keyword.startswith("@"):
            keyword = "@" + keyword
        if any(t.get("trigger") == keyword for t in self._state.get("trigger_config", [])):
            self._flash_add_button(f"{keyword} already added")
            return

        # Commit, then rebuild the panel body in place so the new trigger appears
        # immediately (with its real skill name) — no reopen, no NSAlert relayout.
        self._commit_trigger(keyword, skill_path)
        self._refresh_settings()

    def _set_add_button_title(self, text):
        btn = getattr(self, "_add_trigger_btn", None)
        if btn is None:
            return
        btn.setAttributedTitle_(
            NSAttributedString.alloc().initWithString_attributes_(
                text, {
                    NSForegroundColorAttributeName: NSColor.labelColor(),
                    NSFontAttributeName: NSFont.systemFontOfSize_weight_(12, NSFontWeightMedium),
                }))

    def _flash_add_button(self, text):
        """Briefly show a status on the Add button, then revert to "Add"."""
        self._set_add_button_title(text)
        threading.Timer(
            1.8,
            lambda: self.performSelectorOnMainThread_withObject_waitUntilDone_(
                b"resetAddButtonTitle:", None, False),
        ).start()

    @objc.typedSelector(b"v@:@")
    def resetAddButtonTitle_(self, _):
        self._set_add_button_title("Add")

    def _commit_trigger(self, keyword, skill_path):
        if not keyword or not skill_path:
            return
        if not keyword.startswith("@"):
            keyword = "@" + keyword
        tc = self._state.get("trigger_config", [])
        tc.append({"trigger": keyword, "skill": skill_path})
        self._state["trigger_config"] = tc
        self._save_trigger_config(tc)
        for w in self._state.get("watchers", {}).values():
            w.reload_trigger_config(tc)
        threading.Thread(
            target=self._introspect_new_trigger, args=(skill_path,), daemon=True
        ).start()

    def _introspect_new_trigger(self, skill_path):
        from figwatch.skills import introspect_skill
        result = introspect_skill(skill_path, _skill_claude_path())
        intro = self._state.setdefault("introspection_results", {})
        intro[skill_path] = result

    @objc.typedSelector(b"v@:@")
    def doRemoveTrigger_(self, sender):
        idx = sender.tag()
        tc = self._state.get("trigger_config", [])
        if idx < len(tc):
            tc.pop(idx)
            self._state["trigger_config"] = tc
            self._save_trigger_config(tc)
            for w in self._state.get("watchers", {}).values():
                w.reload_trigger_config(tc)
            # Rebuild the panel body in place so the row disappears immediately.
            self._refresh_settings()

    def _save_trigger_config(self, trigger_config):
        c = _load_config()
        c["triggers"] = trigger_config
        _save_config(c)

    # ── Update progress HUD ────────────────────────────────────
    # System notifications (NSUserNotification) are dead on modern macOS, so the
    # self-update flow needs visible in-app feedback instead of a silent
    # download + quit + relaunch. The same glass panel doubles as a general
    # confirmation toast (_show_toast) for other success paths.

    def _show_toast(self, message):
        """Brief glass confirmation panel, dismissed with Done. Used where a
        success would otherwise be silent (NSUserNotification does nothing on
        modern macOS) and there's no persistent control to flash inline."""
        self._show_update_hud(message, spinning=False,
                              button="Done", button_action=b"hudDismiss:")

    def _show_update_hud(self, message, *, spinning=True, button=None, button_action=None):
        """Show (or update) the glass update HUD with a status line. Main thread.

        button/button_action drive the action button: "Cancel" (b"doCancelUpdate:")
        while downloading so a half-done download can be aborted, "Done"
        (b"hudDismiss:") on the completion toast so the window can be
        dismissed. Pass button=None to hide it (e.g. mid-install, when there's
        nothing safe to cancel).
        """
        if getattr(self, "_hud_panel", None) is None:
            self._build_hud_panel()
        self._hud_label.setStringValue_(message)
        bar = self._hud_bar
        if spinning:
            bar.setHidden_(False)
            bar.setIndeterminate_(True)
            bar.startAnimation_(None)
        else:
            bar.stopAnimation_(None)
            bar.setHidden_(True)
        btn = self._hud_button
        if button:
            btn.setTitle_(button)
            btn.setAction_(button_action or b"hudDismiss:")
            btn.setTarget_(self)
            btn.setHidden_(False)
        else:
            btn.setHidden_(True)
        self._hud_panel.orderFrontRegardless()
        NSApp.activateIgnoringOtherApps_(True)

    def _build_hud_panel(self):
        """Build the liquid-glass update HUD once — matches the popover material."""
        w, h = 300, 158
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, w, h),
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered, False)
        panel.setLevel_(NSFloatingWindowLevel)
        panel.setReleasedWhenClosed_(False)
        panel.setHasShadow_(True)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())

        glass, is_container = _make_glass_view(w, h)
        inner = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))

        title = _label("FigWatch", size=12, weight=NSFontWeightMedium,
                       color=NSColor.secondaryLabelColor())
        title.setAlignment_(NSTextAlignmentCenter)
        title.setFrame_(NSMakeRect(0, 20, w, 16))
        inner.addSubview_(title)

        bar = NSProgressIndicator.alloc().initWithFrame_(
            NSMakeRect((w - 210) / 2, 58, 210, 8))
        bar.setStyle_(0)  # NSProgressIndicatorStyleBar
        bar.setIndeterminate_(True)
        bar.setMinValue_(0.0); bar.setMaxValue_(1.0)
        inner.addSubview_(bar)

        label = _label("", size=13)
        label.setAlignment_(NSTextAlignmentCenter)
        label.setFrame_(NSMakeRect(16, 82, w - 32, 20))
        inner.addSubview_(label)

        btn = NSButton.buttonWithTitle_target_action_("", self, b"hudDismiss:")
        btn.setFrame_(NSMakeRect((w - 96) / 2, 116, 96, 28))
        btn.setHidden_(True)
        inner.addSubview_(btn)

        if is_container:
            glass.setContentView_(inner)  # NSGlassEffectView hosts a single view
        else:
            glass.addSubview_(inner)
        panel.setContentView_(glass)
        panel.center()

        self._hud_panel = panel
        self._hud_bar = bar
        self._hud_label = label
        self._hud_button = btn

    @objc.typedSelector(b"v@:@")
    def hudHideButton_(self, _):
        """Main-thread selector: hide the action button (download done → can't cancel)."""
        if getattr(self, "_hud_button", None) is not None:
            self._hud_button.setHidden_(True)

    @objc.typedSelector(b"v@:@")
    def hudSetText_(self, message):
        """Main-thread selector: update the HUD status line from a bg thread."""
        if getattr(self, "_hud_label", None) is not None:
            self._hud_label.setStringValue_(str(message))
            self._hud_panel.orderFrontRegardless()

    @objc.typedSelector(b"v@:@")
    def hudSetProgress_(self, frac_str):
        """Main-thread selector: switch the bar to determinate + set 0..1 fraction.

        Passed as a string — strings bridge cleanly through
        performSelectorOnMainThread where a bare NSNumber does not float()
        reliably across PyObjC versions.
        """
        bar = getattr(self, "_hud_bar", None)
        if bar is None:
            return
        try:
            frac = max(0.0, min(1.0, float(frac_str)))
        except (TypeError, ValueError):
            return
        if bar.isIndeterminate():
            bar.stopAnimation_(None)
            bar.setIndeterminate_(False)
        bar.setDoubleValue_(frac)

    @objc.typedSelector(b"v@:@")
    def hudDismiss_(self, _):
        """Main-thread selector: stop the bar and close the HUD."""
        bar = getattr(self, "_hud_bar", None)
        if bar is not None:
            bar.stopAnimation_(None)
        if getattr(self, "_hud_panel", None) is not None:
            self._hud_panel.orderOut_(None)

    # ── Update check ───────────────────────────────────────────

    @objc.typedSelector(b"v@:@")
    def doUpdateBanner_(self, sender):
        # The banner already knows the newer release (from _check_for_update),
        # so skip the re-fetch doCheckUpdate_ does and open the install dialog.
        self._close_popover()
        latest = self._state.get("update_available")
        if not latest:
            return
        self._update_result = latest
        self.showUpdateResult_(None)

    def doCheckUpdate_(self, sender):
        try:
            NSApp.abortModal()
            NSApp.keyWindow().orderOut_(None)
        except Exception:
            pass
        threading.Thread(target=self._run_update_check, daemon=True).start()

    def _run_update_check(self):
        self._update_result = _fetch_latest_release()
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            b"showUpdateResult:", None, False)

    @objc.typedSelector(b"v@:@")
    def showUpdateResult_(self, _):
        latest = getattr(self, "_update_result", None)
        NSApp.activateIgnoringOtherApps_(True)
        alert = NSAlert.alloc().init()
        if not latest:
            alert.setMessageText_("Couldn\u2019t check for updates")
            alert.setInformativeText_("Check your internet connection and try again.")
            alert.addButtonWithTitle_("OK")
            alert.runModal()
            return

        cur = _parse_version(VERSION)
        new = _parse_version(latest.get("tag", ""))
        release_ts = latest.get("published_at", "")
        # Only a strictly higher version is an update. A matching version is up
        # to date even if the release timestamp differs (e.g. after a manual or
        # install.sh install that never recorded a timestamp) \u2014 otherwise the
        # dialog offers to "update" to the version you're already on.
        if new <= cur:
            alert.setMessageText_("You\u2019re up to date")
            alert.setInformativeText_(f"FigWatch v{VERSION} is the latest version.")
            alert.addButtonWithTitle_("OK")
            alert.runModal()
            return

        # The GitHub release body is Markdown (headers, **bold**, manual-install
        # steps that don't apply in-app), so don't dump it raw \u2014 show a clean
        # line and point at "View on GitHub" for the full notes.
        alert.setMessageText_(f"Update available: {latest.get('tag', '')}")
        alert.setInformativeText_(
            f"You\u2019re on v{VERSION}. Install & Restart downloads the update and "
            f"relaunches FigWatch automatically.\n\n"
            f"See View on GitHub for the full release notes.")

        zip_url = latest.get("zip_url")
        if zip_url:
            alert.addButtonWithTitle_("Install & Restart")
            alert.addButtonWithTitle_("View on GitHub")
            alert.addButtonWithTitle_("Later")
            resp = alert.runModal()
            if resp == NSAlertFirstButtonReturn:
                self._show_update_hud("Downloading update\u2026",
                                      button="Cancel", button_action=b"doCancelUpdate:")
                threading.Thread(
                    target=self._install_update,
                    args=(zip_url, release_ts, latest.get("tag", "")), daemon=True
                ).start()
            elif resp == NSAlertSecondButtonReturn:
                NSWorkspace.sharedWorkspace().openURL_(
                    NSURL.URLWithString_(latest.get("url", RELEASES_URL)))
        else:
            alert.addButtonWithTitle_("View on GitHub")
            alert.addButtonWithTitle_("Later")
            if alert.runModal() == NSAlertFirstButtonReturn:
                NSWorkspace.sharedWorkspace().openURL_(
                    NSURL.URLWithString_(latest.get("url", RELEASES_URL)))

    def _install_update(self, zip_url, release_ts="", tag=""):
        import shutil
        cancel = self._update_cancel = threading.Event()
        try:
            cache = os.path.join(HOME, "Library", "Caches", "FigWatch")
            os.makedirs(cache, exist_ok=True)
            zip_path = os.path.join(cache, "update.zip")
            staging = os.path.join(cache, "staging")

            # GitHub asset URLs 302 to a CDN; some corp networks throttle the
            # default urllib UA, so set our own. Stream in chunks so the HUD can
            # show real progress — a 34 MB download over VPN can take minutes,
            # and a blind spinner reads as "hung". A per-read socket timeout
            # means a genuinely stalled connection fails fast (→ error dialog +
            # manual-download fallback) instead of spinning indefinitely.
            req = urllib.request.Request(zip_url, headers={"User-Agent": "FigWatch"})
            with urllib.request.urlopen(req, timeout=60, context=_SSL_CTX) as r, \
                    open(zip_path, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                got = 0
                last_ui = 0.0
                while True:
                    if cancel.is_set():
                        raise _UpdateCancelled()
                    buf = r.read(256 * 1024)
                    if not buf:
                        break
                    f.write(buf)
                    got += len(buf)
                    now = time.time()
                    if total and now - last_ui > 0.15:
                        last_ui = now
                        frac = got / total
                        self.performSelectorOnMainThread_withObject_waitUntilDone_(
                            b"hudSetText:", f"Downloading update… {int(frac * 100)}%", False)
                        self.performSelectorOnMainThread_withObject_waitUntilDone_(
                            b"hudSetProgress:", str(frac), False)
                if total:
                    self.performSelectorOnMainThread_withObject_waitUntilDone_(
                        b"hudSetProgress:", "1.0", False)

            # Past the download the install is a few fast, non-abortable steps,
            # so drop the Cancel button.
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                b"hudHideButton:", None, False)
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                b"hudSetText:", "Installing update…", False)

            shutil.rmtree(staging, ignore_errors=True)
            os.makedirs(staging, exist_ok=True)
            subprocess.run(["/usr/bin/ditto", "-x", "-k", zip_path, staging], check=True)

            new_app = os.path.join(staging, "FigWatch.app")
            if not os.path.isdir(new_app):
                raise RuntimeError("FigWatch.app not found inside downloaded zip")

            subprocess.run(
                ["/usr/bin/xattr", "-dr", "com.apple.quarantine", new_app],
                capture_output=True,
            )

            current_app = NSBundle.mainBundle().bundlePath()
            if not current_app.endswith(".app"):
                raise RuntimeError(f"Running from unexpected location: {current_app}")

            # The script owns the handoff: wait briefly for a graceful exit, then
            # force-kill us by PID (TERM, then KILL) so the bundle can be replaced
            # cleanly, then relaunch with `open -n` — a plain `open` refuses to
            # start a second instance while the old one is still registered as
            # running, which is exactly how a stuck update stranded the old
            # process with a frozen HUD. Killing by our own PID is precise; the
            # pkill is a belt-and-braces catch for any stray instance.
            script_path = os.path.join(cache, "install.sh")
            script = (
                "#!/bin/bash\n"
                f'PID={os.getpid()}\n'
                'for i in $(seq 1 20); do kill -0 "$PID" 2>/dev/null || break; sleep 0.25; done\n'
                'if kill -0 "$PID" 2>/dev/null; then\n'
                '  kill -TERM "$PID" 2>/dev/null\n'
                '  for i in $(seq 1 20); do kill -0 "$PID" 2>/dev/null || break; sleep 0.25; done\n'
                '  kill -KILL "$PID" 2>/dev/null\n'
                'fi\n'
                'pkill -x FigWatch 2>/dev/null\n'
                'sleep 0.5\n'
                f'rm -rf "{current_app}" 2>/dev/null\n'
                f'/usr/bin/ditto "{new_app}" "{current_app}"\n'
                f'/usr/bin/xattr -dr com.apple.quarantine "{current_app}" 2>/dev/null\n'
                f'open -n "{current_app}"\n'
            )
            with open(script_path, "w") as f:
                f.write(script)
            os.chmod(script_path, 0o755)

            subprocess.Popen(
                ["/bin/bash", script_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            c = _load_config()
            if release_ts:
                c["installedReleaseTimestamp"] = release_ts
            # Marker so the relaunched app can confirm the update (the restart is
            # otherwise silent). Cleared on next launch after the confirmation.
            c["pendingUpdate"] = tag or "the latest version"
            _save_config(c)

            # Hold the final state briefly so it's seen before the app vanishes.
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                b"hudSetText:", "Restarting FigWatch…", False)
            time.sleep(0.8)

            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                b"quitForUpdate:", None, False)
        except _UpdateCancelled:
            # User hit Cancel mid-download — bin the partial file and close the
            # HUD quietly (no "update failed" alert; nothing went wrong).
            with contextlib.suppress(Exception):
                os.remove(zip_path)
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                b"hudDismiss:", None, False)
        except Exception as e:
            self._update_error = str(e)
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                b"showInstallError:", None, False)

    @objc.typedSelector(b"v@:@")
    def doCancelUpdate_(self, sender):
        """Cancel an in-progress download and dismiss the HUD (main thread)."""
        ev = getattr(self, "_update_cancel", None)
        if ev is not None:
            ev.set()
        self.hudDismiss_(None)

    @objc.typedSelector(b"v@:@")
    def quitForUpdate_(self, _):
        # Do NOT run _stop_all_watchers here: it joins every watcher thread with
        # a 5s timeout each, so with N watched files it froze the main thread for
        # up to N*5s (frozen spinner, HUD stuck) right at "Restarting…". Watchers
        # are daemon threads and die with the process; the installer force-kills
        # us by PID as a backstop, so just terminate promptly.
        NSApp.terminate_(None)

    @objc.typedSelector(b"v@:@")
    def showInstallError_(self, _):
        self.hudDismiss_(None)
        err = getattr(self, "_update_error", "Unknown error")
        NSApp.activateIgnoringOtherApps_(True)
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Update failed")
        alert.setInformativeText_(
            f"{err}\n\nYou can download the update manually from the release page.")
        alert.addButtonWithTitle_("Open Release Page")
        alert.addButtonWithTitle_("OK")
        if alert.runModal() == NSAlertFirstButtonReturn:
            NSWorkspace.sharedWorkspace().openURL_(
                NSURL.URLWithString_(RELEASES_URL))

    # ── Power / Quit ───────────────────────────────────────────

    @objc.typedSelector(b"v@:@")
    def doPowerMenu_(self, sender):
        self._close_popover()
        NSApp.activateIgnoringOtherApps_(True)
        alert = NSAlert.alloc().init()
        alert.setMessageText_("FigWatch")
        alert.setInformativeText_("What would you like to do?")
        alert.addButtonWithTitle_("Restart")
        alert.addButtonWithTitle_("Quit")
        alert.addButtonWithTitle_("Cancel")
        r = alert.runModal()
        if r == NSAlertFirstButtonReturn:
            self._relaunch()
        elif r == NSAlertSecondButtonReturn:
            self._stop_all_watchers()
            NSApp.terminate_(None)

    def _relaunch(self):
        """Restart the app reliably.

        `open <bundle>` while we're still running only activates the existing
        instance — it won't start a new one — so the old code quit without ever
        relaunching. Spawn a detached helper that waits for THIS process to exit,
        then forces a fresh instance with `open -n`. Watchers are daemon threads
        that die with the process, so skip the blocking graceful stop (it froze
        the main thread up to ~5s per watched file).
        """
        bundle = NSBundle.mainBundle().bundlePath()
        cache = os.path.join(HOME, "Library", "Caches", "FigWatch")
        os.makedirs(cache, exist_ok=True)
        script_path = os.path.join(cache, "relaunch.sh")
        script = (
            "#!/bin/bash\n"
            f'PID={os.getpid()}\n'
            'for i in $(seq 1 40); do kill -0 "$PID" 2>/dev/null || break; sleep 0.25; done\n'
            f'open -n "{bundle}"\n'
        )
        with open(script_path, "w") as f:
            f.write(script)
        os.chmod(script_path, 0o755)
        subprocess.Popen(
            ["/bin/bash", script_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        NSApp.terminate_(None)

    @objc.typedSelector(b"v@:@")
    def doQuit_(self, sender):
        self._close_popover()
        self._stop_all_watchers()
        NSApp.terminate_(None)

    # ── Onboarding Actions ─────────────────────────────────────

    @objc.typedSelector(b"v@:@")
    def doInstallClaude_(self, sender):
        NSWorkspace.sharedWorkspace().openURL_(
            NSURL.URLWithString_("https://docs.anthropic.com/en/docs/claude-code/getting-started"))

    @objc.typedSelector(b"v@:@")
    def doSwitchAI_(self, sender):
        # Close the settings panel, then show the backend chooser as its own
        # alert (a simple button list — no drift risk, so it stays an NSAlert).
        if getattr(self, "_settings_panel", None) is not None:
            self._settings_panel.orderOut_(None)
        self._show_ai_switch_chooser()

    def _show_ai_switch_chooser(self):
        """Standalone backend chooser, shown after Settings closes (not nested)."""
        from figwatch.gateway import read_manual_gateway
        gw = gateway_info()
        manual = read_manual_gateway() is not None
        if gw and manual:
            current = "Currently using a manually-entered gateway."
        elif gw:
            current = "Currently using the company gateway (cc-switch)."
        else:
            current = "Currently using personal Claude login."

        alert = NSAlert.alloc().init()
        alert.setMessageText_("Switch AI backend")
        alert.setInformativeText_(f"{current}\n\nChoose how FigWatch connects to Claude.")
        alert.setIcon_(_emoji_icon("🔀"))
        alert.addButtonWithTitle_("Company gateway (cc-switch)")
        alert.addButtonWithTitle_("Enter gateway manually…")
        alert.addButtonWithTitle_("Personal Claude login")
        alert.addButtonWithTitle_("Cancel")
        NSApp.activateIgnoringOtherApps_(True)
        resp = alert.runModal()
        if resp == NSAlertFirstButtonReturn:
            self.doConnectGateway_(None)
        elif resp == NSAlertFirstButtonReturn + 1:
            self.doEnterGateway_(None)
        elif resp == NSAlertFirstButtonReturn + 2:
            self.doClaudeAuth_(None)

    @objc.typedSelector(b"v@:@")
    def doClaudeAuth_(self, sender):
        subprocess.run([
            'osascript', '-e',
            'tell application "Terminal" to do script "claude login"'
        ], capture_output=True)

    @objc.typedSelector(b"v@:@")
    def doConnectGateway_(self, sender):
        # Open cc-switch so the user can activate their company gateway profile;
        # they then tap "Check Again". Falls back to the project page if the app
        # isn't found under a common name.
        launched = False
        for app_name in ("CC Switch", "cc-switch", "CCSwitch"):
            try:
                r = subprocess.run(['open', '-a', app_name], capture_output=True)
                if r.returncode == 0:
                    launched = True
                    break
            except Exception:
                pass
        if not launched:
            NSWorkspace.sharedWorkspace().openURL_(
                NSURL.URLWithString_("https://github.com/farion1231/cc-switch"))

    @objc.typedSelector(b"v@:@")
    def doEnterGateway_(self, sender):
        """Option B fallback: manually enter a gateway URL + token.

        For users without cc-switch. Writes ~/.figwatch/gateway.json (which the
        gateway detection reads as a fallback source), then re-checks deps and
        starts watching. Audits then run via the Anthropic API — no CLI.
        """
        from figwatch.gateway import read_manual_gateway, write_manual_gateway
        self._close_popover()
        NSApp.activateIgnoringOtherApps_(True)

        alert = NSAlert.alloc().init()
        alert.setMessageText_("Connect a Gateway")
        alert.setInformativeText_(
            "Enter your company gateway URL and token. FigWatch talks to it via "
            "the Anthropic API directly — no Claude CLI needed.")
        alert.addButtonWithTitle_("Save")
        alert.addButtonWithTitle_("Cancel")

        acc_w = 360
        LEFT = 4
        cw = acc_w - LEFT * 2
        lbl_h, fh, gap = 16, 24, 12
        existing = read_manual_gateway() or {}
        acc = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, acc_w, 3 * (lbl_h + fh + gap)))
        _fy = 0

        def _field(label_text, placeholder, value):
            nonlocal _fy
            lbl = _label(label_text, size=11, color=NSColor.secondaryLabelColor())
            lbl.setFrameOrigin_((LEFT, _fy)); acc.addSubview_(lbl)
            _fy += lbl_h
            tf = NSTextField.alloc().initWithFrame_(NSMakeRect(LEFT, _fy, cw, fh))
            tf.setPlaceholderString_(placeholder)
            if value:
                tf.setStringValue_(value)
            acc.addSubview_(tf)
            _fy += fh + gap
            return tf

        url_field = _field("Gateway URL", "http://llm-gw.example.com/anthropic",
                           existing.get("base_url", ""))
        tok_field = _field("Token", "auth token (or API key)",
                           existing.get("auth_token") or existing.get("api_key") or "")
        model_field = _field("Model (optional)", "gateway model id",
                             existing.get("model", ""))

        alert.setAccessoryView_(acc)
        alert.window().setInitialFirstResponder_(url_field)
        if alert.runModal() != NSAlertFirstButtonReturn:
            return

        base_url = url_field.stringValue().strip()
        token = tok_field.stringValue().strip()
        model = model_field.stringValue().strip()
        if not base_url or not token:
            err = NSAlert.alloc().init()
            err.setMessageText_("Gateway not saved")
            err.setInformativeText_("A gateway URL and a token are both required.")
            err.addButtonWithTitle_("OK"); err.runModal()
            return
        try:
            write_manual_gateway(base_url, auth_token=token, model=model or None)
        except Exception as e:
            err = NSAlert.alloc().init()
            err.setMessageText_("Couldn't save gateway")
            err.setInformativeText_(str(e))
            err.addButtonWithTitle_("OK"); err.runModal()
            return

        self._state["deps"] = check_deps()
        if self._state["deps"].get("all_required") and not self._state.get("workers"):
            threading.Thread(target=self._app_init, daemon=True).start()
        self._rebuild_popover()
        self._show_toast("Gateway connected ✓")

    @objc.typedSelector(b"v@:@")
    def doCheckDeps_(self, sender):
        self._refresh_popover()

    @objc.typedSelector(b"v@:@")
    def doContinueSetup_(self, sender):
        self._state["force_onboarding"] = False
        self._refresh_popover()

    @objc.typedSelector(b"v@:@")
    def doShowSetup_(self, sender):
        self._state["force_onboarding"] = True
        self._refresh_popover()


# ── Entry ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    d = FigWatch.alloc().init()
    app.setDelegate_(d)
    AppHelper.runEventLoop()
