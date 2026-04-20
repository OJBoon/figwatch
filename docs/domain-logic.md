# Domain Logic

How a Figma comment becomes an AI audit reply.

## Audit Production

A comment containing a trigger keyword (e.g. `@ux`, `@tone`) is matched to a **skill** — a markdown rubric that tells the AI what to evaluate and how. The skill declares what design data it needs, that data is fetched from Figma, and everything is assembled into a prompt for the AI provider.

```mermaid
flowchart TD
    COMMENT["💬 Figma Comment
    ───────────────────
    '@ux check the spacing
    on this card layout'"]

    COMMENT --> MATCH["Match trigger keyword"]
    MATCH --> SKILL

    SKILL["📋 Skill
    ───────────────────
    Evaluation rubric
    written in markdown

    e.g. builtin:ux uses
    Nielsen's 10 Heuristics"]

    SKILL --> INTROSPECT["Determine required data"]

    INTROSPECT --> DATA

    subgraph Design Data from Figma
        DATA["What the skill asked for"]

        SCREENSHOT["🖼️ Screenshot
        Visual capture of the frame"]

        NODE_TREE["🌳 Node Tree
        Full layer hierarchy as JSON —
        frame structure, auto-layout,
        constraints, component instances"]

        TEXT_NODES["📝 Text Nodes
        Every text layer extracted
        with its content and name"]

        STYLES["🎨 Styles
        Colour, typography, and
        effect styles in the file"]

        COMPONENTS["🧩 Components
        Component definitions
        used in the file"]

        VARIABLES["📐 Variables
        Design tokens — spacing,
        colour, and sizing values"]

        ANNOTATIONS["📌 Annotations
        Designer notes attached
        to specific nodes"]

        PROTOTYPES["🔗 Prototype Flows
        Screen-to-screen navigation
        connections and interactions"]

        DEV_RESOURCES["🔧 Dev Resources
        Links and assets attached
        to nodes for developers"]

        FILE_STRUCTURE["📂 File Structure
        Top-level pages and
        frame organisation"]

        DATA --- SCREENSHOT
        DATA --- NODE_TREE
        DATA --- TEXT_NODES
        DATA --- STYLES
        DATA --- COMPONENTS
        DATA --- VARIABLES
        DATA --- ANNOTATIONS
        DATA --- PROTOTYPES
        DATA --- DEV_RESOURCES
        DATA --- FILE_STRUCTURE
    end

    SCREENSHOT --> PROMPT
    NODE_TREE --> PROMPT
    TEXT_NODES --> PROMPT
    STYLES --> PROMPT
    COMPONENTS --> PROMPT
    VARIABLES --> PROMPT
    ANNOTATIONS --> PROMPT
    PROTOTYPES --> PROMPT
    DEV_RESOURCES --> PROMPT
    FILE_STRUCTURE --> PROMPT
    SKILL --> PROMPT

    PROMPT["Assemble Prompt
    ───────────────────
    Skill rubric
    + reference documents
    + reviewer's extra context
    + selected design data"]

    PROMPT --> AI["AI Provider evaluates
    against the skill rubric"]

    AI --> REPLY

    REPLY["💬 Audit Reply
    ───────────────────
    Plain-text evaluation
    posted as a Figma
    comment reply"]
```

### Builtin skills

| Skill | Trigger | What it evaluates | Data it needs |
|-------|---------|-------------------|---------------|
| **UX Heuristic Review** | `@ux` | Nielsen's 10 Usability Heuristics — cross-references the visual screenshot against the structural node tree | Screenshot, Node Tree |
| **Tone of Voice Review** | `@tone` | Copy against locale-specific brand guidelines (DE, FR, NL, Benelux) with reference docs per locale | Node Tree, Text Nodes |

Custom skills can be added as markdown files. Each skill is introspected to determine which design data types it requires — only the data it asks for is fetched.
