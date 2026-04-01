# Nielsen's 10 Usability Heuristics — Evaluation Reference

Detailed criteria for evaluating each heuristic using two sources: the frame screenshot (visual perspective) and the node tree JSON (structural perspective). Includes flow context severity rules and escalation triggers.

---

## Nielsen Severity Scale

| Rating | Meaning | Action |
|---|---|---|
| 0 | Not a usability problem | No action required |
| 1 | Cosmetic problem only | Fix if time permits |
| 2 | Minor usability problem | Low priority — add to backlog |
| 3 | Major usability problem | High priority — fix next sprint |
| 4 | Usability catastrophe | Fix before release |

---

## H1 — Visibility of System Status

**Definition:** The design keeps users informed about what is going on through appropriate feedback within reasonable time.

### Visual signals (screenshot)
- **Loading states**: Is a spinner, skeleton screen, or progress indicator visible? If the screen depicts an action in progress (e.g. a submit button pressed, a list being fetched), a loading state should be visible.
- **Progress indicators**: For multi-step flows (checkout, onboarding, setup wizards), is a step counter ("Step 2 of 4"), progress bar, or numbered breadcrumb visible at the top of the screen?
- **Status badges**: Are there any notification badges, unread count indicators, or sync status icons visible?
- **Confirmation feedback**: Does a success/error/warning banner or toast appear after an action completes?
- **Active/selected states**: Do tabs, nav items, and filter chips show a visually distinct active state?

### Structural signals (node tree)
- Text nodes containing: "loading", "saving", "uploading", "syncing", "processing", "step N of N", "%", "progress"
- Layer names containing: "skeleton", "spinner", "progress-bar", "step-indicator", "badge", "loading-state", "toast", "snackbar", "banner"
- Component variant properties keyed "loading" or "skeleton" present on the component
- Absence of any loading/status layer in a screen that clearly depicts a data-dependent view (list, profile, dashboard) = finding

### Flow context severity rules
- **No progress indicator** in a confirmed multi-step flow (3+ steps): severity 3
- **No progress indicator** on a standalone one-page action: severity 1
- **No loading state** for any async operation: severity 3
- **No confirmation feedback** after a destructive action (delete, send, pay): severity 4

### Severity escalation triggers
- No loading state on any screen: severity 3
- No confirmation after payment or send: severity 4
- Progress indicator present but shows wrong step count: severity 2

---

## H2 — Match Between System and the Real World

**Definition:** The design uses words, phrases, and concepts familiar to the user rather than system-oriented terms.

### Visual signals (screenshot)
- **Icon metaphors**: Do icons accurately represent their function? Flag: cloud icon for local save, floppy disk on mobile-first apps, phone handset icon for video call, envelope icon for push notification.
- **Visual metaphors**: Are illustrated elements recognisable to the target audience? Abstract illustrations that require learning reduce real-world match.
- **Date/number formats**: Are dates shown in ISO format (2026-04-01) when the audience likely expects "1 Apr 2026" or "April 1"? Are prices shown with the correct currency symbol and decimal format for the locale?

### Structural signals (node tree)
- **Text content audit** — read every text node. Flag:
  - Technical identifiers used as labels: `user_id`, `sku_ref`, `entity_type`, `created_at`
  - Camel/snake case in visible text: `firstName`, `productCategory`, `errorCode`
  - HTTP/system error codes exposed: "Error 403", "500 Internal Server Error", "NullPointerException"
  - Internal process names: "Back-office", "Admin view", "Dev mode", "Staging"
  - Acronyms without expansion on first use: "VAT", "SKU", "ETA", "MFA" in contexts where the audience may not know them
  - Action verbs that don't match the action: "Submit" on a search action, "Confirm" on a filter apply, "Execute" on a simple save
- **Button and CTA labels**: Are action verbs concrete and task-focused ("Add to basket", "Book appointment", "Send message") rather than generic ("OK", "Proceed", "Continue")?

### Flow context severity rules
- Error codes exposed without plain-language explanation at any flow stage: severity 3
- Technical labels on primary navigation (visible at all stages of the flow): severity 3
- Domain-specific jargon on a screen that is a user's first encounter with a concept: severity 2
- Same jargon on a screen deep in a flow where users would already know the term: severity 1

### Severity escalation triggers
- System error code with no plain-language message: severity 3
- Primary CTA labelled "OK" with no context: severity 2
- All form field labels are database column names: severity 3

---

## H3 — User Control and Freedom

**Definition:** Users can leave unwanted states without having to go through extended dialogues. Clear emergency exits exist.

### Visual signals (screenshot)
- **Back navigation**: Is a back button, back arrow, or "X" close button visible? Required on every screen that is not the root/home.
- **Cancel actions**: On any modal, drawer, overlay, or multi-step form, is a "Cancel" link or close button visible?
- **Undo affordance**: After a destructive action (delete, archive, mark as spam), is an "Undo" action visible (typically a snackbar/toast with undo)?
- **Empty/clear controls**: On filtered or searched views, is a "Clear filters" or "Clear search" option visible?
- **Dead-end detection**: Is the screen a confirmation/completion state with no forward or backward path?

### Structural signals (node tree)
- Layer names: "back", "back-button", "close", "cancel", "dismiss", "undo", "clear", "reset"
- Presence of modal/overlay frames: check if they have a close/dismiss layer as a direct child
- Absence of any navigation control on a non-root screen = finding
- Screens named "confirmation", "success", "complete", "done" — check for both a primary forward CTA and a secondary return/home option

### Flow context severity rules
- **No back navigation** on a screen reached from within a multi-step flow: severity 3
- **No back navigation** on a standalone landing page or root screen: severity 0 (expected)
- **No cancel button** on a modal that interrupts a task flow: severity 4
- **No undo** after a delete action with no confirmation dialog: severity 4
- **No undo** after a reversible low-stakes action (remove from favourites): severity 1

### Severity escalation triggers
- Full-screen modal with no dismiss mechanism: severity 4
- Multi-step flow with no way to return to step N-1: severity 3
- Destructive action confirmed with no subsequent undo/recovery path: severity 4

---

## H4 — Consistency and Standards

**Definition:** Users should not have to wonder whether different words, situations, or actions mean the same thing. Platform conventions are followed.

### Visual signals (screenshot)
- **Button visual hierarchy**: Is there a consistent primary/secondary/tertiary/destructive button hierarchy? Does the primary CTA look the same as on other screens visible in the evaluation?
- **Input field styling**: Are all input fields styled consistently (same border radius, border weight, label position)?
- **Card and list item styling**: Do repeated content blocks (product cards, list items, notification rows) share the same visual structure?
- **Icon set consistency**: Are all icons from the same visual family and weight? Mixed filled/outlined icons or different stroke widths = finding.
- **Navigation component**: Is the navigation pattern (bottom tab, top nav, sidebar) the same as on other evaluated screens?

### Structural signals (node tree)
- **Instance vs raw frame ratio**: Count component instances vs standalone frames/groups used as UI elements. A ratio below 60% instances suggests low component reuse — severity 2 or 3 depending on complexity.
- **Fill colour diversity**: Count distinct fill hex values. More than 8 distinct colour fills suggests inconsistent use of the colour system.
- **Font size count**: Count distinct `fontSize` values across text nodes. More than 4–5 distinct sizes on a single screen = H8 finding too.
- **Spacing values**: List all gap/padding values. Values that are not multiples of 4 (e.g. 13px, 27px, 5px) signal ad-hoc spacing.
- **Naming conventions**: Are layer names in consistent format (kebab-case, sentence case, or PascalCase)? Mixed conventions signal a file that multiple people edited without shared standards.
- **Detached components**: Any layer named identically to a known component but not an instance = detached. Flag each one.

### Flow context severity rules
- Inconsistency between screens in the same flow (e.g. primary button colour changes between step 1 and step 2): severity 3
- Inconsistency between this screen and a different flow: severity 2
- Icon style inconsistency within a single screen: severity 2
- Complete absence of a component library on a multi-screen file: severity 2

### Severity escalation triggers
- Primary CTA styled differently on two consecutive screens in the same flow: severity 3
- Navigation component changes style between screens in the same flow: severity 3
- Spacing values that are not on any recognisable scale: severity 2

---

## H5 — Error Prevention

**Definition:** Designs prevent problems from occurring through careful design before the user encounters an error.

### Visual signals (screenshot)
- **Inline validation hints**: Do input fields show character limits, accepted formats, or required indicators before the user types? Look for helper text below fields, placeholder text showing expected format (e.g. "DD/MM/YYYY"), or asterisk required indicators.
- **Disabled CTAs**: On forms, is the primary action button visually disabled until required fields are complete?
- **Confirmation dialogs**: For destructive or irreversible actions, is a confirmation modal visible or implied (e.g. "Are you sure?" pattern)?
- **Input affordances**: Are read-only fields visually distinct from editable fields (greyed background, no underline)?

### Structural signals (node tree)
- **Component variant states**: Search component property keys for "error", "disabled", "required", "warning". Absence of an error state on any form input = finding.
- **Confirmation overlay layers**: Look for modal/dialog frames triggered by destructive buttons (delete, send, publish).
- **Disabled button variants**: Does the primary CTA component have a "disabled" variant? If no disabled variant is designed, the form likely allows submission before completion.
- **Input constraint text**: Text nodes containing "required", "max", "minimum", character count, or format instructions adjacent to input fields.

### Flow context severity rules
- **No error state** on a payment or irreversible submission form: severity 4
- **No error state** on a standard profile/settings form: severity 3
- **No confirmation dialog** before delete action: severity 4
- **No disabled state** on a form submit button: severity 2

### Severity escalation triggers
- No error variant designed for any form field: severity 3
- Destructive action (delete, send payment, publish) with no confirmation: severity 4
- Required fields not marked as required: severity 2

---

## H6 — Recognition Rather Than Recall

**Definition:** Minimise memory load by making objects, actions, and options visible. Users should not have to remember information from one part of the interface to another.

### Visual signals (screenshot)
- **Icon-only navigation**: Are any navigational icons displayed without a text label? Icon-only bottom bars and toolbars force recall of icon meaning.
- **Location cues**: Does the screen show where the user is in the app hierarchy? (breadcrumb, highlighted nav item, screen title, step indicator)
- **Visible options**: Are primary actions surfaced, or are they buried in overflow menus, hamburger menus, or context menus?
- **Summary of previous choices**: In multi-step flows, does the screen show a summary of what the user selected in previous steps?
- **Contextual help text**: Are field instructions visible when the field is focused, or only in placeholder text that disappears when the user types?

### Structural signals (node tree)
- **Icon + label pairing**: Check each icon layer — does it have a sibling text node within the same parent group? Standalone icon layers with no adjacent text = finding.
- **Breadcrumb/stepper layers**: Look for layer names containing "breadcrumb", "stepper", "step-indicator", "progress-steps".
- **Navigation active state**: Does the navigation component have a selected/active variant applied to the current screen's item?
- **Overflow menus**: Look for layers named "overflow", "more-actions", "kebab", "ellipsis", "context-menu". Count the number of primary actions hidden behind these — more than 3 = finding.
- **Summary/review text**: In screens named "review", "confirm", "summary" — do text nodes repeat back key selections from earlier steps?

### Flow context severity rules
- **Icon-only navigation** in an app with 4+ nav destinations: severity 3
- **Icon-only navigation** for a single-purpose tool with 2 nav items: severity 1
- **No location indicator** on screen 3+ of a multi-step flow: severity 3
- **No location indicator** on a root/home screen: severity 0
- **Summary missing** on a final review/confirmation step: severity 3

### Severity escalation triggers
- 4+ nav items with no labels: severity 3
- No current location indicator on a flow 3 screens deep: severity 3
- Key user selection from step N not visible on step N+1 where it's needed: severity 3

---

## H7 — Flexibility and Efficiency of Use

**Definition:** The design caters to both novice and expert users. Accelerators help experts complete tasks faster.

### Visual signals (screenshot)
- **Search availability**: For any list or catalogue screen, is a search bar visible at the top level?
- **Filter/sort controls**: Are filter and sort controls accessible without entering a sub-screen?
- **Quick action buttons**: Is there a floating action button (FAB), swipe-to-reveal action, or quick-action chip for the most common task?
- **Pre-filled defaults**: Do form fields show sensible pre-filled values (country from locale, last-used option, saved address)?
- **Keyboard shortcuts/hints**: In desktop contexts, are shortcut hints visible on hover states or in menus?

### Structural signals (node tree)
- Layer names: "search-bar", "filter", "sort", "fab", "quick-action", "shortcut", "bulk-select", "select-all"
- Presence of a "recent", "suggested", or "saved" content layer (autocomplete patterns)
- Presence of a "select-all" or "bulk-action" control in list screens
- In forms: presence of pre-populated input states vs empty defaults only

### Flow context severity rules
- **No search** on a list/catalogue screen with implied content of 20+ items: severity 2
- **No filter controls** on a catalogue screen that is part of a browse journey: severity 2
- **No FAB or quick action** on a high-frequency task screen (e.g. compose, add item): severity 2
- **No shortcuts** on a screen only reached after 3+ navigation steps (power user path): severity 1

### Severity escalation triggers
- No search on a confirmed list screen with 20+ items: severity 2
- Expert workflow requires the same 5+ step process as the novice path with no shortcut: severity 2

---

## H8 — Aesthetic and Minimalist Design

**Definition:** Interfaces contain no irrelevant or rarely needed information. Every extra element competes with relevant information.

### Visual signals (screenshot)
- **Focal point count**: Count distinct visual regions competing for attention simultaneously. More than 5–6 competing regions = finding.
- **Decorative elements**: Are there purely decorative illustrations, background patterns, or gradient shapes that add visual noise without communicating meaning? Assess their proportion relative to functional content.
- **Whitespace quality**: Does the layout have adequate breathing room, or is every pixel filled?
- **Typographic hierarchy**: Is the type scale clean and obvious? More than 3–4 visible type sizes = visual noise.
- **Colour palette**: Does the palette feel restrained (1–2 brand colours, neutral scale, semantic status colours), or busy with many competing tints?

### Structural signals (node tree)
- **Font size count**: More than 4 distinct `fontSize` values on a single screen = severity 2. More than 6 = severity 3.
- **Colour count**: More than 8 distinct fill colours = severity 2.
- **Layer count**: A very high total layer count relative to visible content elements may signal decorative clutter. Use judgement based on screen complexity.
- **Text density**: Count text nodes. A screen with 20+ text nodes of similar visual weight with no clear hierarchy = H8 finding.
- **Decorative layer names**: Look for layer names like "decoration", "background-shape", "illustration", "pattern" that are at the root level of the frame.

### Flow context severity rules
- Cluttered screen mid-flow (step 2+) where the user has a clear task to complete: severity 3
- Decorative illustration on an onboarding/empty state where it aids comprehension: severity 0
- Dense information on a reference/settings screen where density is expected: severity 1

### Severity escalation triggers
- Primary task screen with 6+ competing focal points: severity 3
- More than 6 font sizes on a single screen: severity 3
- Irrelevant persistent content that cannot be dismissed: severity 3

---

## H9 — Help Users Recognize, Diagnose, and Recover from Errors

**Definition:** Error messages are in plain language, precisely indicate the problem, and constructively suggest a solution.

### Visual signals (screenshot)
- **Error message presence and placement**: Are error messages placed inline, adjacent to the element that caused the error? A top-level error banner only (with no field-level message) is a weaker pattern.
- **Error state visibility**: Do error states use sufficient visual differentiation (red border, error icon, colour) to be noticed?
- **Recovery actions**: Are error states accompanied by actionable buttons ("Try again", "Go back", "Contact support")?
- **Empty state distinction**: Is the empty state clearly different from an error state? An empty list that looks like a failed load is confusing.

### Structural signals (node tree)
- Text node content: flag any messages containing "Error", "Invalid", "Failed", "Not found", "Something went wrong", "Try again", "Contact support"
- **Quality check on error text**: Error messages should: (a) avoid error codes, (b) identify the specific problem, (c) suggest a fix. Flag: "Error 422", "Request failed", "An error occurred."
- Layer names: "error-state", "error-message", "inline-error", "recovery-action", "retry-button", "empty-state"
- Forms: absence of any error state variant on input components = finding
- Error screens: absence of any CTA/action button on error state frames = severity 4

### Flow context severity rules
- Error screen at payment/submission step with no recovery action: severity 4
- Inline form error with no suggestion of how to fix it: severity 3
- Generic "Something went wrong" with no retry on a non-critical screen: severity 2

### Severity escalation triggers
- Error screen with no recovery action or CTA: severity 4
- System error code displayed with no plain-language explanation: severity 3
- Error message placed far from the field that caused it: severity 2

---

## H10 — Help and Documentation

**Definition:** Help and documentation are available, easy to find, focused on user tasks, and provide concrete steps.

### Visual signals (screenshot)
- **Contextual help icons**: Are there "?" icons, info (ℹ) icons, or tooltip triggers on complex form fields or non-obvious features?
- **Onboarding elements**: Are there coach marks, feature spotlights, or onboarding tooltips visible for new or first-time states?
- **Help entry point**: Is there a visible link to help/FAQ/support in the navigation or screen footer?
- **Empty state guidance**: Do empty states include instructional text explaining what the user should do?

### Structural signals (node tree)
- Layer names: "tooltip", "help-icon", "info-icon", "coach-mark", "onboarding", "hint", "helper-text", "empty-state", "help-link", "support-link"
- **Form helper text**: Text nodes adjacent to input fields that explain format, constraints, or purpose
- **Empty state content**: Text nodes in empty-state layers — do they contain action-oriented instructions ("Add your first item by tapping +") rather than passive messages ("No items yet")?
- **Help navigation**: Layer names in the navigation component containing "help", "support", "FAQ", "contact"

### Flow context severity rules
- **Complex non-standard feature** (e.g. custom date picker, multi-select tag input, file upload with constraints) with no contextual help: severity 2
- **Non-obvious form field** (e.g. VAT number, IBAN, promo code) with no helper text: severity 2
- **First-time user screen** with no onboarding guidance: severity 3
- **Routine screen** (e.g. standard login form) with no help: severity 0

### Severity escalation triggers
- No help access point in the navigation on any screen: severity 2
- First-time feature with no onboarding guidance: severity 3
- Empty state with passive text and no action: severity 2
