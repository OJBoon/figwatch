# FigWatch

AI-powered Figma comment watcher. Comment `@tone` or `@ux` on any frame in Figma and get instant design audits powered by Claude.

FigWatch runs as a lightweight macOS menu bar app. Select a Figma file, and it watches for trigger comments in the background. When someone comments `@tone` or `@ux`, Claude analyses the design and replies directly in the Figma comment thread.

---

## What it does

### @tone — Tone of Voice audit

Comment `@tone` on any text layer or frame to audit the copy against Joybuy's locale-specific Tone of Voice guidelines.

- Checks tone, formality, currency formatting, punctuation, glossary compliance
- Supports UK, DE, FR, NL, and Benelux locales
- Targets the specific text layer you comment on, or audits all visible text in the frame
- Add a locale after the trigger to override: `@tone de`

**Example response:**

```
🗣️ Claude ToV Audit

⚠️ Minor issues

🔤 "Buy now!"
→ "Jetzt kaufen"
(use approved DE CTA, remove exclamation)

🔤 "€2.00"
→ "€2,00"
(comma as decimal separator for DE)

— Claude
```

### @ux — Usability Heuristic Evaluation

Comment `@ux` on any frame to run a full evaluation against Nielsen's 10 Usability Heuristics.

- Analyses both the visual design (screenshot) and the structural data (node tree)
- Evaluates all 10 heuristics with severity ratings
- Considers the screen's position in the user flow
- Explains why each finding matters and what to fix

**Example response:**

```
🗣️ Claude UX Audit — Checkout Step 2

🟠 Major issues

H1 System Status ✅ Step indicator at top clearly shows "Step 2 of 4"
H2 Real World Match ✅ All labels use plain English, CTAs are task-specific

H3 User Control 🟠 No back button — users in step 2 have no way to return
→ Add a back arrow to the header navigation

H4 Consistency ✅ Button hierarchy is clear
H5 Error Prevention 🟠 Payment form has no inline validation
→ Add error + disabled variants to all form inputs

H6 Recognition ✅ Navigation items are labelled
H7 Flexibility ✅ Search bar is prominent
H8 Aesthetic ✅ Clean layout with clear focal point
H9 Error Recovery ✅ Error messages are inline with fix instructions
H10 Help ✅ Tooltip icons on CVV and promo code fields

✅ Strong checkout flow with clear step progression

— Claude
```

---

## Getting started

### 1. Download

Go to [Releases](https://github.com/OJBoon/figwatch/releases/latest) and download **FigWatch.zip**.

### 2. Install

Unzip and drag **FigWatch.app** to your **Applications** folder.

### 3. First launch

**Right-click** FigWatch.app and select **Open** (required once — macOS blocks unsigned apps on first launch). After this, it opens normally from Spotlight, Launchpad, or the Applications folder.

### 4. Onboarding

FigWatch will walk you through setup on first launch:

#### Claude Code CLI

FigWatch uses Claude Code to power its AI audits. If you don't have it installed:

1. Click **Install** in the onboarding checklist — this opens the download page
2. Follow the instructions to install Claude Code
3. Click **Login** in the onboarding checklist — this opens Terminal
4. Sign in with your Anthropic account when prompted
5. Close Terminal and click **Check Again** in FigWatch

#### Figma Personal Access Token

FigWatch needs a token to read and reply to Figma comments:

1. Click **Set Up** in the onboarding checklist
2. Click **Get Token** — this opens Figma's settings page
3. In Figma: **Settings → Security → Personal Access Tokens → Generate new token**
4. Give it a name (e.g. "FigWatch"), copy the token
5. Paste it back in FigWatch and click **Connect**
6. You'll see a notification: "Connected as [your name]"

#### Figma Desktop

Open Figma Desktop so FigWatch can detect your open files. The app works best with Figma Desktop running — it auto-detects your open files and enables screenshot-based analysis for `@ux` audits.

### 5. Start watching

1. Click the FigWatch icon in your menu bar
2. Select a file from the list
3. The icon shows a green dot when watching
4. Go to Figma and comment `@tone` or `@ux` on any frame

FigWatch immediately replies with "Claude is working on it..." and then replaces it with the full audit when ready.

---

## Using FigWatch

### Menu bar

- Click the FigWatch icon to open the popover
- Green dot on the icon = actively watching a file
- Select any open Figma file to start watching
- Click **Disconnect** to stop watching
- Change the locale with the flag dropdown (affects `@tone` audits)

### Commenting in Figma

| Comment | What happens |
|---------|-------------|
| `@tone` | Audits copy against UK Tone of Voice guidelines |
| `@tone de` | Audits against German guidelines |
| `@tone fr` | Audits against French guidelines |
| `@tone nl` | Audits against Dutch guidelines |
| `@ux` | Runs a full 10-heuristic usability evaluation |
| `@ux check the navigation` | UX audit with extra context for Claude |

**Tips:**
- Place your comment directly **on a text layer** for a focused `@tone` audit of that specific text
- Place your comment on a **frame** for a broader audit of all copy in that frame
- `@ux` evaluates the entire top-level frame regardless of where you place the comment
- You can comment `@tone` or `@ux` again in the same thread to re-run the audit

### Locale

The locale setting affects `@tone` audits. Each locale has its own Tone of Voice guidelines covering:

| Locale | Language | Key rules |
|--------|----------|-----------|
| UK | English | Friendly, clear, no hype, £2.00 format |
| DE | German | Formal (Sie), direct, €2,00 format, German quotes „" |
| FR | French | Elegant (vous), guillemets « », €2,00 after amount |
| NL | Dutch | Informal (je/jij), plain-speaking, minimal exclamation marks |
| BNX | Benelux | Multi-language, softer Flemish Dutch, warmer Belgian French |

---

## Requirements

| Requirement | Why | How to get it |
|-------------|-----|---------------|
| macOS 13+ | App framework | You probably have this |
| Claude Code CLI | Powers the AI audits | [Install guide](https://docs.anthropic.com/en/docs/claude-code/getting-started) |
| Figma PAT | Reads and replies to comments | Figma → Settings → Security → Personal Access Tokens |
| Figma Desktop | File detection + screenshots | [Download Figma](https://www.figma.com/downloads/) |

---

## Troubleshooting

**App doesn't appear in menu bar**
- Right-click → Open on first launch (Gatekeeper bypass)
- Check if it's already running (only one instance allowed)

**No files showing in the list**
- Make sure Figma Desktop is open with at least one file
- Check that the file is a Design or FigJam file (not the Figma home page)

**Comment not getting a response**
- Check the green dot is showing (watcher is active)
- Make sure you're commenting on the file you selected in FigWatch
- Check that Claude Code is installed and logged in: run `claude auth status` in Terminal

**"Claude is working on it..." appears but no response**
- `@tone` audits take 15-30 seconds
- `@ux` audits take 60-120 seconds (screenshots + full heuristic evaluation)
- If it disappears with no response, check Terminal: `claude auth status`

**Token issues**
- Click the key icon in the footer to update your Figma token
- Tokens can expire — generate a new one in Figma Settings if needed

---

## For developers

### Project structure

```
figwatch/
  app/
    FigWatch.py          # macOS menu bar app (PyObjC)
    watcher.py           # Comment polling + Figma API
    handlers/
      tone.py            # @tone audit handler
      ux.py              # @ux heuristic eval handler
    skills/
      tone/              # ToV guidelines per locale
      ux/                # Nielsen heuristics reference
    build.sh             # Build script
    setup.py             # py2app config
```

### Building from source

```bash
# Prerequisites
pip3 install rumps py2app

# Build
cd app && bash build.sh

# Output
# app/build/FigWatch.app (24MB)
# app/build/FigWatch.zip (13MB)
```

### Running in dev mode

```bash
# Run the app directly (no build needed)
cd app && python3 FigWatch.py

# Run just the watcher (CLI mode)
cd app && python3 watcher.py watch <figma-file-key> -l uk
```
