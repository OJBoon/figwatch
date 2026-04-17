---
name: tone-reviewer
description: Review and fix copy in Figma designs against Joybuy Tone of Voice guidelines using the Figma CLI (figma-ds-cli), and generate translation tables as both Figma frames and Excel exports. Use this skill whenever the user wants to check, audit, review, or improve copy/text in Figma files, find unnatural or off-brand wording in UI designs, enforce tone of voice rules on Figma screens, translate UI copy to DE/FR/NL/Benelux languages, export translations to Excel, or add a translation table to a Figma file. Also trigger when the user mentions Figma together with tone of voice, copy review, localisation, translation, or brand guidelines — even if they don't say "ToV" explicitly.
---

# Figma ToV Reviewer

Review and improve copy in Figma app/web UI designs against Joybuy's locale-specific Tone of Voice guidelines, and generate translation tables — all via the Figma CLI.

This skill has two modes:

1. **Copy Review** — Extract text from a Figma file, flag copy that sounds unnatural or violates ToV rules, propose replacements, let the user review, then apply approved changes back to Figma.
2. **Translation Table** — Extract copy from a Figma file and create a new frame containing a table with translations for each locale (DE, FR, NL, Benelux NL+FR), all meeting the respective ToV guidelines.

3. **Comment Reply** — Given specific text from a Figma comment trigger (e.g. `@tone`), produce a very short, plain-text ToV audit suitable for posting as a Figma comment reply. Used by the FigWatch comment watcher.

All three modes can be run independently.

---

## Prerequisites & Setup

Before doing anything, check whether the Figma CLI is available:

```bash
which fig-start 2>/dev/null || echo "NOT_INSTALLED"
```

**If installed**, skip to the workflow. **If not installed**, walk the user through setup:

1. **Clone and install:**
   ```bash
   git clone https://github.com/silships/figma-cli.git ~/figma-cli
   cd ~/figma-cli && npm install && npm run setup-alias && source ~/.zshrc
   ```
2. **Requirements:** Node.js 18+, Figma Desktop (not the web app), macOS recommended
3. **First run:** `fig-start` launches Figma if needed, connects via CDP, and presents a file picker
4. **Safe Mode** (no binary patching): `fig-start --safe` — requires manually starting the Figma plugin each session but avoids modifying the Figma app
5. **macOS Full Disk Access** is needed for Yolo Mode (default) — grant it to the terminal app in System Settings > Privacy & Security

Once connected, all `fig` commands operate on the selected Figma file.

---

## Mode 1: Copy Review

### Step 1 — Extract all text nodes

Use the Figma CLI to find every text node in the current page (or a user-specified frame/page):

```bash
fig find --type TEXT
```

This returns a list of text nodes with their IDs, names, and content. If the file is large, the user may want to scope it to a specific page or frame — ask if there are multiple pages.

### Step 2 — Load the relevant ToV guide

Ask the user which locale this file targets, or infer it from the copy language. Then read the matching reference:

- **DE** → `references/tov-de.md`
- **FR** → `references/tov-fr.md`
- **NL** → `references/tov-nl.md`
- **Benelux** → `references/tov-benelux.md`

If the file contains multiple languages (common for Benelux), load all relevant guides.

### Step 3 — Analyse the copy

For each text node, check against these criteria (informed by the ToV guide):

**Tone & naturalness:**
- Does it sound like something a real person would say, or is it stiff/robotic/overly literal?
- Does the formality level match the locale (Sie vs du, vous vs tu)?
- Is the energy level right (e.g. Dutch = restrained, French = warm but not excessive)?

**ToV-specific rules:**
- No hype language (amazing, incredible, etc.) — use the locale's recommended alternatives
- Correct currency formatting (symbol position, comma decimals, spacing)
- Correct punctuation (guillemets for FR, German quotes for DE, exclamation mark usage)
- No anglicisms where a native equivalent exists (especially FR)

**Glossary compliance:**
- Cross-reference against the Key Phrases and CTA tables in the ToV guide
- Flag any term that has an approved translation but uses a different wording
- Check "Things to Avoid" tables for known anti-patterns

### Step 4 — Present findings for review

Present the findings as a clear table. Identify text nodes by their actual copy content (designers rarely name layers, so layer names are unreliable). Include the node ID for applying changes later.

**Format:**
```
## Copy Review: [File Name] — [Locale]

### Issues Found

| # | Current Copy | Issue | Suggested Fix |
|---|-------------|-------|--------------|
| 1 | Gain points now!! | Hype language + double exclamation | Joypoints verdienen |
| 2 | Bind your email | "Bind" is not natural German | E-Mail-Adresse verknüpfen |
| ...

### Clean (no issues)
- "Shop Now" ✓
- "Jetzt einkaufen" ✓
- [etc.]

**Total: X issues across Y text nodes**
```

Ask the user to review: "Here are the issues I found. You can approve each fix, modify it, or skip it. Let me know which changes to apply."

### Step 5 — Add annotations to a dedicated layer

Rather than directly changing text in the designs, add visual annotations that sit in their own togglable layer. This lets designers review suggestions without any risk to the source designs, and easily hide the annotations when they're not needed.

**Create the annotation layer:**

```bash
# Create a top-level group to hold all annotations — designers can toggle its visibility and move individual cards freely
fig eval "const group = figma.group([], figma.currentPage); group.name = 'ToV Review Annotations';"
```

**For each flagged issue, create an annotation pinned near the offending text node:**

Use `fig eval` to read the position and dimensions of the flagged text node, then place a small annotation card next to it, then add it to the "ToV Review Annotations" group.

Each annotation should be a small, visually distinct card:
- **Background:** Semi-transparent amber/yellow (like a sticky note) — e.g. `rgba(255, 200, 50, 0.9)`
- **Border:** 1px solid darker amber for definition
- **Content:** Two lines of text:
  - **Issue** (bold, small): e.g. "Hype language + double exclamation"
  - **Suggested fix** (regular): e.g. "→ Joypoints verdienen"
- **Position:** Offset slightly above-right of the flagged text node so it doesn't obscure the original copy
- **Size:** Auto-width based on content, max ~250px wide

```bash
# Example: get position of a flagged node, then create annotation nearby
fig eval "
  const node = figma.getNodeById('flagged-node-id');
  const x = node.absoluteTransform[0][2] + node.width + 12;
  const y = node.absoluteTransform[1][2] - 8;
  JSON.stringify({x, y});
"

# Create annotation card as child of the annotations layer
fig eval "
  const group = figma.currentPage.findOne(n => n.name === 'ToV Review Annotations');
  const card = figma.createFrame();
  card.name = 'Annotation: [short issue]';
  card.resize(240, 60);
  card.x = [calculated_x];
  card.y = [calculated_y];
  card.fills = [{type: 'SOLID', color: {r: 1, g: 0.78, b: 0.2}, opacity: 0.9}];
  card.cornerRadius = 6;
  group.appendChild(card);

  const issueText = figma.createText();
  // ... set font, content, etc.
  card.appendChild(issueText);

  const fixText = figma.createText();
  // ... set font, content with suggested fix
  card.appendChild(fixText);
"
```

Adapt the exact script based on what the CLI supports — the key principle is: all annotations live inside the single "ToV Review Annotations" group so they can be toggled on/off together, and individual cards can be freely moved by designers.

**After adding all annotations**, tell the user:
"I've added annotation cards for X issues. They're all inside a layer called **'ToV Review Annotations'** — you can toggle it on/off in the layers panel. Each card shows the issue and suggested fix next to the affected text."

**If the user later wants to apply a fix**, they can come back and ask to apply specific changes. At that point, use:
```bash
fig select --id "node-id" && fig text set "approved new copy"
```
And remove the corresponding annotation card from the layer.

---

## Mode 2: Translation Table

### Step 1 — Extract source copy

Same as Copy Review Step 1 — extract all text nodes. Ask the user which language the source copy is in (or detect it).

### Step 2 — Load all ToV guides

Read all four reference files since we're translating to every locale:
- `references/tov-de.md`
- `references/tov-fr.md`
- `references/tov-nl.md`
- `references/tov-benelux.md`

### Step 3 — Generate translations

For each text node, produce translations to all target languages. The translation process is not just linguistic — each translation must meet that locale's ToV requirements:

- **DE:** Formal (Sie), direct, no hype, precise. Use approved Key Phrases where they exist.
- **FR:** Elegant, warm (vous), confident. Use guillemets, space before ! and ?. Currency symbol after amount.
- **NL:** Plain-speaking, informal (je/jij), concise. Minimal exclamation marks. No overselling.
- **Benelux NL:** Slightly softer than Netherlands Dutch (Flemish tone).
- **Benelux FR:** Slightly less formal than Parisian French (Belgian French warmth).

Always check the Key Phrases and CTA tables first — if an approved translation exists, use it rather than generating a new one. This ensures consistency across the product.

### Step 4 — Present the translation table for review

Show the complete table to the user before creating anything in Figma:

```
## Translation Table: [File Name]

| Source (EN) | DE | FR | NL | BE-NL | BE-FR |
|------------|----|----|-------|-------|-------|
| Shop Now | Jetzt einkaufen | Acheter maintenant | Nu winkelen | Nu winkelen | Acheter maintenant |
| Earn Joypoints | Joypoints sammeln | Gagnez des Joypoints | Joypoints verdienen | Joypoints verdienen | Gagnez des Joypoints |
| ... |

**Notes:**
- [any locale-specific formatting notes, e.g. "FR currency: symbol after amount"]
- [any terms where no approved translation exists and you created one]
```

Ask the user to review and adjust before proceeding.

### Step 5 — Export to Excel

Before creating anything in Figma, generate an Excel file so the user has a spreadsheet version (this matches their existing workflow where translations live in Excel). Write a small Python script using `openpyxl`:

```python
import openpyxl
from pathlib import Path

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Translations"

# Header row
headers = ["Source (EN)", "DE", "FR", "NL", "BE-NL", "BE-FR"]
ws.append(headers)
for cell in ws[1]:
    cell.font = openpyxl.styles.Font(bold=True)

# Data rows
for row in translations:
    ws.append([row["source"], row["de"], row["fr"], row["nl"], row["be_nl"], row["be_fr"]])

# Auto-width columns
for col in ws.columns:
    max_len = max(len(str(cell.value or "")) for cell in col)
    ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 50)

output_path = Path.home() / "Downloads" / f"translations_{file_name}.xlsx"
wb.save(output_path)
```

Tell the user: "Saved the translation spreadsheet to ~/Downloads/translations_[file_name].xlsx"

If `openpyxl` is not installed, install it first: `pip install openpyxl`.

### Step 6 — Create the translation frame in Figma

After the user approves, create a new section and frame in the Figma file that contains the translation table:

```bash
# Create a new section for translations
fig create frame --name "Translations" --width 1400 --height [calculated] --x 0 --y [below existing content]

# Add header row
fig create text --text "Source (EN)" --parent "Translations" --x 0 --y 0 --size 14 --weight bold
fig create text --text "DE" --parent "Translations" --x 240 --y 0 --size 14 --weight bold
fig create text --text "FR" --parent "Translations" --x 480 --y 0 --size 14 --weight bold
fig create text --text "NL" --parent "Translations" --x 720 --y 0 --size 14 --weight bold
fig create text --text "BE-NL" --parent "Translations" --x 960 --y 0 --size 14 --weight bold
fig create text --text "BE-FR" --parent "Translations" --x 1200 --y 0 --size 14 --weight bold

# Add each row of translations...
```

Use auto-layout on the frame if the CLI supports it, to keep things tidy. The exact commands will depend on the file structure — adapt as needed.

After creation, tell the user: "I've added a 'Translations' frame to the file and saved the Excel version to ~/Downloads/. You'll find the frame below the existing content on the current page."

---

## Mode 3: Comment Reply

This mode is invoked programmatically by the FigWatch comment watcher (not interactively). It receives pre-extracted text and must return a short, plain-text audit.

### Input

You will receive:
- `locale` — the target locale (uk, de, fr, nl, benelux)
- `texts` — a list of text nodes, each with a `name` and `text` property
- `targeted` — boolean, whether a specific text layer was targeted
- `targetName` — the name of the targeted text layer (if targeted)
- `primaryText` — the exact text of the targeted layer (if targeted)

### Rules

1. Load the relevant ToV guide from `references/` for the locale.
2. If `targeted` is true, focus the audit on `primaryText`. The other text nodes are nearby context only — mention them only if they have obvious issues.
3. If `targeted` is false, briefly audit all provided text nodes.

### Analysis checklist (same as Mode 1)

- Tone & naturalness for the locale
- Formality level (Sie/du, vous/tu, je/jij)
- Hype language (check "Things to Avoid" table)
- Currency formatting (symbol position, decimal separator, spacing)
- Punctuation conventions (guillemets, German quotes, exclamation marks)
- Glossary compliance (Key Phrases, CTAs, "Things to Avoid")
- Brand terms (Joypoints, Joybuy) must never be altered

### Output format

CRITICAL — this is a Figma comment reply. Figma comments are PLAIN TEXT ONLY:

- NO markdown whatsoever. No asterisks, hashes, backticks, bullet symbols (* or -), or code blocks.
- Keep it concise but don't artificially truncate. Use as many lines as needed for real issues, but no filler.
- Use blank lines between each issue for readability.
- Do NOT list text that is fine. Only flag problems.
- Do NOT add sign-offs, summaries, headers, or preamble. The header and sign-off are added automatically.

Structure:

Line 1: Verdict emoji and one word.
  ✅ Pass  |  ⚠️ Minor issues  |  🔴 Needs attention

Then a blank line.

Then each issue as a block (separated by blank lines):
  🔤 "original text"
  → suggested fix
  (short reason)

If everything passes: verdict line, blank line, one short sentence.

### Example outputs

Good — passes:
```
✅ Pass

Copy is clear, on-brand, and uses approved terminology.
```

Good — issues found:
```
⚠️ Minor issues

🔤 "Buy now!"
→ "Jetzt kaufen"
(use approved DE CTA, remove exclamation)

🔤 "€2.00"
→ "€2,00"
(comma as decimal separator for DE)
```

Good — targeted single text:
```
✅ Pass

"Jetzt einkaufen" matches the approved DE CTA exactly.
```

Bad — too long, uses markdown, no line breaks:
```
## Tone of Voice Audit
**Overall verdict:** Minor issues found
- **"Buy now!"** — This uses English instead of German...
[continues for 15 more lines]
```

---

## Combining modes

If the user wants both a copy review and translations, run them in this order:

1. **Copy Review first** — fix the source copy so it's clean
2. **Translation Table second** — translate the corrected copy

This avoids translating copy that's about to be changed.

---

## Edge cases

- **Mixed-language files:** Some Figma files may contain multiple languages already (e.g. a Benelux file with NL and FR sections). Detect this and review each section against the correct locale's ToV guide.
- **Component instances:** Text in component instances may be overridden locally. Flag if an override diverges from the main component's copy.
- **Very long files:** If there are hundreds of text nodes, offer to work page-by-page or frame-by-frame rather than dumping everything at once.
- **Copy that's placeholder/lorem ipsum:** Skip it — don't flag dummy text as a ToV violation.
- **Brand terms (Joypoints, Joybuy):** These should never be translated or altered. Flag if they've been modified.

---

## ToV Reference Files

The `references/` directory contains the full Tone of Voice guide for each locale. Read the relevant one(s) for any review or translation task:

- `references/tov-de.md` — German market (formal, precise, trustworthy)
- `references/tov-fr.md` — French market (elegant, warm, confident)
- `references/tov-nl.md` — Netherlands (direct, plain-speaking, concise)
- `references/tov-benelux.md` — Belgium/Luxembourg (multi-language, softer Dutch, warmer French)

Each guide contains: tone principles, currency/formatting rules, approved CTAs, key phrase glossary, and things-to-avoid tables. These are the source of truth for all copy decisions.
