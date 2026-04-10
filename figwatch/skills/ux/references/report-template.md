# Heuristic Evaluation Report Template

Use this exact structure for every report. Replace all `[PLACEHOLDER]` values with real content. Do not omit sections — if a section has no findings, state that explicitly.

---

## Output Filename Convention

```
figma-heuristic-eval-[sanitised-screen-name]-[YYYY-MM-DD].md
```

Sanitise: lowercase, spaces and special characters → hyphens, truncate to 40 chars.

---

## Report Template

---

# Heuristic Evaluation: [SCREEN NAME]

**Date:** [YYYY-MM-DD]
**File Key:** [FILE KEY]
**Source Node ID:** [ORIGINAL NODE ID FROM COMMENT]
**Evaluated Frame ID:** [PARENT FRAME ID — may differ if comment was on a sub-element]
**Evaluator:** Claude (automated heuristic evaluation via figma-ds-cli)
**Data sources:** Screenshot (visual) + Node tree JSON (structural)

---

## Flow Context

**Journey type:** [e.g. "E-commerce browse → product detail", "Checkout — step 2 of 4", "Account onboarding — step 3"]

**Screen position:** [Where this screen sits in the broader user flow]

**Likely entry from:** [What screen or state the user comes from]

**Likely exit to:** [Where the primary CTA leads]

**Flow-sensitive heuristics:** [List which heuristics have elevated severity due to flow position, with a one-sentence rationale for each. E.g. "H3 (Control): Missing back navigation is severity 3 here because this screen is reached mid-checkout, not from a root screen."]

---

## Executive Summary

[2–4 sentences. Lead with the most critical finding. State the overall severity level.]

**Overall Severity:** [Low / Medium / High]
*(Low = no findings above severity 2 | Medium = at least one severity 3 | High = at least one severity 4)*

**Critical issues (severity 4):** [N]
**Major issues (severity 3):** [N]
**Minor issues (severity 1–2):** [N]

---

## Methodology

Evaluation performed by cross-referencing two data sources exported via `figma-ds-cli`:

1. **Visual perspective**: Screenshot of the top-level parent frame at 2x scale, read as an image and assessed for hierarchy, density, contrast, affordance clarity, and navigation cues.
2. **Structural perspective**: Full node tree of the same frame (depth 10), read as JSON and assessed for text content, component usage, colour/spacing values, variant states, and layer naming.

Both perspectives were consulted for each of the 10 Nielsen heuristics. Severity scores reflect the combined evidence plus the screen's inferred position in its user flow.

---

## Severity Scale

| Rating | Meaning |
|---|---|
| 0 | Not a usability problem |
| 1 | Cosmetic — fix if time permits |
| 2 | Minor — low priority |
| 3 | Major — fix next sprint |
| 4 | Catastrophe — fix before release |

---

## Heuristic Findings

### H1 — Visibility of System Status

**Severity:** [0–4]

**Visual finding:**
[What the screenshot shows about loading states, progress indicators, status feedback, and active states.]

**Structural finding:**
[What the node tree shows: text node content for status strings, layer names for skeleton/spinner/toast/progress components, variant state presence/absence.]

**Flow context impact:**
[How the screen's position in the flow affects this score. E.g. "As step 2 of a 4-step checkout, the absence of a step progress indicator is more impactful than it would be on a standalone screen."]

**Recommendation:**
[1–2 specific, actionable fixes. Name the component or screen location. E.g. "Add a step progress bar ('Step 2 of 4') to the screen header, using the existing Progress component from the design system."]

---

### H2 — Match Between System and the Real World

**Severity:** [0–4]

**Visual finding:**
[Icon metaphor accuracy, date/number format observations from the screenshot.]

**Structural finding:**
[Text content audit findings: jargon, technical IDs, system codes, camelCase labels, generic CTA verbs.]

**Flow context impact:**
[Whether the audience at this flow stage would know the terminology used.]

**Recommendation:**
[1–2 specific fixes with example replacement text where relevant.]

---

### H3 — User Control and Freedom

**Severity:** [0–4]

**Visual finding:**
[Presence/absence of back, close, cancel, undo controls visible in the screenshot.]

**Structural finding:**
[Layer names: "back", "cancel", "close", "dismiss", "undo". Modal/overlay dismiss layers. Dead-end detection.]

**Flow context impact:**
[Whether missing exit controls are more or less severe given flow position.]

**Recommendation:**
[1–2 specific fixes. Name the frame and element to add/change.]

---

### H4 — Consistency and Standards

**Severity:** [0–4]

**Visual finding:**
[Button visual hierarchy uniformity, input field consistency, icon set coherence observed in screenshot.]

**Structural finding:**
[Instance vs raw frame ratio, distinct fill colour count, distinct font size count, spacing value irregularities, detached components identified.]

**Flow context impact:**
[Whether inconsistencies are within a single flow (higher severity) or cross-flow (lower severity).]

**Recommendation:**
[1–2 specific fixes. Reference the specific component or layer to address.]

---

### H5 — Error Prevention

**Severity:** [0–4]

**Visual finding:**
[Input constraint hints, disabled CTA states, confirmation dialog presence visible in screenshot.]

**Structural finding:**
[Component variant states found/missing: "error", "disabled", "required". Confirmation overlay frames. Input helper text nodes.]

**Flow context impact:**
[Whether the screen handles irreversible actions (payment, send) — elevates severity if prevention is missing.]

**Recommendation:**
[1–2 specific fixes. Name the input component and state to add.]

---

### H6 — Recognition Rather Than Recall

**Severity:** [0–4]

**Visual finding:**
[Icon labelling, location cues, breadcrumb/stepper, visible vs hidden options in screenshot.]

**Structural finding:**
[Icon layers with/without sibling text nodes. Breadcrumb/stepper layer presence. Overflow menu item count. Previous-step summary in review screens.]

**Flow context impact:**
[Whether missing location cues are more severe given the screen's depth in the flow.]

**Recommendation:**
[1–2 specific fixes. E.g. "Add text labels below the 5 bottom navigation icons."]

---

### H7 — Flexibility and Efficiency of Use

**Severity:** [0–4]

**Visual finding:**
[Search bar, filter/sort controls, FAB, quick-action chips visible in screenshot.]

**Structural finding:**
[Layer names: "search", "filter", "bulk-select", "shortcut", "fab". Pre-populated input states.]

**Flow context impact:**
[Whether efficiency features are appropriate for this screen type and user profile at this flow stage.]

**Recommendation:**
[1–2 specific fixes or additions.]

---

### H8 — Aesthetic and Minimalist Design

**Severity:** [0–4]

**Visual finding:**
[Competing focal point count, whitespace quality, decorative element proportion, typographic hierarchy clarity in screenshot.]

**Structural finding:**
[Distinct font size count: [N]. Distinct fill colour count: [N]. Total layer count: [N]. Decorative layer names at root level: [list if any].]

**Flow context impact:**
[Whether density is appropriate for this screen type (reference screens can be denser; task screens should be minimal).]

**Recommendation:**
[1–2 specific reductions or simplifications. E.g. "Consolidate the 7 font sizes to a 4-size scale: 28/20/16/14px."]

---

### H9 — Help Users Recognize, Diagnose, and Recover from Errors

**Severity:** [0–4]

**Visual finding:**
[Error message placement and visual clarity. Recovery action visibility. Empty vs error state distinction in screenshot.]

**Structural finding:**
[Error message text content quality (plain language, specific, actionable?). Error state variant presence on inputs. Recovery CTA layers.]

**Flow context impact:**
[Whether a missing error state or weak recovery path is catastrophic at this flow stage (e.g. payment submission).]

**Recommendation:**
[1–2 specific fixes. Quote improved error message text where relevant.]

---

### H10 — Help and Documentation

**Severity:** [0–4]

**Visual finding:**
[Contextual help icons, onboarding elements, empty state guidance visible in screenshot.]

**Structural finding:**
[Layer names: "tooltip", "help", "onboarding", "coach", "empty-state". Form helper text nodes. Help navigation entry points.]

**Flow context impact:**
[Whether the screen introduces non-obvious features that require contextual help.]

**Recommendation:**
[1–2 specific additions. E.g. "Add an info tooltip to the 'Promo code' field explaining the accepted format."]

---

## Summary Table

| # | Heuristic | Severity | Status |
|---|---|---|---|
| H1 | Visibility of System Status | [0–4] | [Pass / Issue] |
| H2 | Match Between System and Real World | [0–4] | [Pass / Issue] |
| H3 | User Control and Freedom | [0–4] | [Pass / Issue] |
| H4 | Consistency and Standards | [0–4] | [Pass / Issue] |
| H5 | Error Prevention | [0–4] | [Pass / Issue] |
| H6 | Recognition Rather Than Recall | [0–4] | [Pass / Issue] |
| H7 | Flexibility and Efficiency of Use | [0–4] | [Pass / Issue] |
| H8 | Aesthetic and Minimalist Design | [0–4] | [Pass / Issue] |
| H9 | Help Users Recognize, Diagnose, and Recover from Errors | [0–4] | [Pass / Issue] |
| H10 | Help and Documentation | [0–4] | [Pass / Issue] |

*Status: "Pass" = severity 0–1. "Issue" = severity 2 or above.*

---

## Prioritised Action Items

### Critical — Fix Before Release (Severity 4)

[If none: "No severity-4 issues identified on this screen."]

1. **[Short issue title]** (H[N] — [Heuristic name])
   [One sentence describing the problem and which frame/element it applies to.]
   Fix: [Specific action to take]

### High Priority — Fix Next Sprint (Severity 3)

[If none: "No severity-3 issues identified on this screen."]

1. **[Short issue title]** (H[N])
   [One sentence description + frame/element reference]
   Fix: [Specific action]

### Medium Priority — Add to Backlog (Severity 2)

[If none: "No severity-2 issues identified on this screen."]

1. **[Short issue title]** (H[N])
   [One sentence description + frame/element reference]
   Fix: [Specific action]

---

## Limitations

- Evaluation covers one frame. The broader flow context is inferred from screen content and naming — not validated from the full file.
- No interactive prototype testing was performed. Issues that only emerge during actual user interaction (transition timing, keyboard behaviour, scroll behaviour) are not covered.
- Colour contrast was assessed visually from the screenshot — not measured against WCAG 2.1 ratios. For a full accessibility audit, run a dedicated contrast analysis.
- Flow context (journey type, entry/exit) is inferred. If the inference is incorrect, some severity scores may need adjustment.
- [Any specific limitations from this evaluation, e.g. "The error states for the form were not present in the evaluated frame."]

---

## Comment Reply

*(This is the text the app should post as the reply to the `@ux` comment in Figma.)*

```
@ux audit — [Screen Name] ([flow context summary])

🔴 [Critical finding — one sentence, max 15 words]
🟠 [Major finding — one sentence, max 15 words]
🟡 [Minor finding(s)]
✅ [One positive observation]

Full report: [report filename]
```

---

*Generated by the `figma-heuristic-eval` Claude Code skill on [YYYY-MM-DD] using figma-ds-cli.*
