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
from figwatch.handlers import STATUS_LIVE, STATUS_DETECTED, STATUS_PROCESSING, STATUS_REPLIED, STATUS_ERROR

# ── Config ──────────────────────────────────────────────────────────

VERSION = "1.2.0"
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


W = 320
PAD = 12
ROW_H = 26
MAX_FILE_ROWS = 12  # scroll if more than this


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
        with urllib.request.urlopen(req, timeout=10) as r:
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
        with urllib.request.urlopen(req, timeout=10) as r:
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


def _post_notification(title, message):
    try:
        from Foundation import NSUserNotification, NSUserNotificationCenter
        notif = NSUserNotification.alloc().init()
        notif.setTitle_(title)
        notif.setInformativeText_(message)
        NSUserNotificationCenter.defaultUserNotificationCenter().deliverNotification_(notif)
    except Exception:
        pass


def _validate_token(pat):
    d = _figma_get("/me", pat)
    return d.get("handle") if d else None


def check_deps():
    """Check all dependencies. Returns dict of status per dep."""
    deps = {}

    claude_path = _resolve_claude_path()
    claude_ok = claude_path != "claude" and os.path.exists(claude_path)
    deps["claude"] = {"ok": claude_ok, "path": claude_path if claude_ok else None}

    if claude_ok:
        try:
            result = subprocess.run(
                [claude_path, 'auth', 'status', '--json'],
                capture_output=True, timeout=5,
                env=handlers.subprocess_env()
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
            deps["claude_auth"] = {"ok": logged_in}
        except Exception:
            deps["claude_auth"] = {"ok": False}
    else:
        deps["claude_auth"] = {"ok": False}

    config = _load_config()
    deps["pat"] = {"ok": bool(config.get("figmaPat"))}

    deps["all_required"] = deps["claude"]["ok"] and deps["claude_auth"]["ok"] and deps["pat"]["ok"]
    return deps


# ── Key Panel (borderless but accepts key/mouse) ──────────────────

class KeyPanel(NSPanel):
    """Borderless panel that can become key window (for button clicks)."""
    def canBecomeKeyWindow(self):
        return True


# ── Flipped View (Y=0 at top) ──────────────────────────────────────

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

def _load_menu_icon():
    bundle = NSBundle.mainBundle()
    paths = [
        bundle.pathForResource_ofType_("FigWatch-icon", "pdf"),
        os.path.join(_THIS_DIR, "FigWatch-icon.pdf"),
    ]
    for p in paths:
        if p and os.path.exists(p):
            img = NSImage.alloc().initWithContentsOfFile_(p)
            if img:
                img.setSize_(NSMakeSize(18, 18))
                img.setTemplate_(True)
                return img
    return None


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


def build_onboarding_view(app, deps):
    """Build the onboarding/setup checklist. Returns (view, height)."""
    cw = W - PAD * 2
    y = PAD

    root = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, W, 800))

    title = _label("FigWatch Setup", size=15, weight=NSFontWeightBold)
    title.setFrameOrigin_((PAD + 6, y))
    root.addSubview_(title)
    y += 22

    subtitle = _label("Let\u2019s get everything ready.", size=12, color=NSColor.secondaryLabelColor())
    subtitle.setFrameOrigin_((PAD + 6, y))
    root.addSubview_(subtitle)
    y += 24

    items = [
        {
            "key": "claude",
            "name": "Claude Code",
            "desc": "Powers the AI audits",
            "ok": deps["claude"]["ok"],
            "installing": app._state.get("installing_claude"),
            "action": b"doInstallClaude:",
        },
        {
            "key": "claude_auth",
            "name": "Claude Login",
            "desc": "Sign in to your Claude account",
            "ok": deps["claude_auth"]["ok"],
            "installing": False,
            "action": b"doClaudeAuth:",
        },
        {
            "key": "pat",
            "name": "Figma Token",
            "desc": "Connects to your Figma files",
            "ok": deps["pat"]["ok"],
            "installing": False,
            "action": b"doToken:",
        },
    ]

    for item in items:
        row = NSView.alloc().initWithFrame_(NSMakeRect(PAD, y, cw, 44))

        if item["installing"]:
            icon = _label("\u23F3", size=14)
            icon.setFrameOrigin_((6, 12))
        elif item["ok"]:
            icon = _sf_symbol("checkmark.circle.fill", size=14, color=NSColor.systemGreenColor())
            if icon: icon.setFrameOrigin_((4, 10))
        else:
            icon = _sf_symbol("xmark.circle.fill", size=14, color=NSColor.systemRedColor())
            if icon: icon.setFrameOrigin_((4, 10))

        if icon:
            row.addSubview_(icon)

        nl = _label(item["name"], size=13, weight=NSFontWeightMedium)
        nl.setFrameOrigin_((26, 6))
        row.addSubview_(nl)

        dl = _label(item["desc"], size=11, color=NSColor.secondaryLabelColor())
        dl.setFrameOrigin_((26, 24))
        row.addSubview_(dl)

        if not item["ok"] and not item["installing"]:
            btn_title = "Set Up" if item["key"] == "pat" else "Install"
            btn = NSButton.alloc().initWithFrame_(NSMakeRect(cw - 70, 8, 62, 24))
            btn.setTitle_(btn_title)
            btn.setBezelStyle_(NSBezelStyleRecessed)
            btn.setControlSize_(1)
            btn.setFont_(NSFont.systemFontOfSize_weight_(11, NSFontWeightMedium))
            btn.setTarget_(app)
            btn.setAction_(item["action"])
            row.addSubview_(btn)
        elif item["installing"]:
            il = _label("Installing\u2026", size=11, color=NSColor.secondaryLabelColor())
            il.setFrameOrigin_((cw - 75, 13))
            row.addSubview_(il)

        root.addSubview_(row)
        y += 44

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

    quit_btn = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + cw - 22, y + 1, 20, 20))
    quit_btn.setBordered_(False); quit_btn.setTitle_("")
    qi = _sf_symbol("power", size=12, color=NSColor.tertiaryLabelColor())
    if qi: quit_btn.setImage_(qi.image())
    quit_btn.setTarget_(app); quit_btn.setAction_(b"doPowerMenu:")
    root.addSubview_(quit_btn)
    y += 22

    trigger_config = app._state.get("trigger_config", [])
    triggers_str = " \u00B7 ".join(t.get("trigger", "") for t in trigger_config) if trigger_config else "@tone \u00B7 @ux"
    subline = _label(triggers_str, size=11, color=NSColor.secondaryLabelColor())
    subline.setFrameOrigin_((PAD + 4, y))
    root.addSubview_(subline)
    y += 20

    sep0 = NSBox.alloc().initWithFrame_(NSMakeRect(PAD, y, cw, 1))
    sep0.setBoxType_(2)
    root.addSubview_(sep0)
    y += 10

    # ── File list or empty state ───────────────────────────────
    if not watched:
        no_files = _label("No Figma files detected", size=12, color=NSColor.secondaryLabelColor())
        no_files.setFrameOrigin_((PAD + 4, y))
        root.addSubview_(no_files)
        y += 20

        hint = _label("Open a file in Figma Desktop to start watching.", size=11, color=NSColor.tertiaryLabelColor())
        hint.setFrameOrigin_((PAD + 4, y))
        root.addSubview_(hint)
        y += 22

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
            vc = (ROW_H - 14) // 2

            if status == STATUS_LIVE:
                dot = _label("\u25CF", size=8, color=NSColor.systemGreenColor())
                dot.setFrameOrigin_((8, vc + 2))
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
                badge = _label(badge_text, size=10, color=NSColor.systemOrangeColor())
                badge.sizeToFit()
                bw = min(badge.frame().size.width, 92)
                badge.setFrameSize_(NSMakeSize(bw, 14))
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

    poll_interval = 30
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
    ct.setFrameOrigin_((4, (footer_h - 14) // 2))
    ct.setFrameSize_(NSMakeSize(cw - 30, 14))
    footer.addSubview_(ct)
    app._state["_countdown_label"] = ct

    gear = NSButton.alloc().initWithFrame_(NSMakeRect(cw - 22, (footer_h - 20) // 2, 20, 20))
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
        self._cached_menu_icon = None
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
        }
        config = _load_config()
        self._state["excluded_keys"] = config.get("excludedFiles", [])
        self._state["pat"] = config.get("figmaPat")
        self._state["locale"] = config.get("watchLocale", "uk")
        self._state["model"] = config.get("aiModel", "sonnet")
        self._state["reply_lang"] = config.get("replyLang", "en")

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
        glass = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, W, 400))
        glass.setMaterial_(3)
        glass.setState_(1)
        glass.setBlendingMode_(0)
        glass.setWantsLayer_(True)
        glass.layer().setCornerRadius_(12)
        glass.layer().setMasksToBounds_(True)
        self._panel.setContentView_(glass)
        self._panel.contentView().superview().setWantsLayer_(True)
        self._panel.contentView().superview().layer().setCornerRadius_(12)
        self._panel.contentView().superview().layer().setMasksToBounds_(True)

        self._register_login_item()

        if self._state["pat"]:
            threading.Thread(target=self._app_init, daemon=True).start()

    # ── Logging ─────────────────────────────────────────────────

    def _write_log(self, msg):
        if self._watcher_log_file is None:
            self._watcher_log_file = open("/tmp/fw-watcher.log", "a", encoding="utf-8")
        self._watcher_log_file.write(msg + "\n")
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
        self._state["user"] = _validate_token(self._state["pat"])

        from figwatch.watcher import load_trigger_config
        self._state["trigger_config"] = load_trigger_config()

        if not os.path.exists(WATCHED_PATH) and os.path.exists(RECENTS_PATH):
            recents = _load_recents()
            if recents:
                _save_watched(recents)

        config = _load_config()
        if not config.get("installedReleaseTimestamp"):
            rel = _fetch_latest_release()
            if rel and rel.get("published_at"):
                config["installedReleaseTimestamp"] = rel["published_at"]
                _save_config(config)

        self._start_workers()
        self._sync_watchers()
        threading.Thread(target=self._sync_loop, daemon=True).start()

    def _sync_loop(self):
        while True:
            time.sleep(10)
            try:
                self._sync_watchers()
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
                delay = (30.0 / n) * i if n > 1 else 0
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

    def _worker_loop(self, q):
        from figwatch.watcher import process_work_item
        while True:
            item = q.get()
            if item is None:
                break
            try:
                process_work_item(item)
            except Exception:
                pass

    def _start_watcher(self, f, initial_delay=0):
        from figwatch.watcher import FigmaWatcher
        key = f["key"]

        if key in self._state["watchers"]:
            self._stop_watcher(key)

        self._state["file_statuses"][key] = {
            "status": STATUS_LIVE, "trigger": "", "user": "",
            "last_poll": None, "error": None,
        }

        def on_status(event, item, **kwargs):
            if key not in self._state["file_statuses"]:
                return
            fs = self._state["file_statuses"][key]
            if event == STATUS_PROCESSING:
                fs["status"] = STATUS_PROCESSING
                fs["trigger"] = item.trigger
                fs["user"] = item.user_handle
            elif event == STATUS_REPLIED:
                fs["status"] = STATUS_REPLIED
                def _revert(k=key):
                    if k in self._state.get("file_statuses", {}):
                        self._state["file_statuses"][k]["status"] = STATUS_LIVE
                        self._schedule_refresh()
                threading.Timer(4.0, _revert).start()
            elif event == STATUS_ERROR:
                fs["status"] = STATUS_ERROR
                fs["error"] = kwargs.get("error", "Unknown error")
            self._schedule_refresh()
            self._update_icon_state()

        def dispatch(item):
            if item.trigger == "@tone":
                self._state["work_queue_tone"].put(item)
            else:
                self._state["work_queue_ux"].put(item)

        def on_poll():
            if key in self._state["file_statuses"]:
                self._state["file_statuses"][key]["last_poll"] = time.time()

        w = FigmaWatcher(
            key, self._state["pat"],
            locale=self._state.get("locale", "uk"),
            model=self._state.get("model", "sonnet"),
            reply_lang=self._state.get("reply_lang", "en"),
            claude_path=_resolve_claude_path(),
            log=self._write_log,
            trigger_config=self._state["trigger_config"],
            dispatch=dispatch,
            on_poll=on_poll,
            on_status=on_status,
            initial_delay=initial_delay,
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
        if self._cached_menu_icon is None:
            self._cached_menu_icon = _load_menu_icon()
        base = self._cached_menu_icon
        if not base:
            name = "bubble.left.fill" if active else "bubble.left"
            base = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, "FigWatch")
            if base: base.setTemplate_(True)

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

    def _rebuild_popover(self):
        view, h = self._build_current_view()
        content = self._panel.contentView()
        for sub in list(content.subviews()):
            sub.removeFromSuperview()
        view.setFrame_(NSMakeRect(0, 0, W, h))
        view.setDrawsBackground_(False) if hasattr(view, 'setDrawsBackground_') else None
        view.setWantsLayer_(True)
        view.layer().setBackgroundColor_(None)
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
        return (watched_keys, statuses)

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
        try:
            label.setStringValue_(text)
        except Exception:
            pass

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
        try:
            self._panel.orderOut_(None)
        except Exception:
            pass
        self._remove_event_monitor()

    def _remove_event_monitor(self):
        monitor = self._state.get("event_monitor")
        if monitor:
            try:
                NSEvent.removeMonitor_(monitor)
            except Exception:
                pass
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
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Connect to Figma")
        alert.setInformativeText_(
            "Paste your Personal Access Token.\n\n"
            "Figma \u2192 Settings \u2192 Security \u2192 Personal Access Tokens")
        alert.addButtonWithTitle_("Connect")
        alert.addButtonWithTitle_("Get Token \u2197")
        alert.addButtonWithTitle_("Cancel")
        inp = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 340, 24))
        inp.setPlaceholderString_("figd_...")
        if self._state["pat"]: inp.setStringValue_(self._state["pat"])
        alert.setAccessoryView_(inp); alert.window().setInitialFirstResponder_(inp)
        r = alert.runModal()
        if r == NSAlertFirstButtonReturn:
            tok = inp.stringValue().strip()
            if tok:
                def validate():
                    name = _validate_token(tok)
                    if name:
                        self._state["pat"] = tok; self._state["user"] = name
                        c = _load_config(); c["figmaPat"] = tok; _save_config(c)
                        _post_notification("FigWatch", f"Connected as {name}")
                    else:
                        _post_notification("FigWatch", "Invalid token \u2014 please check and try again.")
                threading.Thread(target=validate, daemon=True).start()
        elif r == NSAlertSecondButtonReturn:
            NSWorkspace.sharedWorkspace().openURL_(
                NSURL.URLWithString_("https://www.figma.com/developers/api#access-tokens"))

    @objc.typedSelector(b"v@:@")
    def doSettings_(self, sender):
        self._close_popover()
        NSApp.activateIgnoringOtherApps_(True)

        SW = 420
        acc = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, SW, 800))
        y = 0
        config = _load_config()

        def _pill(title, action, width=SW, height=28):
            btn = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
            btn.setTitle_(title)
            btn.setBordered_(False)
            btn.setWantsLayer_(True)
            btn.layer().setBackgroundColor_(
                NSColor.labelColor().colorWithAlphaComponent_(0.06).CGColor())
            btn.layer().setCornerRadius_(height // 2)
            btn.setFont_(NSFont.systemFontOfSize_weight_(12, NSFontWeightMedium))
            btn.setTarget_(self); btn.setAction_(action)
            return btn

        def _sep():
            nonlocal y
            y += 10
            s = NSBox.alloc().initWithFrame_(NSMakeRect(0, y, SW, 1))
            s.setBoxType_(2)
            acc.addSubview_(s)
            y += 14

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
            y += 30

        # ── Triggers ──────────────────────────────────────────
        _section("Triggers", "bolt.fill")

        trigger_config = self._state.get("trigger_config", [])
        intro_results = self._state.get("introspection_results", {})

        for i, tc in enumerate(trigger_config):
            trigger_word = tc.get("trigger", "")
            skill_name = tc.get("skill", "")
            builtin = skill_name.startswith("builtin:")
            display = f"{skill_name.replace('builtin:', '')} (built-in)" if builtin else os.path.basename(skill_name)

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
            tw.setFrameOrigin_((20, (rh - tw.frame().size.height) / 2))
            row.addSubview_(tw)

            sn = _label(display, size=11, color=NSColor.secondaryLabelColor())
            sn.sizeToFit()
            sn.setFrameOrigin_((100, (rh - sn.frame().size.height) / 2))
            sn.setFrameSize_(NSMakeSize(SW - 130, sn.frame().size.height))
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

        y += 4
        add_lbl = _label("Add new trigger", size=11, color=NSColor.secondaryLabelColor())
        add_lbl.setFrameOrigin_((0, y))
        acc.addSubview_(add_lbl)
        y += 16

        kw_field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, y, 80, 24))
        kw_field.setPlaceholderString_("@keyword")
        kw_field.setFont_(NSFont.monospacedSystemFontOfSize_weight_(12, NSFontWeightRegular))
        acc.addSubview_(kw_field)

        from figwatch.handlers.generic import _find_skills
        available = _find_skills()
        sk_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(88, y, SW - 88, 24), False)
        sk_popup.setFont_(NSFont.systemFontOfSize_(11))
        skill_paths = []
        for s in available:
            label = s["name"] + (" (built-in)" if s["builtin"] else "")
            sk_popup.addItemWithTitle_(label)
            skill_paths.append(s["path"])
        sk_popup.addItemWithTitle_("Browse for file\u2026")
        acc.addSubview_(sk_popup)
        y += 28

        add_btn = NSButton.alloc().initWithFrame_(NSMakeRect(0, y, SW, 24))
        add_btn.setTitle_("Add Trigger")
        add_btn.setBezelStyle_(NSBezelStyleRecessed)
        add_btn.setControlSize_(1)
        add_btn.setFont_(NSFont.systemFontOfSize_weight_(11, NSFontWeightMedium))
        add_btn.setTarget_(self); add_btn.setAction_(b"doAddTriggerInline:")
        acc.addSubview_(add_btn)
        y += 28

        self._add_trigger_kw = kw_field
        self._add_trigger_sk = sk_popup
        self._add_trigger_paths = skill_paths

        # ── Connection ────────────────────────────────────────
        tok_hdr_btn = _pill("Change Token\u2026", b"doToken:", width=120, height=22)
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
        conn_lbl.setFrameOrigin_((20, (crh - conn_lbl.frame().size.height) / 2))
        conn_row.addSubview_(conn_lbl)
        acc.addSubview_(conn_row)
        y += 22

        tok_input = NSTextField.alloc().initWithFrame_(NSMakeRect(0, -100, 0, 0))
        if self._state.get("pat"):
            tok_input.setStringValue_(self._state["pat"])
        acc.addSubview_(tok_input)

        # ── AI ────────────────────────────────────────────────
        _section("AI", "cpu")

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

        # ── About ─────────────────────────────────────────────
        upd_hdr_btn = _pill("Check for Updates", b"doCheckUpdate:", width=140, height=22)
        upd_hdr_btn.setFont_(NSFont.systemFontOfSize_weight_(11, NSFontWeightMedium))
        _section("About", "info.circle", trailing_btn=upd_hdr_btn)

        ver = _label(f"FigWatch v{VERSION}", size=12, color=NSColor.secondaryLabelColor())
        ver.setFrameOrigin_((0, y))
        acc.addSubview_(ver)
        y += 20

        acc.setFrameSize_(NSMakeSize(SW, y))

        alert = NSAlert.alloc().init()
        alert.setMessageText_("FigWatch Settings")
        alert.setInformativeText_("")
        alert.addButtonWithTitle_("Save")
        alert.addButtonWithTitle_("Cancel")
        alert.setAccessoryView_(acc)

        alert.layout()
        buttons = alert.buttons()
        if len(buttons) >= 2:
            win_w = alert.window().frame().size.width
            save_b = buttons[0]
            sf = save_b.frame()
            save_b.setFrameOrigin_(NSMakePoint(win_w - sf.size.width - 20, sf.origin.y))
            cancel_b = buttons[1]
            cf = cancel_b.frame()
            cancel_b.setFrameOrigin_(NSMakePoint(win_w - sf.size.width - cf.size.width - 28, cf.origin.y))

        if alert.runModal() == NSAlertFirstButtonReturn:
            rmap = {0: "sonnet", 1: "opus", 2: "haiku"}
            new_model = rmap.get(model_popup.indexOfSelectedItem(), "sonnet")
            lrmap = {0: "en", 1: "cn"}
            new_lang = lrmap.get(lang_popup.indexOfSelectedItem(), "en")
            new_tone_workers = tone_popup.indexOfSelectedItem() + 1
            new_ux_workers = ux_popup.indexOfSelectedItem() + 1

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
            _save_config(c)

            if needs_restart and self._state.get("watchers"):
                self._stop_all_watchers()
                self._start_workers()
                watched = self._state.get("watched", [])
                n = len(watched)
                for i, f in enumerate(watched):
                    delay = (30.0 / n) * i if n > 1 else 0
                    self._start_watcher(f, initial_delay=delay)

    # ── Trigger management ─────────────────────────────────────

    @objc.typedSelector(b"v@:@")
    def doAddTriggerInline_(self, sender):
        keyword = self._add_trigger_kw.stringValue().strip()
        sel = self._add_trigger_sk.indexOfSelectedItem()
        paths = self._add_trigger_paths

        if sel < len(paths):
            skill_path = paths[sel]
        else:
            panel = NSOpenPanel.alloc().init()
            panel.setCanChooseFiles_(True)
            panel.setCanChooseDirectories_(False)
            panel.setAllowedFileTypes_(["md"])
            if panel.runModal() == NSModalResponseOK:
                skill_path = panel.URLs()[0].path()
            else:
                return

        if keyword:
            self._commit_trigger(keyword, skill_path)
            self._add_trigger_kw.setStringValue_("")
            _post_notification("FigWatch", f"Trigger {keyword} added")

    @objc.typedSelector(b"v@:@")
    def doAddTrigger_(self, sender):
        try:
            NSApp.abortModal()
            NSApp.keyWindow().orderOut_(None)
        except Exception:
            pass

        from figwatch.handlers.generic import _find_skills
        available = _find_skills()

        NSApp.activateIgnoringOtherApps_(True)
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Add Trigger")
        alert.setInformativeText_("Enter the trigger keyword and select a skill.")
        alert.addButtonWithTitle_("Add")
        alert.addButtonWithTitle_("Cancel")

        acc = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, 340, 120))

        kw_label = _label("Trigger keyword (e.g. @a11y)", size=11)
        kw_label.setFrameOrigin_((0, 0))
        acc.addSubview_(kw_label)
        kw_input = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 18, 340, 24))
        kw_input.setPlaceholderString_("@keyword")
        acc.addSubview_(kw_input)

        sk_label = _label("Skill", size=11)
        sk_label.setFrameOrigin_((0, 50))
        acc.addSubview_(sk_label)

        sk_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 68, 260, 24), False)
        sk_popup.setFont_(NSFont.systemFontOfSize_(11))
        skill_paths = []
        for s in available:
            label = s["name"] + (" (built-in)" if s["builtin"] else "")
            sk_popup.addItemWithTitle_(label)
            skill_paths.append(s["path"])
        sk_popup.addItemWithTitle_("Browse for file\u2026")
        acc.addSubview_(sk_popup)

        sk_path_label = _label("", size=9, color=NSColor.tertiaryLabelColor())
        sk_path_label.setFrameOrigin_((0, 96))
        sk_path_label.setFrameSize_(NSMakeSize(340, 14))
        if skill_paths:
            sk_path_label.setStringValue_(skill_paths[0])
        acc.addSubview_(sk_path_label)

        acc.setFrameSize_(NSMakeSize(340, 114))
        alert.setAccessoryView_(acc)
        alert.window().setInitialFirstResponder_(kw_input)

        if alert.runModal() == NSAlertFirstButtonReturn:
            keyword = kw_input.stringValue().strip()
            sel = sk_popup.indexOfSelectedItem()
            if sel < len(skill_paths):
                self._commit_trigger(keyword, skill_paths[sel])
            else:
                panel = NSOpenPanel.alloc().init()
                panel.setCanChooseFiles_(True)
                panel.setCanChooseDirectories_(False)
                panel.setAllowedFileTypes_(["md"])
                if panel.runModal() == NSModalResponseOK:
                    self._commit_trigger(keyword, panel.URLs()[0].path())

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
        from figwatch.handlers.generic import introspect_skill
        result = introspect_skill(skill_path, _resolve_claude_path())
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
            try:
                NSApp.stopModal()
                self._settings_panel.orderOut_(None)
            except Exception:
                pass

    def _save_trigger_config(self, trigger_config):
        c = _load_config()
        c["triggers"] = trigger_config
        _save_config(c)

    # ── Update check ───────────────────────────────────────────

    @objc.typedSelector(b"v@:@")
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
        installed_ts = _load_config().get("installedReleaseTimestamp", "")
        release_ts = latest.get("published_at", "")
        is_newer = new > cur or (new == cur and release_ts and release_ts != installed_ts)
        if not is_newer:
            alert.setMessageText_("You\u2019re up to date")
            alert.setInformativeText_(f"FigWatch v{VERSION} is the latest version.")
            alert.addButtonWithTitle_("OK")
            alert.runModal()
            return

        body = (latest.get("body") or "").strip()
        if len(body) > 500:
            body = body[:500].rstrip() + "\u2026"
        alert.setMessageText_(f"Update available: {latest.get('tag', '')}")
        alert.setInformativeText_(
            f"You\u2019re on v{VERSION}.\n\n{body}" if body else f"You\u2019re on v{VERSION}.")

        zip_url = latest.get("zip_url")
        if zip_url:
            alert.addButtonWithTitle_("Install & Restart")
            alert.addButtonWithTitle_("View on GitHub")
            alert.addButtonWithTitle_("Later")
            resp = alert.runModal()
            if resp == NSAlertFirstButtonReturn:
                _post_notification("FigWatch", "Downloading update\u2026")
                threading.Thread(
                    target=self._install_update, args=(zip_url, release_ts), daemon=True
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

    def _install_update(self, zip_url, release_ts=""):
        import shutil
        try:
            cache = os.path.join(HOME, "Library", "Caches", "FigWatch")
            os.makedirs(cache, exist_ok=True)
            zip_path = os.path.join(cache, "update.zip")
            staging = os.path.join(cache, "staging")

            with urllib.request.urlopen(zip_url, timeout=120) as r, open(zip_path, "wb") as f:
                shutil.copyfileobj(r, f)

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

            script_path = os.path.join(cache, "install.sh")
            script = (
                "#!/bin/bash\n"
                "for i in $(seq 1 40); do\n"
                "  pgrep -x FigWatch > /dev/null 2>&1 || break\n"
                "  sleep 0.25\n"
                "done\n"
                "sleep 1\n"
                f'rm -rf "{current_app}" 2>/dev/null\n'
                f'/usr/bin/ditto "{new_app}" "{current_app}"\n'
                f'/usr/bin/xattr -dr com.apple.quarantine "{current_app}" 2>/dev/null\n'
                f'open "{current_app}"\n'
            )
            with open(script_path, "w") as f:
                f.write(script)
            os.chmod(script_path, 0o755)

            subprocess.Popen(
                ["/bin/bash", script_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            if release_ts:
                c = _load_config()
                c["installedReleaseTimestamp"] = release_ts
                _save_config(c)

            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                b"_quitForUpdate:", None, False)
        except Exception as e:
            self._update_error = str(e)
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                b"showInstallError:", None, False)

    @objc.typedSelector(b"v@:@")
    def _quitForUpdate_(self, _):
        self._stop_all_watchers()
        NSApp.terminate_(None)

    @objc.typedSelector(b"v@:@")
    def showInstallError_(self, _):
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
            self._stop_all_watchers()
            bundle = NSBundle.mainBundle().bundlePath()
            subprocess.Popen(["open", bundle])
            NSApp.terminate_(None)
        elif r == NSAlertSecondButtonReturn:
            self._stop_all_watchers()
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
    def doClaudeAuth_(self, sender):
        subprocess.run([
            'osascript', '-e',
            'tell application "Terminal" to do script "claude login"'
        ], capture_output=True)

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
