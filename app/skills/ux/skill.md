---
name: figma-heuristic-eval-comment
description: Lightweight comment-reply mode for heuristic evaluation. Evaluates a Figma
  screen against Nielsen's 10 Usability Heuristics using a screenshot and node tree
  exported by the calling handler. Returns a short plain-text Figma comment reply only
  (no markdown report).
version: 1.0.0
---

# Figma Heuristic Evaluation — Comment Reply Mode

You are given two data sources for a single Figma screen:

1. **Screenshot** — a PNG image of the full screen frame (read it as an image)
2. **Node tree** — a JSON structure of the full screen frame

Cross-reference BOTH sources to evaluate all 10 of Nielsen's Usability Heuristics. Use `references/nielsen-heuristics.md` for the precise criteria, visual/structural signals, and severity rules.

---

## Input

You will receive:
- `screenName` — name of the top-level frame being evaluated
- `screenshotPath` — file path to the exported PNG screenshot (read this image)
- `treePath` — file path to the exported node tree JSON (read this file)
- `flowContext` — (optional) any context about the screen's position in a flow

---

## Evaluation Process

### Step 1 — Read both data sources
Read the screenshot image and the node tree JSON file.

### Step 2 — Establish flow context
From the screen name, content, and navigation elements, infer:
- Journey type (e.g. checkout, onboarding, browse, settings)
- Screen position (e.g. "step 2 of 4", "detail page", "root screen")
- This context affects severity scores for H1, H3, and H6.

### Step 3 — Evaluate all 10 heuristics
For each heuristic, consult both the screenshot (visual signals) and the node tree (structural signals) as specified in the heuristics reference. Assign a severity score (0-4).

### Step 4 — Compose the comment reply

---

## Output Format

CRITICAL — this is a Figma comment reply. Figma comments are PLAIN TEXT ONLY.

- NO markdown. No asterisks, hashes, backticks, bullet markers, or code blocks.
- Use blank lines between sections for readability.
- Do NOT add sign-offs, summaries, or preamble. The header and sign-off are added automatically by the app.

Structure:

Line 1: Overall severity verdict
  🟢 No issues  |  🟡 Minor issues  |  🟠 Major issues  |  🔴 Critical issues

Blank line.

Then list ALL 10 heuristics — every single one, in order H1-H10. This is a heuristic evaluation and must cover all pillars. Use this format for each:

For heuristics with no issues (severity 0):
  H[N] [Short name] ✅ [Brief reason why it passes — what specific element satisfies this heuristic]

For heuristics with findings (severity 1+):
  H[N] [Short name] [emoji] [Finding — what the issue is and why it matters for the user]
  → [Specific recommendation]

Use these severity emojis: 🔴 severity 4, 🟠 severity 3, 🟡 severity 1-2

IMPORTANT: Always explain WHY. For passes, name the specific element that satisfies the heuristic (e.g. "back arrow visible in header", "step indicator shows 2 of 3"). For issues, explain the user impact (e.g. "users can't recover if they tap the wrong option", "forces users to memorise icon meanings"). Never just state the finding without the reasoning.

Blank line between each heuristic that has a finding. No blank line between passing heuristics (keep them tight).

End with one blank line then exactly one positive observation:
  ✅ [What the design does well and why it works]

IMPORTANT: Focus on real usability issues from the screenshot and user flow, not design system hygiene (layer names, token usage, component detachment). Those are design system concerns, not usability heuristics. A heuristic evaluation assesses the end-user experience.

### Example output

```
🟠 Major issues

H1 System Status ✅ Step indicator at top clearly shows "Step 2 of 4"
H2 Real World Match ✅ All labels use plain English, CTAs are task-specific ("Add to basket")

H3 User Control 🟠 No back button — users in step 2 of checkout have no way to return to step 1 to change their selection
→ Add a back arrow to the header navigation

H4 Consistency ✅ Button hierarchy is clear with one primary and two secondary actions

H5 Error Prevention 🟠 Payment form has no inline validation — users won't know their card number is wrong until they submit
→ Add error + disabled variants to all form inputs

H6 Recognition 🟡 4 bottom nav icons with no text labels — users must memorise what each icon means
→ Add text labels below each navigation icon

H7 Flexibility ✅ Search bar is prominent at the top of the catalogue

H8 Aesthetic 🟡 6 competing focal points — the promotional banner, search, filters, product grid, and two CTAs all fight for attention
→ Reduce to 2-3 by grouping secondary info into collapsible sections

H9 Error Recovery ✅ Error messages appear inline next to each field with clear fix instructions
H10 Help ✅ Tooltip icons on the CVV and promo code fields explain what to enter

✅ Strong checkout flow with clear step progression and well-labelled form fields that guide the user confidently through payment
```
