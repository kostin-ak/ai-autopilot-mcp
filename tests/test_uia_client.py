"""Deterministic tests for the bounded walk and its continuation token.

Live UIA cannot be a correctness gate: the desktop changes under it, and a
passing run says nothing about the next one. These tests drive the walk against a
fixed fake tree, so the paging contract -- every path exactly once, in order, no
duplicates, no gaps -- is checkable on every run.

The fake tree is deliberately unbalanced, because a balanced one hides off-by-one
errors in the token arithmetic:

    root
    ├── A (0)
    │   ├── A0 (0.0)
    │   │   ├── A00 (0.0.0)
    │   │   └── A01 (0.0.1)
    │   └── A1 (0.1)
    ├── B (1)
    │   └── B0 (1.0)
    └── C (2)
"""
from __future__ import annotations

import pytest

import uia_client as uc


# --------------------------------------------------------------------- fakes
class FakeArray:
    def __init__(self, elements):
        self._elements = elements

    @property
    def Length(self):
        return len(self._elements)

    def GetElement(self, index):
        return self._elements[index]


class FakeElement:
    """Stands in for IUIAutomationElement, cached-properties path only."""

    def __init__(self, name, children=(), control_type=50000, class_name="",
                 bounds=(0, 0, 10, 10), enabled=True, offscreen=False,
                 focusable=True, has_focus=False, automation_id=""):
        self.CachedName = name
        self.CachedControlType = control_type
        self.CachedClassName = class_name
        self.CachedAutomationId = automation_id
        self.CachedIsEnabled = enabled
        self.CachedIsOffscreen = offscreen
        self.CachedIsKeyboardFocusable = focusable
        self.CachedHasKeyboardFocus = has_focus
        self.CachedBoundingRectangle = _Rect(*bounds)
        self._children = list(children)

    def FindAllBuildCache(self, scope, cond, req):
        return FakeArray(self._children)

    def GetCachedPropertyValue(self, pid):
        return False


class _Rect:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class _FakeAuto:
    def CreateTrueCondition(self):
        return object()


class _FakeTypelib:
    TreeScope_Children = 2


class FakeClient(uc.UIAClient):
    """UIAClient with every COM touchpoint replaced."""

    def __init__(self, root, minimized=False):
        super().__init__()
        self.root = root
        self._minimized = minimized
        self.restore_calls = 0

    def _typelib(self):
        return _FakeTypelib()

    def _automation(self):
        return _FakeAuto()

    def _cache_request(self):
        return None

    def element_from_handle(self, hwnd):
        return self.root

    def control_type_names(self):
        return {50000: "Pane", 50004: "Edit", 50032: "Window"}

    def pattern_flags(self):
        return {}

    def is_minimized(self, hwnd):
        return self._minimized

    def ensure_restored(self, hwnd):
        self.restore_calls += 1
        self._minimized = False
        return True


def build_tree():
    a00 = FakeElement("A00", bounds=(0, 20, 10, 30))
    a01 = FakeElement("A01", bounds=(0, 30, 10, 40))
    a0 = FakeElement("A0", children=[a00, a01], bounds=(0, 10, 10, 40))
    a1 = FakeElement("A1", bounds=(0, 40, 10, 50))
    a = FakeElement("A", children=[a0, a1], bounds=(0, 10, 10, 50))
    b0 = FakeElement("B0", bounds=(20, 10, 30, 20))
    b = FakeElement("B", children=[b0], bounds=(20, 0, 30, 20))
    c = FakeElement("C", bounds=(40, 0, 50, 10))
    return FakeElement("root", children=[a, b, c], bounds=(0, 0, 100, 100))


EXPECTED_ORDER = ["0", "0.0", "0.0.0", "0.0.1", "0.1", "1", "1.0", "2"]


@pytest.fixture
def client():
    return FakeClient(build_tree())


# ------------------------------------------------------------- walk order
def test_preorder_paths_match_expected(client):
    snap = client.snapshot(1, max_nodes=100, time_budget_s=30.0)
    assert [n.path for n in snap.nodes] == EXPECTED_ORDER
    assert not snap.truncated
    assert snap.next_token is None
    assert snap.failed_queries == 0


def test_depth_is_recorded(client):
    snap = client.snapshot(1, max_nodes=100, time_budget_s=30.0)
    by_path = {n.path: n.depth for n in snap.nodes}
    assert by_path["0"] == 1
    assert by_path["0.0"] == 2
    assert by_path["0.0.0"] == 3


def test_node_carries_text_and_centre(client):
    snap = client.snapshot(1, max_nodes=100, time_budget_s=30.0)
    a00 = next(n for n in snap.nodes if n.path == "0.0.0")
    assert a00.text == "A00"
    assert a00.text_source == "name"
    assert a00.text_usable
    # bounds (0, 20, 10, 30) -> centre (5, 25)
    assert a00.clickable == (5, 25)
    assert a00.bounds == (0, 20, 10, 30)


# ------------------------------------------------------------ paging
@pytest.mark.parametrize("per_page", [1, 2, 3, 5, 7, 8, 20])
def test_paging_covers_every_path_exactly_once(client, per_page):
    """The core contract: pages concatenate to one clean pre-order walk."""
    emitted, token, rounds = [], None, 0
    while rounds < 100:
        rounds += 1
        snap = client.snapshot(1, max_nodes=per_page, time_budget_s=30.0, token=token)
        emitted.extend(n.path for n in snap.nodes)
        if not snap.next_token:
            break
        token = snap.next_token

    assert emitted == EXPECTED_ORDER, (
        f"per_page={per_page}: paging produced {emitted}"
    )
    assert len(set(emitted)) == len(emitted), "duplicate paths across pages"


def test_paging_never_spins_without_progress(client):
    """A token that does not advance the walk is worse than no token.

    The first implementation looped between two tokens forever, emitting the same
    node each round. Guard against that regression directly.
    """
    seen_tokens = []
    token, rounds = None, 0
    while rounds < 50:
        rounds += 1
        snap = client.snapshot(1, max_nodes=1, time_budget_s=30.0, token=token)
        if not snap.next_token:
            break
        token = snap.next_token
        assert token not in seen_tokens, f"token {token!r} repeated; paging loops"
        seen_tokens.append(token)
    assert rounds < 50


def test_token_semantics_is_next_node_not_last_emitted(client):
    snap = client.snapshot(1, max_nodes=2, time_budget_s=30.0)
    assert [n.path for n in snap.nodes] == ["0", "0.0"]
    # After emitting "0" and "0.0", the next unvisited node in pre-order is
    # A0's first child, not A0's sibling.
    assert snap.next_token == "0.0.0"
    assert snap.truncated
    assert snap.truncated_reason == "nodes"


def test_token_from_exhausted_frames_falls_back_to_parent(client):
    """Frames on top of the stack may be spent; the frontier is further down."""
    snap = client.snapshot(1, max_nodes=4, time_budget_s=30.0)
    assert [n.path for n in snap.nodes] == ["0", "0.0", "0.0.0", "0.0.1"]
    # A0 and A are both exhausted here, so the token comes from the root frame.
    assert snap.next_token == "0.1"


# ------------------------------------------------------------- truncation
def test_depth_truncation_is_terminal_and_says_why(client):
    snap = client.snapshot(1, max_depth=1, max_nodes=100, time_budget_s=30.0)
    assert snap.truncated
    assert snap.truncated_reason == "depth"
    assert snap.next_token is None, "depth truncation cannot be resumed by token"
    assert any("max_depth" in w for w in snap.warnings)


def test_leaf_at_depth_limit_is_not_truncation(client):
    """C is a leaf, so reaching the depth limit on it loses nothing."""
    root = FakeElement("root", children=[FakeElement("only-leaf")],
                       bounds=(0, 0, 10, 10))
    snap = FakeClient(root).snapshot(1, max_depth=1, max_nodes=100,
                                     time_budget_s=30.0)
    assert not snap.truncated
    assert [n.path for n in snap.nodes] == ["0"]


def test_unresolvable_token_restarts_with_a_warning(client):
    snap = client.snapshot(1, max_nodes=100, time_budget_s=30.0, token="9.9.9")
    assert [n.path for n in snap.nodes] == EXPECTED_ORDER
    assert any("не разрешён" in w for w in snap.warnings)


def test_malformed_token_restarts(client):
    snap = client.snapshot(1, max_nodes=100, time_budget_s=30.0, token="abc")
    assert [n.path for n in snap.nodes] == EXPECTED_ORDER


# ---------------------------------------------------------- text resolution
def test_qt_class_chain_is_rejected_as_text():
    name = "class MainWindow.class Ui::RpWidget.class MainWidget.class Chat"
    text, source, usable = uc.UIAClient._resolve_text(name, "MainWindow")
    assert usable is False, "a class chain must never count as content"
    assert text == "MainWindow", "falls back to ClassName rather than going blank"
    assert source == "class"
    assert "RpWidget" not in text


def test_qt_class_chain_keeps_class_name_instead():
    """Rejecting a garbage Name must not also discard a usable ClassName.

    The ClassName is a display hint only, so text_usable stays False: it is enough
    to tell elements apart, not enough to identify a recipient.
    """
    text, source, usable = uc.UIAClient._resolve_text(
        "class Foo.class Bar", "Qt51519QWindowIcon"
    )
    assert (text, source, usable) == ("Qt51519QWindowIcon", "class", False)


def test_normal_name_wins_over_class():
    assert uc.UIAClient._resolve_text("Сохранить", "QPushButton") == (
        "Сохранить", "name", True)


def test_no_text_at_all():
    assert uc.UIAClient._resolve_text("", "") == ("", "none", False)


@pytest.mark.parametrize("text,expected", [
    ("class MainWindow.class Ui::RpWidget.class MainWidget", True),
    ("class Foo.class Bar", True),
    ("class", False),
    ("Классификация документов", False),
    ("class of 2024", False),
    ("", False),
])
def test_class_chain_detector(text, expected):
    assert uc._looks_like_class_chain(text) is expected


# --------------------------------------------------------------- rendering
def test_format_reports_incompleteness(client):
    snap = client.snapshot(1, max_nodes=2, time_budget_s=30.0)
    text = client.format_snapshot(snap)
    assert "СНИМОК НЕПОЛОН" in text
    assert "исчерпан потолок узлов" in text
    assert f"token={snap.next_token}" in text


def test_format_reports_complete_snapshot(client):
    snap = client.snapshot(1, max_nodes=100, time_budget_s=30.0)
    text = client.format_snapshot(snap)
    assert "СНИМОК НЕПОЛОН" not in text
    assert "A00" in text


def test_format_marks_unusable_text(client):
    root = FakeElement("root", children=[
        FakeElement("class A.class B.class C", class_name="QWidget")
    ], bounds=(0, 0, 10, 10))
    snap = FakeClient(root).snapshot(1, max_nodes=10, time_budget_s=30.0)
    assert snap.nodes[0].text_usable is False
    assert "QWidget" in client.format_snapshot(snap)
    assert "class A.class B" not in client.format_snapshot(snap)


def test_centre_from_bounds():
    assert uc.UIAClient._centre((0, 0, 100, 200)) == (50, 100)
    assert uc.UIAClient._centre((10, 20, 11, 21)) == (10, 20)


def test_degenerate_bounds_give_no_clickable_point():
    root = FakeElement("root", children=[
        FakeElement("flat", bounds=(5, 5, 5, 5))
    ], bounds=(0, 0, 10, 10))
    snap = FakeClient(root).snapshot(1, max_nodes=10, time_budget_s=30.0)
    assert snap.nodes[0].clickable is None
    assert snap.nodes[0].bounds == (5, 5, 5, 5)


# ------------------------------------------------- minimized-window sentinel
def test_minimized_sentinel_coordinates_are_not_clickable():
    """A minimized window reports roughly (-32000, -32000), offset by its border.

    Those coordinates lie on no monitor, so publishing them as a click target
    sends the agent aiming at a point that does not exist on any screen.
    """
    leaf = FakeElement("x", bounds=(-31981, -31981, -31971, -31971))
    root = FakeElement("root", children=[leaf], bounds=(-31981, -31981, 10, 10))
    snap = FakeClient(root, minimized=True).snapshot(1, max_nodes=10,
                                                     time_budget_s=30.0)
    assert snap.nodes[0].clickable is None
    assert snap.nodes[0].bounds == (-31981, -31981, -31971, -31971)


def test_real_negative_monitor_coordinates_are_kept():
    """A secondary display to the left of the primary legitimately has x < 0."""
    leaf = FakeElement("x", bounds=(-1920, 351, -1800, 400))
    root = FakeElement("root", children=[leaf], bounds=(-1920, 0, 100, 100))
    snap = FakeClient(root).snapshot(1, max_nodes=10, time_budget_s=30.0)
    assert snap.nodes[0].clickable == (-1860, 375)


@pytest.mark.parametrize("x,y,expected", [
    (-31981, -31981, True),
    (-32000, 100, True),
    (100, -32000, True),
    (-1920, 351, False),
    (0, 0, False),
    (-16001, 0, True),
    (-15999, 0, False),
])
def test_sentinel_detector(x, y, expected):
    assert uc._is_sentinel(x, y) is expected


def test_minimized_window_warns_and_is_flagged():
    """The tree is still readable when minimized; the coordinates are not."""
    snap = FakeClient(build_tree(), minimized=True).snapshot(
        1, max_nodes=100, time_budget_s=30.0
    )
    assert snap.minimized
    assert not snap.auto_restored
    assert len(snap.nodes) == len(EXPECTED_ORDER), "tree still readable"
    assert any("свёрнуто" in w for w in snap.warnings)


def test_auto_restore_is_opt_in_and_reported():
    c = FakeClient(build_tree(), minimized=True)
    snap = c.snapshot(1, max_nodes=100, time_budget_s=30.0, auto_restore=True)
    assert c.restore_calls == 1, "restoring changes the desktop, so it is opt-in"
    assert snap.auto_restored
    assert not snap.minimized
    assert not any("свёрнуто" in w for w in snap.warnings)


def test_not_minimized_never_restores():
    c = FakeClient(build_tree(), minimized=False)
    snap = c.snapshot(1, max_nodes=100, time_budget_s=30.0, auto_restore=True)
    assert c.restore_calls == 0
    assert not snap.auto_restored


# ------------------------------------------- single-segment Qt class chains
@pytest.mark.parametrize("text,expected", [
    ("class Ui::RpWidget", True),
    ("class Ui::Platform::TitleWidget", True),
    ("class A.class B", True),
    ("class MainWindow.class Ui::RpWidget.class MainWidget", True),
    ("class 2024 schedule", False),
    ("classification of documents", False),
    ("Классификация", False),
])
def test_class_chain_detector_single_segment(text, expected):
    assert uc._looks_like_class_chain(text) is expected


def test_single_segment_class_chain_is_not_shown_as_a_label():
    """The multi-segment detector missed this form on a live Qt window."""
    text, source, usable = uc.UIAClient._resolve_text(
        "class Ui::RpWidget", "Group"
    )
    assert usable is False
    assert text == "Group"
    assert source == "class"


def test_qt_writes_the_class_chain_into_classname_too():
    """Observed live: both Name and ClassName read "class Ui::RpWidget".

    Trusting the fallback therefore reproduces the same garbage. Rejecting both
    leaves the control type as the only honest description of an unnamed
    container.
    """
    text, source, usable = uc.UIAClient._resolve_text(
        "class Ui::RpWidget", "class Ui::RpWidget"
    )
    assert (text, source, usable) == ("", "none", False)


def test_class_chain_in_classname_only_is_rejected():
    assert uc.UIAClient._resolve_text("", "class Ui::PlainShadow") == (
        "", "none", False
    )
