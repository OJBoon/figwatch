"""Tests for figwatch.providers.figma — node data extraction."""


import os

import figwatch.providers.figma as fig
from figwatch.providers.figma import (
    _extract_annotations,
    _extract_prototype_flows,
    enumerate_child_screens,
    extract_text_from_node,
    fetch_screenshots_batch,
    is_journey_frame,
    order_screens,
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


# ── Journey enumeration / ordering / batch capture ────────────────────

def _screen(cid, name="S", x=0, y=0, w=400, h=800, type="FRAME", visible=True):
    n = {"id": cid, "name": name, "type": type,
         "absoluteBoundingBox": {"x": x, "y": y, "width": w, "height": h}}
    if not visible:
        n["visible"] = False
    return n


def test_enumerate_child_screens_keeps_screen_frames():
    tree = {"children": [
        _screen("1:1", "A", x=0), _screen("1:2", "B", x=500),
        {"id": "1:3", "type": "TEXT", "absoluteBoundingBox": {"width": 400, "height": 40}},
    ]}
    assert [s["id"] for s in enumerate_child_screens(tree)] == ["1:1", "1:2"]


def test_enumerate_skips_small_and_hidden():
    tree = {"children": [
        _screen("1:1", "A"),
        _screen("1:2", "tiny", w=50, h=50),
        _screen("1:3", "hidden", visible=False),
    ]}
    assert [s["id"] for s in enumerate_child_screens(tree)] == ["1:1"]


def test_is_journey_frame_threshold():
    assert is_journey_frame({"children": [_screen("1"), _screen("2"), _screen("3")]}) is True
    assert is_journey_frame({"children": [_screen("1"), _screen("2")]}) is False


def test_order_screens_reading_order():
    screens = enumerate_child_screens({"children": [
        _screen("a", x=0, y=0), _screen("b", x=500, y=0), _screen("c", x=0, y=1000),
    ]})
    assert [s["id"] for s in order_screens(screens)] == ["a", "b", "c"]


def test_order_screens_prototype_flow():
    tree = {"name": "J", "type": "FRAME", "flowStartingPoints": [{"nodeId": "a"}],
            "children": [
                dict(_screen("a", x=0), reactions=[{"action": {"destinationId": "c"}}]),
                _screen("b", x=500),
                dict(_screen("c", x=1000), reactions=[{"action": {"destinationId": "b"}}]),
            ]}
    screens = enumerate_child_screens(tree)
    assert [s["id"] for s in order_screens(screens, tree)] == ["a", "c", "b"]


def test_fetch_screenshots_batch_maps_ids_and_skips_null(monkeypatch, tmp_path):
    monkeypatch.setattr(fig.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(fig, "figma_get_retry", lambda *a, **k: {
        "images": {"1:1": "http://x/1.png", "1:2": None, "1:3": "http://x/3.png"}})

    class _Resp:
        def __init__(self, data): self._d = data
        def read(self): return self._d
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(fig.urllib.request, "urlopen", lambda url, **k: _Resp(b"img-bytes"))
    out = fetch_screenshots_batch("key", ["1:1", "1:2", "1:3"], "pat")
    assert set(out) == {"1:1", "1:3"}  # 1:2 had a null URL
    assert all(os.path.exists(p) for p in out.values())


def test_fetch_screenshots_batch_skips_oversized(monkeypatch, tmp_path):
    monkeypatch.setattr(fig.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(fig, "figma_get_retry", lambda *a, **k: {
        "images": {"1:1": "http://x/ok.png", "1:2": "http://x/big.png"}})

    class _Resp:
        def __init__(self, data): self._d = data
        def read(self): return self._d
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _open(url, **k):
        return _Resp(b"x" if "ok" in url else b"y" * (fig._MAX_IMAGE_BYTES + 1))
    monkeypatch.setattr(fig.urllib.request, "urlopen", _open)
    out = fetch_screenshots_batch("key", ["1:1", "1:2"], "pat")
    assert set(out) == {"1:1"}  # oversized 1:2 dropped


def test_fetch_screenshots_batch_empty_ids():
    assert fetch_screenshots_batch("key", [], "pat") == {}
