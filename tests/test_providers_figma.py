"""Tests for figwatch.providers.figma — node data extraction."""


from figwatch.providers.figma import (
    _extract_annotations,
    _extract_prototype_flows,
    extract_text_from_node,
)

# ── extract_text_from_node ────────────────────────────────────────────

def _text_node(name, text, node_id="1:1", x=0, y=0, w=100, h=20, visible=True):
    node = {
        "type": "TEXT",
        "name": name,
        "characters": text,
        "id": node_id,
        "absoluteBoundingBox": {"x": x, "y": y, "width": w, "height": h},
    }
    if not visible:
        node["visible"] = False
    return node


def test_extract_text_basic():
    node = {"type": "FRAME", "children": [_text_node("Title", "Hello")]}
    texts = extract_text_from_node(node)
    assert len(texts) == 1
    assert texts[0]["text"] == "Hello"
    assert texts[0]["name"] == "Title"


def test_extract_text_skips_whitespace_only():
    node = {"type": "FRAME", "children": [_text_node("Empty", "   ")]}
    assert extract_text_from_node(node) == []


def test_extract_text_skips_invisible_nodes():
    node = {"type": "FRAME", "children": [_text_node("Hidden", "Secret", visible=False)]}
    assert extract_text_from_node(node) == []


def test_extract_text_nested():
    node = {
        "type": "FRAME",
        "children": [
            {
                "type": "GROUP",
                "children": [_text_node("Nested", "Deep text", node_id="2:1")],
            }
        ],
    }
    texts = extract_text_from_node(node)
    assert len(texts) == 1
    assert texts[0]["text"] == "Deep text"


def test_extract_text_bounding_box():
    node = {"type": "FRAME", "children": [_text_node("T", "Hi", x=10, y=20, w=50, h=15)]}
    t = extract_text_from_node(node)[0]
    assert t["x"] == 10
    assert t["y"] == 20
    assert t["w"] == 50
    assert t["h"] == 15


def test_extract_text_multiple_nodes():
    node = {
        "type": "FRAME",
        "children": [
            _text_node("A", "First", node_id="1:1"),
            _text_node("B", "Second", node_id="1:2"),
        ],
    }
    texts = extract_text_from_node(node)
    assert len(texts) == 2
    assert {t["text"] for t in texts} == {"First", "Second"}


# ── _extract_prototype_flows ──────────────────────────────────────────

def test_extract_prototype_flows_basic():
    node = {
        "id": "1:1",
        "name": "Button",
        "reactions": [
            {
                "trigger": {"type": "ON_CLICK"},
                "action": {"type": "NODE", "destinationId": "2:1"},
            }
        ],
        "children": [],
    }
    flows = _extract_prototype_flows(node)
    assert len(flows) == 1
    assert flows[0]["trigger"] == "ON_CLICK"
    assert flows[0]["destination"] == "2:1"
    assert flows[0]["node_name"] == "Button"


def test_extract_prototype_flows_empty():
    node = {"id": "1:1", "name": "Frame", "reactions": [], "children": []}
    assert _extract_prototype_flows(node) == []


def test_extract_prototype_flows_nested():
    node = {
        "id": "1:1", "name": "Frame", "reactions": [],
        "children": [
            {
                "id": "1:2", "name": "Child",
                "reactions": [
                    {
                        "trigger": {"type": "ON_HOVER"},
                        "action": {"type": "NODE", "destinationId": "3:1"},
                    },
                ],
                "children": [],
            }
        ],
    }
    flows = _extract_prototype_flows(node)
    assert len(flows) == 1
    assert flows[0]["trigger"] == "ON_HOVER"


# ── _extract_annotations ─────────────────────────────────────────────

def test_extract_annotations_by_name():
    node = {
        "id": "1:1", "name": "Annotation 1", "type": "TEXT",
        "characters": "This is a note",
        "children": [],
    }
    annotations = _extract_annotations(node)
    assert len(annotations) == 1
    assert annotations[0]["characters"] == "This is a note"


def test_extract_annotations_note_in_name():
    node = {
        "id": "1:1", "name": "Design note", "type": "TEXT",
        "characters": "Important",
        "children": [],
    }
    annotations = _extract_annotations(node)
    assert len(annotations) == 1


def test_extract_annotations_skips_unrelated():
    node = {
        "id": "1:1", "name": "Button", "type": "COMPONENT",
        "characters": "",
        "children": [],
    }
    assert _extract_annotations(node) == []


def test_extract_annotations_nested():
    node = {
        "id": "1:0", "name": "Frame", "type": "FRAME", "characters": "",
        "children": [
            {"id": "1:1", "name": "Annotation tooltip", "type": "TEXT",
             "characters": "Tooltip text", "children": []},
        ],
    }
    annotations = _extract_annotations(node)
    assert any(a["characters"] == "Tooltip text" for a in annotations)
