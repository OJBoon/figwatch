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
import subprocess
import threading
import time
import urllib.request

import objc
from AppKit import *
from Foundation import *
from PyObjCTools import AppHelper

import figwatch.handlers as handlers
from figwatch.config_store import config_dir, load_json, save_json
from figwatch.config_store import load_config as _load_config
from figwatch.config_store import save_config as _save_config
from figwatch.domain import (
    STATUS_LIVE,
    AuditStatus,
)
from figwatch.messages import ack_message, error_kind, error_reply
from figwatch.providers.figma import SSL_CTX, validate_token
from figwatch.trigger_config import skill_label

# String status values for UI state tracking
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
RECENTS_PATH = os.path.join(config_dir(), "recent-watches.json")
WATCHED_PATH = os.path.join(config_dir(), "watched-files.json")
FIGMA_SETTINGS_PATH = os.path.join(HOME, "Library", "Application Support", "Figma", "settings.json")

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
# Type scale: the only text sizes in FigWatch's own panels. 13pt is macOS's
# standard size for row text and regular controls, 11pt its caption size.
TITLE_SIZE = 15    # panel titles, bold
BODY_SIZE = 13     # rows, values, controls, buttons; section headings semibold
CAPTION_SIZE = 11  # secondary description under a row
POPOVER_CORNER_RADIUS = 16  # match macOS Tahoe native menu/popover rounding
# Insets between the settings glass edge and its content (NSAlert used to supply
# these; our own panel must add them or the rows run flush to the glass edge).
SETTINGS_MARGIN_X = 20
SETTINGS_MARGIN_Y = 16

# Auto-watch poll interval choices (seconds), offered in Settings. 15s is the
# floor — shorter risks Figma rate limits when many files are watched.
INTERVAL_PRESETS = [15, 30, 45, 60]
DEFAULT_INTERVAL = 30

# Settings popups: (stored value, title), in menu order.
MODEL_CHOICES = [("sonnet", "Sonnet (recommended)"), ("opus", "Opus (most capable)"),
                 ("haiku", "Haiku (cheapest)")]
LANG_CHOICES = [("en", "English"), ("cn", "\u4e2d\u6587 (Chinese)")]

# Lightweight mention that engages the bot for a conversational follow-up in a
# thread it has already audited (see figwatch/watcher.py detect_triggers).
FOLLOWUP_MENTION = "@figwatch"

# macOS 26 (Tahoe) Liquid Glass container; None on older macOS (we fall back to
# NSVisualEffectView). Looked up dynamically so the app still runs pre-Tahoe.
try:
    _GLASS_CLS = objc.lookUpClass("NSGlassEffectView")
except Exception:
    _GLASS_CLS = None


# ── Watched files persistence ──────────────────────────────────────
# Files the user added by hand ({"key", "name", "figjam"}); migrated from the
# older recent-watches list on first launch.

def _load_watched():
    return load_json(WATCHED_PATH, [])


# ── Figma file detection ──────────────────────────────────────────

_figma_settings_cache = {"mtime": None, "files": []}


def _scan_figma_files():
    """Read Figma Desktop's settings.json to find open files.

    Returns list of {"key": str, "name": str, "figjam": bool}. Called every sync
    tick, so the file is only re-parsed when it has changed.
    """
    try:
        mtime = os.stat(FIGMA_SETTINGS_PATH).st_mtime_ns
    except OSError:
        return []
    if mtime == _figma_settings_cache["mtime"]:
        return list(_figma_settings_cache["files"])
    settings = load_json(FIGMA_SETTINGS_PATH, {})

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
    _figma_settings_cache.update(mtime=mtime, files=files)
    return list(files)


# ── Helpers ────────────────────────────────────────────────────────

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
        with urllib.request.urlopen(req, timeout=10, context=SSL_CTX) as r:
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


def _validate_token(pat):
    """Return (handle, None) on success or (None, error_string) on failure."""
    try:
        return validate_token(pat), None
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


# ── UI Builder ──────────────────────────────────────────────────────

def _pick_md_file():
    """Ask for a skill .md file with the open panel; its path, or None if cancelled.

    Activates the app first — a menu-bar (agent) app isn't frontmost, so the panel
    would otherwise open behind everything or not at all.
    """
    panel = NSOpenPanel.alloc().init()
    panel.setCanChooseFiles_(True)
    panel.setCanChooseDirectories_(False)
    panel.setAllowedFileTypes_(["md"])
    NSApp.activateIgnoringOtherApps_(True)
    if panel.runModal() == NSModalResponseOK and panel.URLs():
        return panel.URLs()[0].path()
    return None


def _label(text, size=BODY_SIZE, weight=NSFontWeightRegular, color=None):
    l = NSTextField.alloc().init()
    l.setStringValue_(text)
    l.setBezeled_(False); l.setEditable_(False)
    l.setSelectable_(False); l.setDrawsBackground_(False)
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


def _sf_symbol(name, size=BODY_SIZE, color=None):
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


MENU_ICON_BOX = 16  # every menu icon shares this canvas so titles line up


def _menu_icon(name, *colors, size=13):
    """SF Symbol for an NSMenuItem image.

    Without colours it stays a template, so the menu tints it (white on the
    accent highlight), as for Settings/Quit in native menus. With palette
    colours (file status: one per symbol layer, e.g. white tick on a green
    disc) it's drawn centred on a fixed canvas and keeps them.
    """
    img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    if not img:
        return None
    cfg = NSImageSymbolConfiguration.configurationWithPointSize_weight_(size, NSFontWeightRegular)
    if not colors:
        img = img.imageWithSymbolConfiguration_(cfg)
        img.setTemplate_(True)
        return img
    # One combined configuration: a second imageWithSymbolConfiguration_
    # call would replace the size rather than add the colours.
    img = img.imageWithSymbolConfiguration_(cfg.configurationByApplyingConfiguration_(
        NSImageSymbolConfiguration.configurationWithPaletteColors_(list(colors))))

    def draw(rect):
        w, h = img.size().width, img.size().height
        img.drawInRect_(NSMakeRect((MENU_ICON_BOX - w) / 2, (MENU_ICON_BOX - h) / 2, w, h))
        return True

    return NSImage.imageWithSize_flipped_drawingHandler_(
        NSMakeSize(MENU_ICON_BOX, MENU_ICON_BOX), False, draw)


def _file_row_look(info):
    """(icon, status text, action) for a watched file's status-menu item. Clicking
    opens the file in Figma; an errored file shows its error instead."""
    status = info.get("status", STATUS_LIVE)
    trigger = info.get("trigger") or ""
    if status == STATUS_PROCESSING:
        user = info.get("user")
        return (_menu_icon("arrow.triangle.2.circlepath", NSColor.systemOrangeColor()),
                f"Running {trigger or 'audit'}{f' for {user}' if user else ''}\u2026",
                b"doOpenFile:")
    if status == STATUS_REPLIED:
        return (_menu_icon("checkmark.circle.fill", NSColor.whiteColor(), NSColor.systemGreenColor()),
                "Replied", b"doOpenFile:")
    if status == STATUS_ERROR:
        return (_menu_icon("exclamationmark.triangle.fill", NSColor.whiteColor(),
                           NSColor.systemRedColor()),
                "Error (click for details)", b"doShowError:")
    return _menu_icon("circle.fill", NSColor.systemGreenColor(), size=8), None, b"doOpenFile:"


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


def build_onboarding_view(app, deps):
    """Build the onboarding/setup checklist. Returns (view, height)."""
    cw = W - PAD * 2
    y = PAD

    root = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, W, 800))

    title = _label("FigWatch Setup", size=TITLE_SIZE, weight=NSFontWeightBold)
    title.setFrameOrigin_((PAD + 6, y))
    root.addSubview_(title)
    y += 24

    subtitle = _label("Let\u2019s get everything ready.", color=NSColor.secondaryLabelColor())
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
            "action": b"doInstallClaude:",
        })
    items.append({
        "key": "claude_auth",
        "name": auth_name,
        "desc": auth_desc,
        "ok": auth["ok"],
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
        "action": b"doToken:",
    })

    for item in items:
        has_actions = bool(item.get("actions")) and not item["ok"]
        # A two-button auth row is taller so the buttons get their own line and
        # never overlap the (long) description text.
        row_h = 68 if has_actions else 44
        row = NSView.alloc().initWithFrame_(NSMakeRect(PAD, y, cw, row_h))

        # Name above its description, as everywhere else. The row view isn't
        # flipped, so the larger y is the upper line.
        name_y, desc_y = (46, 30) if has_actions else (24, 6)

        if item["ok"]:
            icon = _sf_symbol("checkmark.circle.fill", size=BODY_SIZE, color=NSColor.systemGreenColor())
        else:
            icon = _sf_symbol("xmark.circle.fill", size=BODY_SIZE, color=NSColor.systemRedColor())

        if icon:
            # Centred on the two text lines (labels are ~16pt tall).
            icon_mid = (desc_y + name_y + 16) / 2
            icon.setFrameOrigin_((4, icon_mid - icon.frame().size.height / 2))
            row.addSubview_(icon)

        # Available text width: full width when the buttons sit on their own line
        # (has_actions) or the row is complete (ok, no trailing control); otherwise
        # reserve the right side for the Install/Set Up button.
        text_w = (cw - 32) if (has_actions or item["ok"]) else (cw - 108)

        nl = _label(item["name"])
        nl.setFrameSize_(NSMakeSize(text_w, nl.frame().size.height))
        nl.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
        nl.setFrameOrigin_((28, name_y))
        row.addSubview_(nl)

        dl = _label(item["desc"], size=CAPTION_SIZE, color=NSColor.secondaryLabelColor())
        dl.setFrameSize_(NSMakeSize(text_w, dl.frame().size.height))
        dl.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
        dl.setFrameOrigin_((28, desc_y))
        row.addSubview_(dl)

        if has_actions:
            # Buttons on their own line along the bottom, right-aligned.
            bx = cw - 8
            for title, selector in reversed(item["actions"]):
                b = NSButton.buttonWithTitle_target_action_(title, app, selector)
                b.sizeToFit()
                bx -= b.frame().size.width
                b.setFrameOrigin_((bx, 8))
                row.addSubview_(b)
                bx -= 8
        elif not item["ok"]:
            btn_title = "Set Up" if item["key"] == "pat" else "Install"
            btn = NSButton.buttonWithTitle_target_action_(btn_title, app, item["action"])
            btn.sizeToFit()
            bf = btn.frame().size
            btn.setFrameOrigin_((cw - 8 - bf.width, (row_h - bf.height) / 2))
            row.addSubview_(btn)

        root.addSubview_(row)
        y += row_h

    y += 4
    sep = NSBox.alloc().initWithFrame_(NSMakeRect(PAD + 4, y, cw - 8, 1))
    sep.setBoxType_(2)
    root.addSubview_(sep)
    y += 12

    footer = NSView.alloc().initWithFrame_(NSMakeRect(PAD, y, cw, 28))

    def _footer_button(title, action, right):
        b = NSButton.buttonWithTitle_target_action_(title, app, action)
        b.sizeToFit()
        bf = b.frame().size
        b.setFrameOrigin_((cw - bf.width if right else 0, (28 - bf.height) / 2))
        footer.addSubview_(b)
        return b

    _footer_button("Check Again", b"doCheckDeps:", right=False)
    if deps["all_required"]:
        cont_btn = _footer_button("Continue", b"doContinueSetup:", right=True)
        cont_btn.setKeyEquivalent_(chr(13))  # default button, in the accent colour
    else:
        _footer_button("Quit", b"doQuit:", right=True)

    root.addSubview_(footer)
    y += 28 + PAD

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
            # Audit queues by worker pool: @tone gets its own, everything else "ux".
            "work_queues": {"tone": queue.Queue(), "ux": queue.Queue()},
            "worker_counts": {},
            "workers": [],
            "trigger_config": [],
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
        self._set_icon(False)
        # A real NSMenu: the system draws the glass, the accent highlight and
        # keyboard navigation. Items are rebuilt from state each time it opens
        # (menuNeedsUpdate_) and refreshed live while open (menuTick_).
        self._menu = NSMenu.alloc().initWithTitle_("FigWatch")
        self._menu.setAutoenablesItems_(False)
        self._menu.setDelegate_(self)
        self.statusItem.setMenu_(self._menu)

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
            # Deferred until the event loop is running (we're still in init).
            AppHelper.callLater(0.5, self._show_toast, "FigWatch updated",
                                f"You\u2019re now on {pending}.")

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
            recents = load_json(RECENTS_PATH, [])
            if recents:
                save_json(WATCHED_PATH, recents)

        # Start watching first; the update check is a GitHub round trip (up to
        # 10s) and must not delay the first comment poll.
        self._start_workers()
        self._sync_watchers()
        threading.Thread(target=self._check_for_update, daemon=True).start()
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
        if "_manual_watched" not in self._state:
            self._state["_manual_watched"] = _load_watched()
        manual = self._state["_manual_watched"]
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
        for i, key in enumerate(new_keys):
            self._start_watcher(merged[key], initial_delay=self._stagger(i, len(new_keys)))

        # Manual files are always in desired_keys, so anything stale was detected.
        for key in current_keys - desired_keys:
            self._stop_watcher(key)

        self._state["watched"] = list(merged.values())

    def _stagger(self, i, n):
        """Initial delay for the i-th of n watchers, spreading polls over the interval."""
        return self._state.get("poll_interval", DEFAULT_INTERVAL) / n * i if n > 1 else 0

    def _start_workers(self):
        config = _load_config()
        counts = {"tone": config.get("workersTone", 2), "ux": config.get("workersUx", 1)}
        self._state["worker_counts"] = counts
        for pool, n in counts.items():
            for _ in range(n):
                t = threading.Thread(target=self._worker_loop,
                                     args=(self._state["work_queues"][pool],), daemon=True)
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
        self._update_icon_state()
        if revert_after is not None:
            def _revert():
                cur = self._state.get("file_statuses", {}).get(key)
                if cur is not None and cur.get("status") == status:
                    cur["status"] = STATUS_LIVE
                    self._update_icon_state()
            threading.Timer(revert_after, _revert).start()

    def _worker_loop(self, q):
        while True:
            item = q.get()
            if item is None:
                break
            audit, ack_id = item
            svc = self._state.get("audit_service")
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
                response = svc.execute_and_reply(audit, ack_id)
                self._write_log(f"\u2709\ufe0f  Reply posted ({len(response)} chars)")
                self._set_file_status(file_key, STATUS_REPLIED, revert_after=4.0)
            except Exception as e:
                self._write_log(f"\u26a0\ufe0f Audit failed: {e}")
                self._set_file_status(file_key, STATUS_ERROR, error=str(e))
                # Post a short reply instead of going silent: the reviewer gets
                # feedback, and because the trigger now has a reply it won't be
                # re-audited on the next poll (avoids a bad frame clogging the worker).
                svc.reply_with_error(audit, ack_id, error_reply(
                    error_kind(e), trigger_name, self._state.get("reply_lang", "en")))

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
            # Post the ack the instant the poll detects the trigger, on the poll
            # thread -- NOT when a worker dequeues it. Otherwise a busy worker
            # (audits take 30-120s) makes the "working on it" reply wait behind
            # the whole queue. The worker later replaces/deletes it via ack_id.
            svc = self._state.get("audit_service")
            ack_id = None
            if svc:
                try:
                    ack_id = svc.post_ack(
                        audit, ack_message(trigger_kw, self._state.get("reply_lang", "en")),
                    )
                    self._write_log("✉️  Ack posted")
                except Exception as e:
                    self._write_log(f"⚠️ Ack failed: {e}")
            pool = "tone" if trigger_kw == "@tone" else "ux"
            self._state["work_queues"][pool].put((audit, ack_id))

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

        for pool, n in self._state["worker_counts"].items():
            for _ in range(n):
                self._state["work_queues"][pool].put(None)
        self._state["workers"].clear()
        self._update_icon_state()

    def _restart_watchers(self):
        """Stop every watcher and worker and start them again, staggered, so
        they pick up changed settings."""
        self._stop_all_watchers()
        self._start_workers()
        watched = self._state.get("watched", [])
        for i, f in enumerate(watched):
            self._start_watcher(f, initial_delay=self._stagger(i, len(watched)))

    def _update_icon_state(self):
        # Filled while anything is being watched (a file mid-audit always is).
        active = bool(self._state.get("watchers"))
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            b"setIconActive:", active, False)

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

    # ── Status menu ────────────────────────────────────────────

    def _setup_complete(self):
        cached = self._state.get("deps")
        if not (cached and cached["all_required"]):
            self._state["deps"] = check_deps()
        return self._state["deps"]["all_required"]

    def menuNeedsUpdate_(self, menu):
        self._populate_menu(menu)

    def menuWillOpen_(self, menu):
        self._close_popover()
        self._state["menu_open"] = True
        self._state["_last_data_snap"] = self._data_snapshot()
        # Common modes, so it keeps firing while the menu tracks the mouse.
        self._menu_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0, self, b"menuTick:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self._menu_timer, NSRunLoopCommonModes)

    def menuDidClose_(self, menu):
        self._state["menu_open"] = False
        timer = getattr(self, "_menu_timer", None)
        if timer is not None:
            timer.invalidate()
            self._menu_timer = None

    @objc.typedSelector(b"v@:@")
    def menuTick_(self, timer):
        snap = self._data_snapshot()
        if snap != self._state.get("_last_data_snap"):
            self._state["_last_data_snap"] = snap
            self._populate_menu(self._menu)
        else:
            self._update_countdown_label()

    def _populate_menu(self, menu):
        menu.removeAllItems()

        def add(title, action=None, icon=None, key="", tag=0, status=None):
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
            if action:
                item.setTarget_(self)
            else:
                item.setEnabled_(False)
            item.setTag_(tag)
            if icon is not None:
                item.setImage_(icon)
            if status:
                # On the name's line at the same size (not a smaller subtitle),
                # so every line in the menu is one size.
                item.setTitle_(f"{title} \u00B7 {status}")
            menu.addItem_(item)
            return item

        def header(title):
            # A plain disabled item rather than sectionHeaderWithTitle_, whose
            # small bold style is smaller than every other line in the menu.
            add(title)

        if not self._setup_complete():
            header("FigWatch isn\u2019t set up yet")
            add("Finish Setting Up\u2026", b"doOpenSetup:")
            menu.addItem_(NSMenuItem.separatorItem())
            add("Quit FigWatch", b"doQuit:", key="q")
            return

        update = self._state.get("update_available")
        if update:
            tag = update.get("tag", "")
            add(f"Update to {tag}\u2026" if tag else "Update Available\u2026",
                b"doUpdateBanner:", _menu_icon("arrow.down.circle"))
            menu.addItem_(NSMenuItem.separatorItem())

        watched = self._state.get("watched", [])
        statuses = self._state.get("file_statuses", {})
        trigger_config = self._state.get("trigger_config", [])
        triggers = " \u00b7 ".join(t.get("trigger", "") for t in trigger_config) or "@tone \u00b7 @ux"
        if watched:
            n = len(watched)
            header(f"Watching {n} file{'s' if n != 1 else ''} for {triggers}")
            for i, f in enumerate(watched):
                icon, status, action = _file_row_look(statuses.get(f["key"], {}))
                add(f["name"], action, icon, tag=i, status=status)
            menu.addItem_(NSMenuItem.separatorItem())
            self._state["_countdown_item"] = add(self._countdown_text())
        else:
            header("No Figma files open")
            add("Open a file in Figma Desktop to start watching")
            self._state["_countdown_item"] = None

        menu.addItem_(NSMenuItem.separatorItem())
        add("Settings\u2026", b"doSettings:", key=",")
        add("Check for Updates\u2026", b"doCheckUpdate:")
        add("Quit FigWatch", b"doQuit:", key="q")

    def _data_snapshot(self):
        statuses = tuple(
            (k, s.get("status"), s.get("trigger"), s.get("user"))
            for k, s in sorted(self._state.get("file_statuses", {}).items())
        )
        watched_keys = tuple(f["key"] for f in self._state.get("watched", []))
        update = self._state.get("update_available")
        update_tag = update.get("tag") if update else None
        return (watched_keys, statuses, update_tag)

    def _countdown_text(self):
        file_statuses = self._state.get("file_statuses", {})
        last_polls = [s.get("last_poll") for s in file_statuses.values() if s.get("last_poll")]
        if not last_polls:
            return "Starting\u2026"
        interval = self._state.get("poll_interval", DEFAULT_INTERVAL)
        remaining = max(0, int(interval - (time.time() - max(last_polls))))
        return f"Checking for comments in {remaining}s" if remaining > 0 else "Checking now\u2026"

    def _update_countdown_label(self):
        item = self._state.get("_countdown_item")
        if item is not None:
            item.setTitle_(self._countdown_text())

    # ── Setup panel ────────────────────────────────────────────

    @objc.typedSelector(b"v@:@")
    def doOpenSetup_(self, sender):
        """Show the setup checklist in the glass panel under the menu bar icon."""
        try:
            self._state["popover_open"] = True
            self._rebuild_popover()
            if not self._state.get("popover_open"):
                return  # setup turned out to be complete
            btn = self.statusItem.button()
            btn_screen = btn.window().convertRectToScreen_(btn.frame())
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
        except Exception as e:
            self._write_log(f"\u26a0\ufe0f Setup panel failed: {e}")

    @objc.typedSelector(b"v@:@")
    def rebuildPopoverFromThread_(self, _sender):
        self._rebuild_popover()

    def _rebuild_popover(self):
        """Refresh what's on screen: the open status menu, and the setup panel
        (closed instead once setup is complete)."""
        if self._state.get("menu_open"):
            self._populate_menu(self._menu)
        if not self._state.get("popover_open"):
            return
        # One fresh check serves both the "done?" test and the checklist.
        deps = self._state["deps"] = check_deps()
        if deps["all_required"]:
            self._close_popover()
            return
        view, h = build_onboarding_view(self, deps)
        content = self._panel.contentView()
        view.setFrame_(NSMakeRect(0, 0, W, h))
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

    def _close_popover(self):
        self._state["popover_open"] = False
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

    @objc.typedSelector(b"v@:@")
    def doOpenFile_(self, sender):
        idx = sender.tag()
        watched = self._state.get("watched", [])
        if idx < len(watched):
            f = watched[idx]
            self._close_popover()
            ws = NSWorkspace.sharedWorkspace()
            # Figma Desktop handles figma:// links; without it, open the web app.
            if not ws.openURL_(NSURL.URLWithString_(f"figma://file/{f['key']}")):
                kind = "board" if f.get("figjam") else "design"
                ws.openURL_(NSURL.URLWithString_(f"https://www.figma.com/{kind}/{f['key']}"))

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
                    token_changed = tok != self._state["pat"]
                    self._state["pat"] = tok; self._state["user"] = name
                    c = _load_config(); c["figmaPat"] = tok; _save_config(c)
                    cached = self._state.get("deps")
                    if cached:
                        cached["pat"] = {"ok": True}
                        cached["all_required"] = _all_required(cached)
                    if not self._state.get("workers"):
                        threading.Thread(target=self._app_init, daemon=True).start()
                    elif token_changed:
                        # Running watchers and the audit service were built with
                        # the old token; without a restart they keep polling with
                        # it (401s) until the app is relaunched.
                        self._state.pop("audit_service", None)
                        self._restart_watchers()
                    self._rebuild_popover()
                    self._show_toast("Connected to Figma", f"Signed in as {name}.")
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

        def _button(title, action, width=None):
            # Standard push button: regular size, 13pt, drawn by the system so
            # it adapts to dark mode (the Return-key one takes the accent colour).
            btn = NSButton.buttonWithTitle_target_action_(title, self, action)
            btn.sizeToFit()
            if width:
                btn.setFrameSize_(NSMakeSize(width, btn.frame().size.height))
            return btn

        def _popup(width):
            # Natural height, so it centres on a row like the text field next to it.
            p = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, width, 24), False)
            p.setFrameSize_(NSMakeSize(width, p.intrinsicContentSize().height))
            return p

        def _choice_popup(width, choices, current):
            p = _popup(width)
            for _, title in choices:
                p.addItemWithTitle_(title)
            values = [v for v, _ in choices]
            p.selectItemAtIndex_(values.index(current) if current in values else 0)
            return p

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
            icon = _sf_symbol(icon_name, size=BODY_SIZE, color=NSColor.secondaryLabelColor())
            if icon:
                icon.setFrameOrigin_((0, (sh - icon.frame().size.height) / 2))
                h.addSubview_(icon)
            lbl = _label(title, weight=NSFontWeightSemibold)
            lbl.sizeToFit()
            lbl.setFrameOrigin_((20, (sh - lbl.frame().size.height) / 2))
            h.addSubview_(lbl)
            if trailing_btn:
                tf = trailing_btn.frame()
                trailing_btn.setFrameOrigin_((SW - tf.size.width, (sh - tf.size.height) / 2))
                h.addSubview_(trailing_btn)
            acc.addSubview_(h)
            y += sh + 4

        def _status_row(ok, text):
            # Icon + one line of status text, e.g. "Connected as …".
            nonlocal y
            crh = 20
            row = NSView.alloc().initWithFrame_(NSMakeRect(0, y, SW, crh))
            icon = _sf_symbol("checkmark.circle.fill" if ok else "xmark.circle",
                              size=BODY_SIZE, color=NSColor.secondaryLabelColor())
            if icon:
                icon.setFrameOrigin_((0, (crh - icon.frame().size.height) / 2))
                row.addSubview_(icon)
            lbl = _label(text)
            lbl.sizeToFit()
            lbl.setFrameSize_(NSMakeSize(min(lbl.frame().size.width, SW - 20), lbl.frame().size.height))
            lbl.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
            lbl.setFrameOrigin_((20, (crh - lbl.frame().size.height) / 2))
            row.addSubview_(lbl)
            acc.addSubview_(row)
            y += 24

        def _row(label_text, control):
            nonlocal y
            rh = 28  # label and control centred on the same line
            lbl = _label(label_text)
            lbl.setFrameOrigin_((0, y + (rh - lbl.frame().size.height) / 2))
            acc.addSubview_(lbl)
            cf = control.frame()
            control.setFrameOrigin_((SW - cf.size.width, y + (rh - cf.size.height) / 2))
            acc.addSubview_(control)
            y += rh

        # ── Header (own panel supplies its own title; NSAlert used to) ──
        htitle = _label("FigWatch Settings", size=TITLE_SIZE, weight=NSFontWeightBold)
        htitle.sizeToFit()
        htitle.setFrameOrigin_((0, y + 1))
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
            display = skill_label(skill_name) + (" (built-in)" if builtin else "")

            rh = 28
            row = NSView.alloc().initWithFrame_(NSMakeRect(0, y, SW, rh))

            intro = intro_results.get(skill_name)
            is_ok = builtin or (intro and intro.get("comment_compatible"))
            ck = _sf_symbol("checkmark.circle.fill" if is_ok else "exclamationmark.circle.fill",
                            size=BODY_SIZE, color=NSColor.secondaryLabelColor())
            if ck:
                ck.setFrameOrigin_((0, (rh - ck.frame().size.height) / 2))
                row.addSubview_(ck)

            tw = _label(trigger_word)
            tw.sizeToFit()
            # Clamp so a long keyword truncates instead of overrunning the skill
            # name at x=100 (leaves a 4pt gap: 20 + 76 = 96).
            tw.setFrameSize_(NSMakeSize(min(tw.frame().size.width, 76), tw.frame().size.height))
            tw.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
            tw.setFrameOrigin_((20, (rh - tw.frame().size.height) / 2))
            row.addSubview_(tw)

            sn = _label(display, color=NSColor.secondaryLabelColor())
            sn.sizeToFit()
            sn.setFrameOrigin_((100, (rh - sn.frame().size.height) / 2))
            sn.setFrameSize_(NSMakeSize(SW - 128, sn.frame().size.height))
            sn.cell().setLineBreakMode_(5)
            row.addSubview_(sn)

            if not builtin:
                rm = NSButton.alloc().initWithFrame_(NSMakeRect(SW - 20, (rh - 18) // 2, 18, 18))
                rm.setBordered_(False); rm.setTitle_("")
                rm_icon = _sf_symbol("xmark.circle", size=BODY_SIZE, color=NSColor.tertiaryLabelColor())
                if rm_icon: rm.setImage_(rm_icon.image())
                rm.setTag_(i)
                rm.setTarget_(self); rm.setAction_(b"doRemoveTrigger:")
                row.addSubview_(rm)

            acc.addSubview_(row)
            y += 28

        y += 12
        # Doubles as the place Add reports problems ("Enter a keyword first").
        add_lbl = _label("Add your own skill", color=NSColor.secondaryLabelColor())
        add_lbl.setFrameSize_(NSMakeSize(SW, add_lbl.frame().size.height))
        add_lbl.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
        add_lbl.setFrameOrigin_((0, y))
        acc.addSubview_(add_lbl)
        y += 24

        # Keyword, skill and Add share one row, sized and centred like the
        # settings rows below.
        add_btn = _button("Add", b"doAddTriggerInline:")
        kw_field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 100, 22))
        kw_field.setPlaceholderString_("@keyword")
        kw_field.setFrameSize_(NSMakeSize(100, kw_field.intrinsicContentSize().height))

        from figwatch.skills import find_skills as _find_skills
        available = _find_skills()
        sk_x = 108
        sk_popup = _popup(SW - sk_x - add_btn.frame().size.width - 8)
        skill_paths = []
        for s in available:
            label = s["name"] + (" (built-in)" if s["builtin"] else "")
            sk_popup.addItemWithTitle_(label)
            skill_paths.append(s["path"])
        sk_popup.addItemWithTitle_("Browse for file\u2026")
        sk_popup.setTarget_(self)
        sk_popup.setAction_(b"doSkillPopupChanged:")
        rh = 28
        for ctl, x in ((kw_field, 0), (sk_popup, sk_x), (add_btn, SW - add_btn.frame().size.width)):
            ctl.setFrameOrigin_((x, y + (rh - ctl.frame().size.height) / 2))
            acc.addSubview_(ctl)
        y += rh + 4

        self._add_trigger_kw = kw_field
        self._add_trigger_sk = sk_popup
        self._add_trigger_paths = skill_paths
        self._add_trigger_hint = add_lbl

        # ── Connection ────────────────────────────────────────
        tok_hdr_btn = _button("Change Token\u2026", b"doToken:")
        _section("Connection", "link", trailing_btn=tok_hdr_btn)

        user = self._state.get("user")
        connected = bool(user and self._state.get("pat"))
        _status_row(connected, f"Connected as {user}" if connected else "Not connected")

        # ── AI ────────────────────────────────────────────────
        gw = gateway_info()
        # Claude access status — company gateway (cc-switch) vs personal login.
        # Mirror the Connection row: the icon must reflect real auth state, not a
        # hard-coded checkmark. Authed = a gateway is configured OR personal login
        # succeeded (cached deps from the last check).
        ai_ok = self._ai_signed_in(gw)
        # Once signed in by any route the button changes the method, like
        # "Change Token\u2026" above; "Sign in\u2026" only while there is none.
        conn_btn = _button("Change Sign-in\u2026" if ai_ok else "Sign in\u2026", b"doSwitchAI:")
        _section("AI", "cpu", trailing_btn=conn_btn)

        _status_row(ai_ok, f"Gateway · {gw['host']}" if gw
                    else "Personal Claude login" if ai_ok else "Not signed in")

        if gw:
            # The model is dictated by the gateway (cc-switch); show it read-only
            # so the picker can't imply an override that the gateway would reject.
            model_popup = None
            mval = _label((gw.get("model") or "gateway default") + "  (from gateway)",
                          color=NSColor.secondaryLabelColor())
            mval.sizeToFit()
            # Cap so a long gateway model id truncates instead of running off the
            # left edge (the row right-aligns the value at SW - width).
            mval.setFrameSize_(NSMakeSize(min(mval.frame().size.width, SW - 64), mval.frame().size.height))
            mval.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
            _row("Model", mval)
        else:
            model_popup = _choice_popup(180, MODEL_CHOICES, self._state.get("model", "sonnet"))
            _row("Model", model_popup)

        lang_popup = _choice_popup(180, LANG_CHOICES, self._state.get("reply_lang", "en"))
        _row("Reply language", lang_popup)

        tone_popup = _popup(80)
        for n in range(1, 6): tone_popup.addItemWithTitle_(str(n))
        tone_popup.selectItemAtIndex_(config.get("workersTone", 2) - 1)
        _row("Tone workers", tone_popup)

        ux_popup = _popup(80)
        for n in range(1, 6): ux_popup.addItemWithTitle_(str(n))
        ux_popup.selectItemAtIndex_(config.get("workersUx", 1) - 1)
        _row("UX workers", ux_popup)

        interval_popup = _popup(120)
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
        upd_hdr_btn = _button("Check for Updates", b"doCheckUpdate:")
        _section("About", "info.circle", trailing_btn=upd_hdr_btn)

        ver = _label(f"FigWatch v{VERSION}", color=NSColor.secondaryLabelColor())
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
        cancel_btn = _button("Cancel", b"doSettingsCancel:", width=90)
        cancel_btn.setKeyEquivalent_(chr(27))  # Escape
        cancel_btn.setFrameOrigin_((SW - 90 * 2 - 8, y))
        acc.addSubview_(cancel_btn)
        save_btn = _button("Save", b"doSettingsSave:", width=90)
        save_btn.setKeyEquivalent_(chr(13))  # Return: the default button, in the accent colour
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
            # Borderless has no title bar to grab, so drag by the background.
            panel.setMovableByWindowBackground_(True)
            glass, self._settings_glass_container = _make_glass_view(pw, ph)
            glass.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
            # A near-invisible rounded fill behind the glass gives the window a
            # rounded shape. Without it, macOS outlines the focused borderless
            # window as a square around the glass's rounded corners.
            holder = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, pw, ph))
            holder.setWantsLayer_(True)
            holder.layer().setCornerRadius_(POPOVER_CORNER_RADIUS)
            holder.layer().setBackgroundColor_(
                NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.02).CGColor())
            holder.addSubview_(glass)
            panel.setContentView_(holder)
            self._settings_panel = panel
            self._settings_glass = glass
            self._host_settings_content(acc, height)
            panel.center()  # first open only; later opens keep where it was dragged
        else:
            self._host_settings_content(acc, height)
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
        glass = self._settings_glass
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
        self._settings_panel.invalidateShadow()

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
        model_popup = ctl["model"]
        if model_popup is not None:
            new_model = MODEL_CHOICES[model_popup.indexOfSelectedItem()][0]
        else:
            # Gateway mode: model is fixed by cc-switch, leave it unchanged.
            new_model = self._state.get("model", "sonnet")
        new_lang = LANG_CHOICES[ctl["lang"].indexOfSelectedItem()][0]
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
            self._restart_watchers()

        if getattr(self, "_settings_panel", None) is not None:
            self._settings_panel.orderOut_(None)

    # ── Trigger management ─────────────────────────────────────

    @objc.typedSelector(b"v@:@")
    def doSkillPopupChanged_(self, sender):
        # "Browse for file…" is the last item (index == number of real skills).
        # Selecting it opens the macOS file picker immediately; the chosen file is
        # added to the popup and selected, so "Add" then commits it.
        if sender.indexOfSelectedItem() != len(self._add_trigger_paths):
            return
        path = _pick_md_file()
        if path:
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

        # Fallback: "Browse for file…" still selected (e.g. picker cancelled).
        skill_path = paths[sel] if sel < len(paths) else _pick_md_file()
        if not skill_path:
            return

        if not keyword:
            self._flash_add_hint("Enter a keyword first")
            return
        if not keyword.startswith("@"):
            keyword = "@" + keyword
        if any(t.get("trigger") == keyword for t in self._state.get("trigger_config", [])):
            self._flash_add_hint(f"{keyword} already added")
            return

        # Commit, then rebuild the panel body in place so the new trigger appears
        # immediately (with its real skill name) — no reopen, no NSAlert relayout.
        self._commit_trigger(keyword, skill_path)
        self._refresh_settings()

    def _set_add_hint(self, text):
        hint = getattr(self, "_add_trigger_hint", None)
        if hint is not None:
            hint.setStringValue_(text)

    def _flash_add_hint(self, text):
        """Briefly show a status in place of the "Add your own skill" caption."""
        self._set_add_hint(text)
        threading.Timer(
            1.8,
            lambda: self.performSelectorOnMainThread_withObject_waitUntilDone_(
                b"resetAddHint:", None, False),
        ).start()

    @objc.typedSelector(b"v@:@")
    def resetAddHint_(self, _):
        self._set_add_hint("Add your own skill")

    def _commit_trigger(self, keyword, skill_path):
        tc = self._state.get("trigger_config", [])
        tc.append({"trigger": keyword, "skill": skill_path})
        self._apply_triggers(tc)
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
            self._apply_triggers(tc)
            # Rebuild the panel body in place so the row disappears immediately.
            self._refresh_settings()

    def _apply_triggers(self, trigger_config):
        """Save the trigger list and hot-reload it into every running watcher."""
        self._state["trigger_config"] = trigger_config
        c = _load_config()
        c["triggers"] = trigger_config
        _save_config(c)
        for w in self._state.get("watchers", {}).values():
            w.reload_trigger_config(trigger_config)

    # ── Update progress HUD ────────────────────────────────────
    # System notifications (NSUserNotification) are dead on modern macOS, so the
    # self-update flow needs visible in-app feedback instead of a silent
    # download + quit + relaunch. The same glass panel doubles as a general
    # confirmation toast (_show_toast) for other success paths.

    def _show_toast(self, title, info=""):
        """Confirm a success that would otherwise be silent (NSUserNotification
        does nothing on modern macOS) and has no persistent control to flash
        inline. A standard alert, like "You're up to date": app icon, bold
        title, detail line, accent-coloured OK."""
        NSApp.activateIgnoringOtherApps_(True)
        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        if info:
            alert.setInformativeText_(info)
        alert.addButtonWithTitle_("OK")
        alert.runModal()

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

        title = _label("FigWatch", weight=NSFontWeightSemibold,
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

        label = _label("")
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

    @objc.typedSelector(b"v@:@")
    def doCheckUpdate_(self, sender):
        # Reached from the status menu and from Settings; close Settings so the
        # result alert isn't stacked on it.
        self.doSettingsCancel_(None)
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
            with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as r, \
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
    def doQuit_(self, sender):
        self._close_popover()
        self._stop_all_watchers()
        NSApp.terminate_(None)

    # ── Onboarding Actions ─────────────────────────────────────

    @objc.typedSelector(b"v@:@")
    def doInstallClaude_(self, sender):
        NSWorkspace.sharedWorkspace().openURL_(
            NSURL.URLWithString_("https://docs.anthropic.com/en/docs/claude-code/getting-started"))

    def _ai_signed_in(self, gw=None):
        """A gateway is configured, or the last check found a personal login."""
        if gw:
            return True
        return bool((self._state.get("deps") or {}).get("claude_auth", {}).get("ok"))

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
        signed_in = self._ai_signed_in(gw)
        if gw and manual:
            current = "Currently using a manually-entered gateway."
        elif gw:
            current = "Currently using the company gateway (cc-switch)."
        elif signed_in:
            current = "Currently using personal Claude login."
        else:
            current = "FigWatch isn\u2019t signed in to Claude yet."

        alert = NSAlert.alloc().init()
        alert.setMessageText_("Change Claude sign-in" if signed_in else "Sign in to Claude")
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
            'tell application "Terminal" to do script "claude auth login"'
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
        lbl_h, fh, gap = 18, 24, 12
        existing = read_manual_gateway() or {}
        acc = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, acc_w, 3 * (lbl_h + fh + gap)))
        _fy = 0

        def _field(label_text, placeholder, value):
            nonlocal _fy
            lbl = _label(label_text)
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
        self._show_toast("Gateway connected", f"FigWatch will use {base_url}.")

    @objc.typedSelector(b"v@:@")
    def doCheckDeps_(self, sender):
        self._rebuild_popover()

    @objc.typedSelector(b"v@:@")
    def doContinueSetup_(self, sender):
        self._rebuild_popover()


# ── Entry ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    d = FigWatch.alloc().init()
    app.setDelegate_(d)
    AppHelper.runEventLoop()
