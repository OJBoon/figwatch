#!/usr/bin/env python3
"""FigWatch — macOS menu bar app for watching Figma comments."""

import json, os, re, subprocess, threading, urllib.request
import objc
from AppKit import *
from Foundation import *
from PyObjCTools import AppHelper

# ── Config ──────────────────────────────────────────────────────────

HOME = os.path.expanduser("~")
CONFIG_DIR = os.path.join(HOME, ".figwatch")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
RECENTS_PATH = os.path.join(CONFIG_DIR, "recent-watches.json")

# Resolve paths — bundled .app or dev mode
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_RESOURCES = os.path.join(os.path.dirname(_THIS_DIR), "Resources") if _THIS_DIR.endswith("MacOS") else _THIS_DIR

# figma-ds-cli is optional — used for daemon (screenshot fallback)
FIGMA_CLI_PATH = os.path.join(HOME, "figma-cli", "src", "index.js")

# Resolve claude CLI path
CLAUDE_PATH = next((p for p in ["/opt/homebrew/bin/claude", "/usr/local/bin/claude"]
                    if os.path.exists(p)), "claude")

W = 320
PAD = 12
ROW_H = 30


def _load_config():
    # Try new config path first, fall back to legacy figma-ds-cli config
    for path in [CONFIG_PATH, os.path.join(HOME, ".figma-ds-cli", "config.json")]:
        try:
            with open(path) as f:
                config = json.load(f)
                if config.get("figmaPat"): return config
        except Exception: pass
    return {}

def _save_config(c):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w") as f: json.dump(c, f, indent=2)

def _load_recents():
    try:
        with open(RECENTS_PATH) as f: return json.load(f)
    except Exception: return []

def _save_recents(r):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(RECENTS_PATH, "w") as f: json.dump(r[:10], f, indent=2)

def _add_recent(key, name):
    r = [x for x in _load_recents() if x["key"] != key]
    r.insert(0, {"key": key, "name": name})
    _save_recents(r[:10])

def _extract_key(s):
    m = re.search(r"figma\.com/(?:design|file|board)/([a-zA-Z0-9]+)", s)
    if m: return m.group(1)
    s = s.strip()
    return s if re.match(r"^[a-zA-Z0-9]{10,}$", s) else None

def _figma_get(path, pat):
    try:
        req = urllib.request.Request(f"https://api.figma.com/v1{path}", headers={"X-Figma-Token": pat})
        with urllib.request.urlopen(req, timeout=10) as r: return json.loads(r.read())
    except Exception: return None

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

def _get_open_files():
    try:
        with urllib.request.urlopen(urllib.request.Request("http://localhost:9222/json"), timeout=2) as r:
            pages = json.loads(r.read())
    except Exception: return []
    files = []
    for p in pages:
        url, title = p.get("url", ""), p.get("title", "")
        for s in [" \u2013 Figma", " – Figma", " - Figma", " \u2013 FigJam", " – FigJam", " - FigJam"]:
            title = title.replace(s, "")
        if "figma.com" in url and ("/design/" in url or "/board/" in url):
            key = _extract_key(url)
            is_figjam = "/board/" in url or "FigJam" in p.get("title", "")
            if key: files.append({"key": key, "name": title, "figjam": is_figjam})
    return files


# ── Flipped View (Y=0 at top) ──────────────────────────────────────

class FlippedView(NSView):
    def isFlipped(self): return True


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
        # Flash the highlight then send the action
        self.layer().setBackgroundColor_(
            NSColor.labelColor().colorWithAlphaComponent_(0.15).CGColor())
        objc.super(HoverRow, self).mouseDown_(event)

    def mouseUp_(self, event):
        self.layer().setBackgroundColor_(None)
        # Send the action manually since transparent buttons don't auto-send
        if self.target() and self.action():
            NSApp.sendAction_to_from_(self.action(), self.target(), self)
        objc.super(HoverRow, self).mouseUp_(event)


# ── UI Builder ──────────────────────────────────────────────────────

def _load_menu_icon():
    """Load the custom menu bar icon from bundled PDF or fallback path."""
    # Try bundled resource first (py2app), then dev path
    bundle = NSBundle.mainBundle()
    paths = [
        bundle.pathForResource_ofType_("FigWatch-icon", "pdf"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "FigWatch-icon.pdf"),
        os.path.join(_RESOURCES, "FigWatch-icon.pdf"),
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
    """Create a small image view with an SF Symbol."""
    img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, name)
    if not img: return None
    iv = NSImageView.alloc().initWithFrame_(NSMakeRect(0, 0, size + 4, size + 4))

    # Configure symbol
    cfg = NSImageSymbolConfiguration.configurationWithPointSize_weight_(size, 0.0)
    img = img.imageWithSymbolConfiguration_(cfg)

    if color:
        iv.setContentTintColor_(color)
    iv.setImage_(img)
    iv.setImageScaling_(2)  # NSImageScaleProportionallyUpOrDown
    return iv


def check_deps(open_files=None):
    """Check all dependencies. Returns dict of status per dep."""
    deps = {}

    # Claude Code CLI
    claude_ok = os.path.exists(CLAUDE_PATH) if CLAUDE_PATH != "claude" else False
    deps["claude"] = {"ok": claude_ok, "path": CLAUDE_PATH if claude_ok else None}

    # Claude auth status
    if claude_ok:
        try:
            result = subprocess.run(
                [CLAUDE_PATH, 'auth', 'status'],
                capture_output=True, timeout=5,
                env={**os.environ, "PATH": f"/opt/homebrew/bin:/usr/local/bin:{os.environ.get('PATH', '')}"}
            )
            deps["claude_auth"] = {"ok": result.returncode == 0}
        except Exception:
            deps["claude_auth"] = {"ok": False}
    else:
        deps["claude_auth"] = {"ok": False}

    # Figma PAT
    config = _load_config()
    deps["pat"] = {"ok": bool(config.get("figmaPat"))}

    # Figma Desktop (accept pre-fetched file list to avoid double HTTP request)
    figma_installed = os.path.exists("/Applications/Figma.app")
    figma_running = len(open_files) > 0 if open_files is not None else len(_get_open_files()) > 0
    deps["figma"] = {
        "ok": figma_running,
        "installed": figma_installed,
        "running": figma_running,
    }

    deps["all_required"] = deps["claude"]["ok"] and deps["claude_auth"]["ok"] and deps["pat"]["ok"]
    return deps


def build_onboarding_view(app, deps):
    """Build the onboarding/setup checklist. Returns (view, height)."""
    cw = W - PAD * 2
    y = PAD

    root = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, W, 800))

    # Title
    title = _label("FigWatch Setup", size=15, weight=NSFontWeightBold)
    title.setFrameOrigin_((PAD + 6, y))
    root.addSubview_(title)
    y += 22

    subtitle = _label("Let\u2019s get everything ready.", size=12, color=NSColor.secondaryLabelColor())
    subtitle.setFrameOrigin_((PAD + 6, y))
    root.addSubview_(subtitle)
    y += 24

    # Dependency rows
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
        {
            "key": "figma",
            "name": "Figma Desktop",
            "desc": "Open for file detection + screenshots",
            "ok": deps["figma"]["ok"],
            "installing": False,
            "action": b"doOpenFigma:",
            "recommended": True,
        },
    ]

    for item in items:
        row = NSView.alloc().initWithFrame_(NSMakeRect(PAD, y, cw, 44))

        # Status icon
        if item["installing"]:
            icon = _label("\u23F3", size=14)  # hourglass
            icon.setFrameOrigin_((6, 12))
        elif item["ok"]:
            icon = _sf_symbol("checkmark.circle.fill", size=14, color=NSColor.systemGreenColor())
            if icon: icon.setFrameOrigin_((4, 10))
        elif item.get("recommended"):
            icon = _sf_symbol("exclamationmark.circle.fill", size=14, color=NSColor.systemOrangeColor())
            if icon: icon.setFrameOrigin_((4, 10))
        else:
            icon = _sf_symbol("xmark.circle.fill", size=14, color=NSColor.systemRedColor())
            if icon: icon.setFrameOrigin_((4, 10))

        if icon:
            row.addSubview_(icon)

        # Name
        nl = _label(item["name"], size=13, weight=NSFontWeightMedium)
        nl.setFrameOrigin_((26, 6))
        row.addSubview_(nl)

        # Description
        dl = _label(item["desc"], size=11, color=NSColor.secondaryLabelColor())
        dl.setFrameOrigin_((26, 24))
        row.addSubview_(dl)

        # Action button (if not ok and not installing)
        if not item["ok"] and not item["installing"]:
            btn_title = "Set Up" if item["key"] == "pat" else "Open" if item["key"] == "figma" else "Install"
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

    # Separator
    y += 4
    sep = NSBox.alloc().initWithFrame_(NSMakeRect(PAD + 4, y, cw - 8, 1))
    sep.setBoxType_(2)
    root.addSubview_(sep)
    y += 12

    # Footer buttons
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

    # Only show quit if continue isn't showing
    if not deps["all_required"]:
        footer.addSubview_(quit_btn)

    root.addSubview_(footer)
    y += 28 + PAD

    root.setFrameSize_(NSMakeSize(W, y))
    return root, y


def build_popover_view(app):
    """Build the entire popover content. Returns (view, height)."""
    cw = W - PAD * 2  # content width
    y = PAD  # current Y position (top-down thanks to FlippedView)

    root = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, W, 800))
    accent = NSColor.controlAccentColor()

    # ── Status ──────────────────────────────────────────────────
    if app._is_watching() and app._state["current"]:
        # Green dot + file name
        dot = _label("●", size=10, color=NSColor.systemGreenColor())
        dot.setFrameOrigin_((PAD + 4, y + 3))
        root.addSubview_(dot)

        fn = _label(app._state["current"]["name"], size=13, weight=NSFontWeightSemibold)
        fn.setFrameSize_(NSMakeSize(cw - 80, 17))
        fn.setFrameOrigin_((PAD + 18, y))
        root.addSubview_(fn)

        # Disconnect button — pill shaped
        stop = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 0, 24))
        stop.setTitle_("Disconnect")
        stop.setBordered_(False)
        stop.setWantsLayer_(True)
        stop.layer().setBackgroundColor_(NSColor.labelColor().colorWithAlphaComponent_(0.08).CGColor())
        stop.layer().setCornerRadius_(12)
        stop.setFont_(NSFont.systemFontOfSize_weight_(11, NSFontWeightMedium))
        stop.setTarget_(app); stop.setAction_(b"doStop:")
        stop.sizeToFit()
        sf = stop.frame()
        stop.setFrameSize_(NSMakeSize(sf.size.width + 20, 24))
        stop.setFrameOrigin_((PAD + cw - stop.frame().size.width - 2, y - 2))
        root.addSubview_(stop)
        y += 20

        # Meta line: locale + triggers
        loc = app._state.get("locale", "uk").upper()
        meta = _label(f"{loc} \u00B7 @tone \u00B7 @ux", size=11, color=NSColor.secondaryLabelColor())
        meta.setFrameOrigin_((PAD + 18, y))
        root.addSubview_(meta)
        y += 20
    else:
        hint = _label("Select a file to start watching", size=12, color=NSColor.secondaryLabelColor())
        hint.setFrameOrigin_((PAD + 4, y + 2))
        root.addSubview_(hint)
        y += 24

    # ── Separator ───────────────────────────────────────────────
    y += 2
    sep0 = NSBox.alloc().initWithFrame_(NSMakeRect(PAD, y, cw, 1))
    sep0.setBoxType_(2)
    root.addSubview_(sep0)
    y += 8

    # ── File List ───────────────────────────────────────────────
    files = app._state.get("files", [])
    if files:
        hdr = _label("Open in Figma", size=11, weight=NSFontWeightMedium,
                      color=NSColor.secondaryLabelColor())
        hdr.setFrameOrigin_((PAD + 4, y))
        root.addSubview_(hdr)
        y += 20
    else:
        no_files = _label("No Figma files detected.", size=12, color=NSColor.secondaryLabelColor())
        no_files.setFrameOrigin_((PAD + 4, y + 2))
        root.addSubview_(no_files)
        y += 20
        hint2 = _label("Open a file in Figma Desktop.", size=11, color=NSColor.tertiaryLabelColor())
        hint2.setFrameOrigin_((PAD + 4, y))
        root.addSubview_(hint2)
        y += 20

    if files:
        for i, f in enumerate(files):
            row = HoverRow.alloc().initWithFrame_(NSMakeRect(PAD - 2, y, cw + 4, ROW_H))
            row.setTag_(i)
            row.setTarget_(app); row.setAction_(b"fileClick:")

            icon_name = "doc.plaintext" if f.get("figjam") else "paintbrush.pointed"
            icon = _sf_symbol(icon_name, size=12, color=NSColor.secondaryLabelColor())
            if icon:
                icon.setFrameOrigin_((10, 7))
                row.addSubview_(icon)

            nl = _label(f["name"], size=13)
            nl.setFrameSize_(NSMakeSize(cw - 50, 17))
            nl.setFrameOrigin_((30, 7))
            row.addSubview_(nl)

            if (app._is_watching() and app._state["current"]
                    and f["key"] == app._state["current"]["key"]):
                ck = _sf_symbol("checkmark", size=12, color=accent)
                if ck:
                    ck.setFrameOrigin_((cw - 20, 7))
                    row.addSubview_(ck)

            root.addSubview_(row)
            y += ROW_H

    # ── Recent files ────────────────────────────────────────────
    open_keys = {f["key"] for f in files}
    recents = [r for r in _load_recents() if r["key"] not in open_keys]
    if recents:
        y += 4
        hdr = _label("Recent", size=11, weight=NSFontWeightMedium,
                      color=NSColor.secondaryLabelColor())
        hdr.setFrameOrigin_((PAD + 4, y))
        root.addSubview_(hdr)
        y += 20

        for i, f in enumerate(recents[:5]):
            row = HoverRow.alloc().initWithFrame_(NSMakeRect(PAD - 2, y, cw + 4, ROW_H))
            row.setTag_(100 + i)
            row.setTarget_(app); row.setAction_(b"recentClick:")

            icon = _sf_symbol("clock", size=12, color=NSColor.tertiaryLabelColor())
            if icon:
                icon.setFrameOrigin_((10, 7))
                row.addSubview_(icon)

            nl = _label(f["name"], size=13)
            nl.setFrameSize_(NSMakeSize(cw - 50, 17))
            nl.setFrameOrigin_((30, 7))
            row.addSubview_(nl)

            root.addSubview_(row)
            y += ROW_H


    # ── Separator ───────────────────────────────────────────────
    y += 6
    sep = NSBox.alloc().initWithFrame_(NSMakeRect(PAD, y, cw, 1))
    sep.setBoxType_(2)
    root.addSubview_(sep)
    y += 8

    # ── Footer (single row) ─────────────────────────────────────
    footer = NSView.alloc().initWithFrame_(NSMakeRect(PAD, y, cw, 28))

    # Left: locale popup
    lp = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 2, 72, 22), False)
    lp.setFont_(NSFont.systemFontOfSize_(11)); lp.setBordered_(False)
    lp.setControlSize_(1)
    for l in ["\U0001F1EC\U0001F1E7 UK", "\U0001F1E9\U0001F1EA DE", "\U0001F1EB\U0001F1F7 FR",
              "\U0001F1F3\U0001F1F1 NL", "\U0001F1EA\U0001F1FA BNX"]:
        lp.addItemWithTitle_(l)
    lmap = {"uk": 0, "de": 1, "fr": 2, "nl": 3, "benelux": 4}
    lp.selectItemAtIndex_(lmap.get(app._state.get("locale", "uk"), 0))
    lp.setTarget_(app); lp.setAction_(b"doLocale:")
    footer.addSubview_(lp)

    # Right: Quit then Settings — pill shaped with background
    pill_h = 24
    pill_r = pill_h / 2
    pill_bg = NSColor.labelColor().colorWithAlphaComponent_(0.08)

    qb = NSButton.alloc().initWithFrame_(NSMakeRect(cw - 104, 2, 52, pill_h))
    qb.setBordered_(False); qb.setTitle_("Quit")
    qb.setWantsLayer_(True)
    qb.layer().setBackgroundColor_(pill_bg.CGColor())
    qb.layer().setCornerRadius_(pill_r)
    qb.setFont_(NSFont.systemFontOfSize_weight_(11, NSFontWeightMedium))
    qb.setTarget_(app); qb.setAction_(b"doQuit:")
    footer.addSubview_(qb)

    gear = NSButton.alloc().initWithFrame_(NSMakeRect(cw - 46, 2, pill_h + 14, pill_h))
    gear.setBordered_(False); gear.setTitle_("")
    gear.setWantsLayer_(True)
    gear.layer().setBackgroundColor_(pill_bg.CGColor())
    gear.layer().setCornerRadius_(pill_r)
    gi = _sf_symbol("key.fill", size=12, color=NSColor.secondaryLabelColor())
    if gi: gear.setImage_(gi.image())
    gear.setTarget_(app); gear.setAction_(b"doToken:")
    footer.addSubview_(gear)

    root.addSubview_(footer)
    y += 28 + PAD

    root.setFrameSize_(NSMakeSize(W, y))
    return root, y


# ── App ─────────────────────────────────────────────────────────────

class FigWatch(NSObject):
    statusItem = objc.ivar()
    popover = objc.ivar()
    _state = {}

    def applicationDidFinishLaunching_(self, notif):
        self._state = {
            "pat": None, "user": None, "locale": "uk",
            "files": [], "current": None, "watcher": None,
            "daemon_running": False,
            "installing_claude": False,
            "force_onboarding": False, "deps": None,
        }
        config = _load_config()
        self._state["pat"] = config.get("figmaPat")
        self._state["locale"] = config.get("watchLocale", "uk")

        # Add hidden Edit menu so Cmd+V/C/X/A work in text fields and dialogs
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

        self.popover = NSPopover.alloc().init()
        self.popover.setBehavior_(1)  # transient
        self.popover.setAnimates_(True)

        if self._state["pat"]:
            threading.Thread(target=self._bg_init, daemon=True).start()

    def _bg_init(self):
        self._state["user"] = _validate_token(self._state["pat"])
        self._state["files"] = _get_open_files()
        self._check_daemon()
        # Start CDP refresh timer on main thread
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            b"startCdpTimer:", None, False)

    @objc.typedSelector(b"v@:@")
    def startCdpTimer_(self, _):
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            30, self, b"cdpTick:", None, True)

    @objc.typedSelector(b"v@:@")
    def cdpTick_(self, _):
        threading.Thread(target=self._refresh_cdp, daemon=True).start()

    def _refresh_cdp(self):
        self._state["files"] = _get_open_files()
        self._check_daemon()
        self._check_watcher_health()

    def _check_daemon(self):
        """Check if figma-ds-cli daemon is running (optional, for screenshot fallback)."""
        if not os.path.exists(FIGMA_CLI_PATH):
            self._state["daemon_running"] = False
            return
        node_path = next((p for p in ["/opt/homebrew/bin/node", "/usr/local/bin/node"]
                          if os.path.exists(p)), None)
        if not node_path:
            self._state["daemon_running"] = False
            return
        try:
            result = subprocess.run(
                [node_path, FIGMA_CLI_PATH, "daemon", "status"],
                capture_output=True, timeout=5
            )
            self._state["daemon_running"] = b"running" in result.stdout.lower()
        except Exception:
            self._state["daemon_running"] = False

    def _is_watching(self):
        w = self._state.get("watcher")
        return w is not None and w.is_alive()

    def _check_watcher_health(self):
        """Auto-restart the watcher if it died unexpectedly."""
        current = self._state.get("current")
        if not current:
            return
        w = self._state.get("watcher")
        if w is None:
            return
        if not w.is_alive():
            self._state["watcher"] = None
            self._do_start(current)

    @objc.typedSelector(b"v@:@")
    def setIconActive_(self, active):
        self._set_icon(bool(active))

    def _set_icon(self, active):
        btn = self.statusItem.button()
        base = _load_menu_icon()
        if not base:
            name = "bubble.left.fill" if active else "bubble.left"
            base = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, "FigWatch")
            if base: base.setTemplate_(True)

        if not base:
            btn.setTitle_("◉" if active else "○")
            return

        # Remove old dot if any
        for subview in list(btn.subviews()):
            if getattr(subview, '_isFigWatchDot', False):
                subview.removeFromSuperview()

        if not active:
            btn.setImage_(base)
            return

        # When watching: use template icon + add a green dot as a subview
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

    # ── Popover ─────────────────────────────────────────────────

    def _build_current_view(self):
        """Build the right view — onboarding or main — based on dep status."""
        files = _get_open_files()
        self._state["files"] = files
        deps = check_deps(open_files=files)
        self._state["deps"] = deps
        # Debug
        with open("/tmp/figwatch-debug.log", "w") as f:
            f.write(f"files={len(files)}\ndeps={json.dumps({k: str(v) for k, v in deps.items()})}\n")
            f.write(f"all_required={deps['all_required']}\nforce={self._state.get('force_onboarding')}\n")
        if deps["all_required"] and not self._state.get("force_onboarding"):
            self._check_daemon()
            return build_popover_view(self)
        else:
            return build_onboarding_view(self, deps)

    @objc.typedSelector(b"v@:@")
    def toggle_(self, sender):
        if self.popover.isShown():
            self._close_popover(); return
        view, h = self._build_current_view()
        vc = NSViewController.alloc().init()
        vc.setView_(view)
        self.popover.setContentViewController_(vc)
        self.popover.setContentSize_(NSMakeSize(W, h))
        self.popover.showRelativeToRect_ofView_preferredEdge_(
            sender.bounds(), sender, NSMinYEdge)
        self._state["event_monitor"] = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            NSLeftMouseDownMask | NSRightMouseDownMask,
            lambda event: self._close_popover()
        )

    def _close_popover(self):
        if self.popover.isShown():
            self.popover.close()
        monitor = self._state.get("event_monitor")
        if monitor:
            NSEvent.removeMonitor_(monitor)
            self._state["event_monitor"] = None

    # ── File Actions ────────────────────────────────────────────

    def _refresh_popover(self):
        """Rebuild the popover content after a short delay."""
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.1, self, b"doRefreshPopover:", None, False)

    @objc.typedSelector(b"v@:@")
    def doRefreshPopover_(self, timer):
        if not self.popover.isShown():
            return
        view, h = self._build_current_view()
        vc = NSViewController.alloc().init()
        vc.setView_(view)
        self.popover.setContentViewController_(vc)
        self.popover.setContentSize_(NSMakeSize(W, h))

    @objc.typedSelector(b"v@:@")
    def fileClick_(self, sender):
        idx = sender.tag()
        files = self._state.get("files", [])
        if idx < len(files):
            f = files[idx]
            if self._is_watching() and self._state["current"] and f["key"] == self._state["current"]["key"]:
                self._do_stop()
                self._refresh_popover()
            else:
                # Start in background so UI doesn't freeze during daemon start
                self._state["current"] = f  # show immediately in UI
                self._refresh_popover()
                threading.Thread(target=self._do_start, args=(f,), daemon=True).start()

    @objc.typedSelector(b"v@:@")
    def recentClick_(self, sender):
        idx = sender.tag() - 100
        open_keys = {f["key"] for f in self._state.get("files", [])}
        recents = [r for r in _load_recents() if r["key"] not in open_keys]
        if idx < len(recents):
            f = recents[idx]
            self._state["current"] = f
            self._refresh_popover()
            threading.Thread(target=self._do_start, args=(f,), daemon=True).start()

    @objc.typedSelector(b"v@:@")
    def doStop_(self, sender):
        self._do_stop()
        self._refresh_popover()

    @objc.typedSelector(b"v@:@")
    def doUrl_(self, sender):
        self.popover.close()
        NSApp.activateIgnoringOtherApps_(True)
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Watch from Figma URL")
        alert.setInformativeText_("Paste a Figma file URL to start watching.")
        alert.addButtonWithTitle_("Watch"); alert.addButtonWithTitle_("Cancel")
        inp = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 400, 24))
        inp.setPlaceholderString_("https://www.figma.com/design/...")
        alert.setAccessoryView_(inp)
        alert.window().setInitialFirstResponder_(inp)
        if alert.runModal() == NSAlertFirstButtonReturn:
            key = _extract_key(inp.stringValue().strip())
            if key:
                def resolve():
                    d = _figma_get(f"/files/{key}?depth=1", self._state["pat"])
                    name = d.get("name", key) if d else key
                    _add_recent(key, name)
                    self._do_start({"key": key, "name": name})
                threading.Thread(target=resolve, daemon=True).start()

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
                        self._state["files"] = _get_open_files()
                        _post_notification("FigWatch", f"Connected as {name}")
                    else:
                        _post_notification("FigWatch", "Invalid token \u2014 please check and try again.")
                threading.Thread(target=validate, daemon=True).start()
        elif r == NSAlertSecondButtonReturn:
            NSWorkspace.sharedWorkspace().openURL_(
                NSURL.URLWithString_("https://www.figma.com/developers/api#access-tokens"))

    @objc.typedSelector(b"v@:@")
    def doLocale_(self, sender):
        rmap = {0: "uk", 1: "de", 2: "fr", 3: "nl", 4: "benelux"}
        self._state["locale"] = rmap.get(sender.indexOfSelectedItem(), "uk")
        c = _load_config(); c["watchLocale"] = self._state["locale"]; _save_config(c)
        if self._is_watching() and self._state["current"]:
            self._do_stop(); self._do_start(self._state["current"])

    @objc.typedSelector(b"v@:@")
    def doQuit_(self, sender):
        self.popover.close(); self._do_stop(); NSApp.terminate_(None)

    # ── Onboarding Actions ──────────────────────────────────────

    @objc.typedSelector(b"v@:@")
    def doInstallClaude_(self, sender):
        # Open Claude Code download page
        NSWorkspace.sharedWorkspace().openURL_(
            NSURL.URLWithString_("https://docs.anthropic.com/en/docs/claude-code/getting-started"))

    @objc.typedSelector(b"v@:@")
    def doClaudeAuth_(self, sender):
        # Open Terminal to run claude login
        subprocess.run([
            'osascript', '-e',
            'tell application "Terminal" to do script "claude login"'
        ], capture_output=True)

    @objc.typedSelector(b"v@:@")
    def doOpenFigma_(self, sender):
        if os.path.exists("/Applications/Figma.app"):
            subprocess.run(["open", "-a", "Figma"], capture_output=True)
        else:
            NSWorkspace.sharedWorkspace().openURL_(
                NSURL.URLWithString_("https://www.figma.com/downloads/"))

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

    # ── Watcher ─────────────────────────────────────────────────

    def _do_start(self, fi):
        from watcher import FigmaWatcher
        self._stop_watcher()
        self._state["current"] = fi
        _add_recent(fi["key"], fi["name"])

        def on_reply(trigger, user_handle, node_id):
            _post_notification("FigWatch", f"{trigger} audit posted for {user_handle}")

        w = FigmaWatcher(
            fi["key"], self._state["pat"],
            locale=self._state.get("locale", "uk"),
            claude_path=CLAUDE_PATH,
            log=lambda msg: open("/tmp/fw-watcher.log", "a", encoding="utf-8").write(msg + "\n"),
            on_reply=on_reply,
        )
        w.start()
        self._state["watcher"] = w
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            b"setIconActive:", True, False)

    def _do_stop(self):
        """Full stop — watcher + optional daemon."""
        self._stop_watcher()
        # Optionally stop figma-ds-cli daemon
        node_path = next((p for p in ["/opt/homebrew/bin/node", "/usr/local/bin/node"]
                          if os.path.exists(p)), None)
        if node_path and os.path.exists(FIGMA_CLI_PATH):
            try:
                subprocess.run([node_path, FIGMA_CLI_PATH, "daemon", "stop"],
                               capture_output=True, timeout=5)
            except Exception:
                pass
        self._state["daemon_running"] = False

    def _stop_watcher(self):
        """Stop the watcher thread."""
        w = self._state.get("watcher")
        if w:
            w.stop()
        self._state["watcher"] = None
        self._state["current"] = None
        self._set_icon(False)



# ── Entry ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    d = FigWatch.alloc().init()
    app.setDelegate_(d)
    AppHelper.runEventLoop()
