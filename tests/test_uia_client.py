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

import time

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
                 focusable=True, has_focus=False, automation_id="",
                 delay=0.0, pid=4242, pattern_objs=None):
        self.CachedName = name
        self.CachedControlType = control_type
        self.CachedClassName = class_name
        self.CachedAutomationId = automation_id
        self.CachedIsEnabled = enabled
        self.CachedIsOffscreen = offscreen
        self.CachedIsKeyboardFocusable = focusable
        self.CachedHasKeyboardFocus = has_focus
        self.CachedBoundingRectangle = _Rect(*bounds)
        self.CurrentName = name
        self.CurrentClassName = class_name
        self.CurrentProcessId = pid
        self.CurrentBoundingRectangle = _Rect(*bounds)
        self.CurrentIsOffscreen = offscreen
        self._children = list(children)
        self._delay = delay
        self._patterns = dict(pattern_objs or {})

    def FindAllBuildCache(self, scope, cond, req):
        if self._delay:
            time.sleep(self._delay)
        return FakeArray(self._children)

    def GetCachedPropertyValue(self, pid):
        return False

    def GetCurrentPattern(self, pid):
        return _FakeUnknown(self._patterns.get(pid))


class _FakeUnknown:
    """Stands in for the IUnknown that GetCurrentPattern returns."""

    def __init__(self, pattern):
        self._pattern = pattern

    def QueryInterface(self, iface):
        if self._pattern is None:
            raise uc.COMError(-2147024809, "pattern not supported", None)
        return self._pattern


class FakeValuePattern:
    def __init__(self, value="", ignore_writes=False):
        self.CurrentValue = value
        self._ignore = ignore_writes

    def SetValue(self, text):
        # ignore_writes models the terminal trap: the call succeeds, IsReadOnly
        # reports 0, and the value does not change.
        if not self._ignore:
            self.CurrentValue = text


class FakeTogglePattern:
    def __init__(self, state=0, ignore_calls=False):
        self.CurrentToggleState = state
        self._ignore = ignore_calls

    def Toggle(self):
        if not self._ignore:
            self.CurrentToggleState = 1 - self.CurrentToggleState


class FakeSelectionItemPattern:
    def __init__(self):
        self.CurrentIsSelected = False

    def Select(self):
        self.CurrentIsSelected = True


class FakeExpandCollapsePattern:
    def __init__(self, state=0):
        self.CurrentExpandCollapseState = state
        self.calls = []

    def Expand(self):
        self.calls.append("expand")
        self.CurrentExpandCollapseState = 1

    def Collapse(self):
        self.calls.append("collapse")
        self.CurrentExpandCollapseState = 0


class FakeInvokePattern:
    def __init__(self):
        self.calls = 0

    def Invoke(self):
        self.calls += 1


PATTERN_IDS = {
    "Invoke": 10000, "Value": 10002, "Toggle": 10015,
    "SelectionItem": 10010, "ExpandCollapse": 10005,
}


class _Rect:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class _FakeAuto:
    def CreateTrueCondition(self):
        return object()

    def PollForPotentialSupportedPatterns(self, el):
        return ([], [])


class _FakeTypelib:
    TreeScope_Children = 2
    IUIAutomationInvokePattern = "IID_INVOKE"
    IUIAutomationValuePattern = "IID_VALUE"
    IUIAutomationTogglePattern = "IID_TOGGLE"
    IUIAutomationSelectionItemPattern = "IID_SELITEM"
    IUIAutomationExpandCollapsePattern = "IID_EXPCOLL"
    IUIAutomationWindowPattern = "IID_WINDOW"


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
        # 50000 stays mapped to a non-actionable role on purpose: the pre-existing
        # fixtures build trees out of it, and reclassifying them as Button would
        # change what the fill calculation sees.
        return {50000: "Pane", 50004: "Edit", 50005: "Hyperlink",
                50007: "ListItem", 50019: "TabItem", 50026: "Group",
                50032: "Window"}

    def pattern_flags(self):
        return {}

    def patterns(self):
        return PATTERN_IDS

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


# ------------------------------------------------ deep tokens and slow queries
def build_deep_chain(depth, delay=0.0):
    """A chain of `depth` nodes, each with one child and one leaf sibling."""
    node = FakeElement("leaf", delay=delay)
    for i in range(depth):
        node = FakeElement(f"n{i}", children=[node, FakeElement(f"sib{i}", delay=delay)],
                           delay=delay)
    return FakeElement("root", children=[node], delay=delay)


def test_budget_starts_after_the_rebuild():
    """A deep token costs one query per segment to rebuild.

    Counting that against the walk budget makes the page emit nothing and return
    the same token -- a livelock. Observed live with a 15-segment token and the
    150 ms budget: zero nodes, 203 ms, identical token.
    """
    root = build_deep_chain(6, delay=0.004)
    client = FakeClient(root)
    deep_token = "0" + ".0" * 5 + ".1"

    snap = client.snapshot(1, max_nodes=5, time_budget_s=0.010, token=deep_token)
    assert snap.nodes, "page emitted nothing: the rebuild consumed the whole budget"


def test_elapsed_includes_navigation_cost():
    """elapsed_ms is wall clock, so a slow rebuild is not hidden from the caller."""
    root = build_deep_chain(4, delay=0.010)
    client = FakeClient(root)
    snap = client.snapshot(1, max_nodes=2, time_budget_s=5.0, token="0.0.0.0")
    assert snap.elapsed_ms >= 40, f"expected >=40 ms, got {snap.elapsed_ms:.0f}"


def test_deep_paging_still_makes_progress(client):
    """Paging down a deep path must advance rather than re-emit."""
    emitted, token, rounds = [], None, 0
    while rounds < 20:
        rounds += 1
        snap = client.snapshot(1, max_nodes=1, max_depth=30,
                               time_budget_s=30.0, token=token)
        emitted.extend(n.path for n in snap.nodes)
        if not snap.next_token:
            break
        assert snap.next_token not in emitted, "token names an already-emitted node"
        token = snap.next_token
    assert emitted == EXPECTED_ORDER


# ------------------------------------------------------------- classification
def build_narrow_deep(top=3, second=10, delay=0.0):
    """Narrow at depth one, wide at depth two: the Chromium/Electron shape."""
    tops = []
    for i in range(top):
        tops.append(FakeElement(
            f"view{i}",
            children=[FakeElement(f"c{i}_{j}", delay=delay) for j in range(second)],
            delay=delay,
        ))
    return FakeElement("root", children=tops, bounds=(0, 0, 1280, 800))


def build_firefox_shape():
    """Unnamed structural chrome at depth one, well-named actionable nodes below.

    This is the live Firefox shape: depth one filled 11% of names while Hyperlink
    sat at 97%, Button at 99%, ListItem at 99% and TabItem at 100%. Structural
    roles were the drag -- Group at 5%, Text at 20%.
    """
    chrome = [FakeElement("", control_type=50026) for _ in range(8)]
    for i in range(6):
        inner = [FakeElement(f"Link {i}-{j}", control_type=50005) for j in range(8)]
        chrome.append(FakeElement("", control_type=50026, children=inner))
    return FakeElement("root", children=chrome, bounds=(0, 0, 1280, 800))


def test_firefox_shape_is_rich_not_partial():
    """Depth-one classification called Firefox 'partial' at 11% name fill although
    its links, tabs and buttons were all drivable through patterns."""
    probe = FakeClient(build_firefox_shape()).probe_target(1)
    assert probe.child_count == 14, "all wrappers, none named at depth one"
    assert probe.classification == "rich", probe.classification
    assert any("действующих" in n for n in probe.notes)


def snap_node(role, usable):
    """A NodeSnapshot with just the fields the fill calculation reads."""
    return uc.NodeSnapshot(
        depth=1, role=role, text="t" if usable else "",
        text_source="name" if usable else "none",
        automation_id="", class_name="", bounds=None, clickable=None,
        enabled=True, offscreen=False, focusable=True, has_focus=False,
        patterns=(), path="0", text_usable=usable,
    )


def test_actionable_name_fill_ignores_structural_roles():
    """Structural roles are legitimately unnamed: 50 unnamed Groups plus 5 named
    Hyperlinks is a fully controllable tree, not a 9% one."""
    nodes = ([snap_node("Group", False) for _ in range(50)]
             + [snap_node("Hyperlink", True) for _ in range(5)])
    assert uc.UIAClient._actionable_name_fill(nodes) == 1.0


def test_actionable_name_fill_reports_unnamed_controls():
    nodes = [snap_node("Button", False) for _ in range(10)]
    assert uc.UIAClient._actionable_name_fill(nodes) == 0.0


def test_actionable_name_fill_is_none_without_actionable_roles():
    assert uc.UIAClient._actionable_name_fill([snap_node("Group", False)]) is None


def test_probe_cost_reflects_query_time():
    """The probe once reported 7.2 us/node for a Firefox window whose real walk
    cost 506 us/node, because it timed cached property reads rather than the
    per-node query. Sizing max_nodes from that is off by two orders of magnitude."""
    root = build_narrow_deep(delay=0.002)
    probe = FakeClient(root).probe_target(1)
    assert probe.us_per_node is not None
    assert probe.us_per_node > 1000, (
        f"{probe.us_per_node:.0f} us/node is implausible for a 2 ms/query tree"
    )


def test_narrow_but_deep_tree_is_not_opaque():
    """A live Electron window reported 2 children at depth one and had 184 usable
    nodes when walked, yet fan-out alone classified it as opaque and recommended
    the pixel path -- steering the agent away from a perfectly good tree.

    One extra level was not enough either: Chromium puts a view chain four to six
    levels deep above the real content, so the sample has to walk rather than peek.
    """
    probe = FakeClient(build_narrow_deep()).probe_target(1)
    assert probe.child_count == 3
    assert probe.tree_sample >= 20
    assert probe.classification == "rich", probe.classification
    assert any("Chromium" in n for n in probe.notes)


def build_deep_wide(depth=5, breadth=4):
    """Narrow at the top for several levels, wide only below them.

    This is the shape that defeats a one-level-deeper opacity test, and roughly
    what Chromium's view hierarchy looks like.
    """
    node = FakeElement("leaf")
    for i in range(depth):
        kids = [FakeElement(f"s{i}_{j}") for j in range(breadth - 1)] + [node]
        node = FakeElement(f"n{i}", children=kids)
    return FakeElement("root", children=[node], bounds=(0, 0, 1280, 800))


def test_deep_narrow_chain_is_not_misread_as_opaque():
    """A tree that stays narrow for several levels before widening."""
    probe = FakeClient(build_deep_wide()).probe_target(1)
    assert probe.child_count == 1
    assert probe.tree_sample >= 20
    assert probe.classification != "opaque"


def test_large_window_with_nothing_usable_is_opaque():
    """The Explorer and Java AWT shape: a big window reporting only chrome."""
    kids = [FakeElement("", class_name="Pane") for _ in range(7)]
    root = FakeElement("root", children=kids, bounds=(0, 0, 2560, 1440))
    probe = FakeClient(root).probe_target(1)
    assert probe.area > uc.OPAQUE_AREA_THRESHOLD
    assert probe.useful < uc.OPAQUE_USEFUL_THRESHOLD
    assert probe.classification == "opaque"


def test_small_dialog_with_few_but_usable_nodes_is_not_opaque():
    """What broke a plain node-count threshold: ten nodes, all actionable.

    A live Qt dialog of this shape was classified opaque and sent to the pixel
    path, and its checkbox then toggled perfectly through TogglePattern. Node count
    says nothing without window size, which is the qualifier ADR-0008 actually had.
    """
    kids = [FakeElement("Apply"), FakeElement("Cancel"), FakeElement("Enable")]
    root = FakeElement("dialog", children=kids, bounds=(0, 0, 420, 220))
    probe = FakeClient(root).probe_target(1)
    assert probe.area < uc.OPAQUE_AREA_THRESHOLD
    assert probe.classification != "opaque"


def test_shallow_tree_is_opaque():
    root = FakeElement("root", children=[FakeElement("a"), FakeElement("b")],
                       bounds=(0, 0, 800, 600))
    probe = FakeClient(root).probe_target(1)
    assert probe.child_count == 2
    assert probe.tree_sample < 20
    assert probe.classification == "opaque"


def test_empty_tree_is_opaque():
    probe = FakeClient(FakeElement("root", children=[],
                                   bounds=(0, 0, 800, 600))).probe_target(1)
    assert probe.child_count == 0
    assert probe.tree_sample == 0
    assert probe.classification == "opaque"


@pytest.mark.parametrize("cls", ["CASCADIA_HOSTING_WINDOW_CLASS", "ConsoleWindowClass"])
def test_terminal_class_is_recognised(cls):
    """Detected by window class, which is stable, rather than by tree shape."""
    root = FakeElement("term", class_name=cls, children=[FakeElement("c")],
                       bounds=(0, 0, 800, 600))
    assert FakeClient(root).probe_target(1).classification == "terminal"


class SlowElement:
    """Every cached read is slow, simulating a pathological provider."""

    def __init__(self, name):
        self._name = name
        self.CurrentName = name
        self.CurrentClassName = "Slow"
        self.CurrentProcessId = 1
        self.CurrentBoundingRectangle = _Rect(0, 0, 10, 10)

    def __getattr__(self, item):
        if item.startswith("Cached"):
            time.sleep(0.006)
            if item == "CachedName":
                return self._name
            if item == "CachedControlType":
                return 50000
            if item == "CachedClassName":
                return "Slow"
            if item == "CachedAutomationId":
                return ""
            if item == "CachedBoundingRectangle":
                return _Rect(0, 0, 10, 10)
            return False
        raise AttributeError(item)

    def FindAllBuildCache(self, scope, cond, req):
        return FakeArray([])

    def GetCachedPropertyValue(self, pid):
        return False


def test_expensive_target_is_flagged_by_measured_cost():
    """Cost per node varied by a factor of 100 between live targets, so the
    classification has to come from a measurement, not a constant."""
    kids = [SlowElement(f"c{i}") for i in range(6)]
    root = FakeElement("root", children=kids, bounds=(0, 0, 800, 600))
    probe = FakeClient(root).probe_target(1)
    assert probe.us_per_node is not None
    assert probe.us_per_node > 2000
    assert probe.classification == "expensive"
    assert any("дорогая" in n for n in probe.notes)


def test_probe_reports_iconic_and_restoration_advice():
    root = build_narrow_deep()
    probe = FakeClient(root, minimized=True).probe_target(1)
    assert probe.iconic
    assert any("свёрнуто" in n for n in probe.notes)


def test_probe_patterns_are_not_from_global_focus():
    """Polling the globally focused element reported the same inventory for every
    window unless that window happened to be focused."""
    client = FakeClient(build_narrow_deep())
    probe = client.probe_target(1)
    assert probe.patterns == ()


# ------------------------------------------------------------- Class B actions
def one_element(pattern_obj, pid):
    """A tree whose single child carries one pattern."""
    child = FakeElement("target", pattern_objs={pid: pattern_obj})
    return FakeElement("root", children=[child], bounds=(0, 0, 100, 100))


class TestPatternActions:
    def test_set_value_confirms_by_readback(self):
        pat = FakeValuePattern("before")
        root = one_element(pat, PATTERN_IDS["Value"])
        r = FakeClient(root).set_value(1, "0", "after")
        assert r.ok and r.verified is True
        assert (r.before, r.after) == ("before", "after")
        assert pat.CurrentValue == "after"

    def test_set_value_detects_silent_success(self):
        """The terminal trap: SetValue returns success, IsReadOnly reports 0, and
        the value does not change. Reporting ok here is exactly the failure that
        made ADR-0009 require read-back on every terminal write.
        """
        pat = FakeValuePattern("unchanged", ignore_writes=True)
        root = one_element(pat, PATTERN_IDS["Value"])
        r = FakeClient(root).set_value(1, "0", "ignored")
        assert r.ok is False
        assert r.verified is False
        assert "НЕ подтверждено" in r.message
        assert pat.CurrentValue == "unchanged"

    def test_invoke_is_reported_as_unverifiable(self):
        """Invoke has no readable state of its own, so ok=True must not be dressed
        up as confirmation."""
        pat = FakeInvokePattern()
        root = one_element(pat, PATTERN_IDS["Invoke"])
        r = FakeClient(root).invoke(1, "0")
        assert r.ok is True
        assert r.verified is None, "invoke must not claim verification"
        assert pat.calls == 1
        assert "не проверяем" in r.message.lower()

    def test_toggle_confirms_state_change(self):
        pat = FakeTogglePattern(0)
        root = one_element(pat, PATTERN_IDS["Toggle"])
        r = FakeClient(root).toggle(1, "0")
        assert r.ok and r.verified is True
        assert (r.before, r.after) == ("Off", "On")

    def test_toggle_detects_a_state_that_did_not_change(self):
        pat = FakeTogglePattern(1, ignore_calls=True)
        root = one_element(pat, PATTERN_IDS["Toggle"])
        r = FakeClient(root).toggle(1, "0")
        assert r.ok is False and r.verified is False
        assert "НЕ изменилось" in r.message

    def test_select_item_confirms(self):
        pat = FakeSelectionItemPattern()
        root = one_element(pat, PATTERN_IDS["SelectionItem"])
        r = FakeClient(root).select_item(1, "0")
        assert r.ok and r.verified is True
        assert r.after == "True"

    def test_expand_and_collapse(self):
        pat = FakeExpandCollapsePattern(0)
        root = one_element(pat, PATTERN_IDS["ExpandCollapse"])
        c = FakeClient(root)
        assert c.expand_collapse(1, "0", True).after == "Expanded"
        assert c.expand_collapse(1, "0", False).after == "Collapsed"
        assert pat.calls == ["expand", "collapse"]

    def test_missing_pattern_is_reported_not_guessed(self):
        root = one_element(None, PATTERN_IDS["Value"])
        r = FakeClient(root).set_value(1, "0", "x")
        assert r.ok is False
        assert "ValuePattern" in r.message

    @pytest.mark.parametrize("method,args", [
        ("set_value", ("0.9", "x")),
        ("invoke", ("9",)),
        ("toggle", ("0.9",)),
        ("select_item", ("0.9",)),
        ("expand_collapse", ("0.9", True)),
    ])
    def test_stale_path_is_refused(self, method, args):
        """Acting on a path the tree no longer has must fail rather than land on
        some other element."""
        root = FakeElement("root", children=[FakeElement("a")],
                           bounds=(0, 0, 10, 10))
        r = getattr(FakeClient(root), method)(1, *args)
        assert r.ok is False, f"{method} acted on a stale path"
        assert "разрешается" in r.message


class TestReadBack:
    """UIA state is not synchronous with the call that changed it.

    On a live Qt combo box, Expand returned success and an immediate read still
    reported Collapsed; the read a few hundred milliseconds later reported
    Expanded. A single read turns that into a false negative, and an agent that
    retries a toggle or an expand undoes the action it just performed.

    On a live Firefox address bar the gap was about 1000 ms, which is why the
    default timeout is 1.5 s rather than 0.5 s.
    """

    def test_polls_until_the_state_arrives(self):
        calls = {"n": 0}

        def read():
            calls["n"] += 1
            return "Collapsed" if calls["n"] < 3 else "Expanded"

        value, ok = uc.UIAClient._read_back(
            read, lambda v: v == "Expanded", timeout_s=1.0, interval_s=0.0
        )
        assert ok is True
        assert value == "Expanded"
        assert calls["n"] == 3

    def test_gives_up_and_reports_the_last_value(self):
        value, ok = uc.UIAClient._read_back(
            lambda: "Collapsed", lambda v: v == "Expanded",
            timeout_s=0.05, interval_s=0.01
        )
        assert ok is False
        assert value == "Collapsed"

    def test_survives_a_transient_com_error(self):
        calls = {"n": 0}

        def read():
            calls["n"] += 1
            if calls["n"] < 2:
                raise uc.COMError(-2147024809, "transient", None)
            return "On"

        value, ok = uc.UIAClient._read_back(
            read, lambda v: v == "On", timeout_s=1.0, interval_s=0.0
        )
        assert ok is True and value == "On"

    def test_succeeds_immediately_when_already_correct(self):
        calls = {"n": 0}

        def read():
            calls["n"] += 1
            return "Expanded"

        value, ok = uc.UIAClient._read_back(
            read, lambda v: v == "Expanded", timeout_s=1.0, interval_s=0.0
        )
        assert ok is True
        assert calls["n"] == 1, "no reason to sleep when the first read matches"


# ---------------------------------------------------------------- terminals
class _FakeTextRange:
    def __init__(self, text):
        self._text = text

    def GetText(self, _max):
        return self._text


class _FakeTextPattern:
    def __init__(self, text):
        self.DocumentRange = _FakeTextRange(text)


HOSTS = {
    "ConsoleWindowClass": "conhost",
    "CASCADIA_HOSTING_WINDOW_CLASS": "windows-terminal",
}


class TermClient(FakeClient):
    """FakeClient with a canned console buffer behind TextPattern."""

    def __init__(self, root, buffer_text="", host_class="ConsoleWindowClass"):
        super().__init__(root)
        self.buffer_text = buffer_text
        self.host_class = host_class
        self.reads = 0

    def terminal_buffer_element(self, hwnd):
        self.reads += 1
        return object(), _FakeTextPattern(self.buffer_text)

    def terminal_host(self, hwnd):
        return HOSTS.get(self.host_class, "unknown")

    def find_terminal_input(self, hwnd):
        return hwnd


PAD = " " * 40


class TestReadTerminal:
    def test_rows_are_right_stripped_but_indentation_kept(self):
        """The console pads every row to its width; indentation is meaningful."""
        text = f"$ cmd{PAD}\r\n    indented output{PAD}\r\n"
        buf = TermClient(FakeElement("root"), buffer_text=text).read_terminal(1)
        assert buf.error is None
        assert buf.lines == ["$ cmd", "    indented output"]
        assert buf.row_count == 2

    def test_trailing_blank_rows_are_dropped(self):
        text = f"real line{PAD}\r\n{PAD}\r\n{PAD}\r\n"
        buf = TermClient(FakeElement("root"), buffer_text=text).read_terminal(1)
        assert buf.lines == ["real line"]

    def test_carriage_returns_are_removed(self):
        buf = TermClient(FakeElement("root"),
                         buffer_text="a\r\nb\r\n").read_terminal(1)
        assert "\r" not in buf.text
        assert buf.lines == ["a", "b"]

    def test_buffer_full_is_signalled_at_the_ring_limit(self):
        """Past 9000 rows the oldest output is gone for good, and the reader has
        to say so rather than return a plausible-looking truncated result."""
        rows = "\r\n".join(f"line{i}" for i in range(uc.TERMINAL_BUFFER_ROWS))
        buf = TermClient(FakeElement("root"), buffer_text=rows).read_terminal(1)
        assert buf.row_count == uc.TERMINAL_BUFFER_ROWS
        assert buf.buffer_full is True

    def test_small_buffer_is_not_reported_full(self):
        buf = TermClient(FakeElement("root"),
                         buffer_text="a\r\nb\r\n").read_terminal(1)
        assert buf.buffer_full is False

    def test_host_is_reported(self):
        for host_class, expected in HOSTS.items():
            buf = TermClient(FakeElement("root"), buffer_text="x",
                             host_class=host_class).read_terminal(1)
            assert buf.host == expected

    def test_missing_text_pattern_is_an_error_not_empty_text(self):
        """An empty string would look like a terminal with no output."""
        client = FakeClient(FakeElement("root"))
        client.terminal_buffer_element = lambda hwnd: (None, None)
        buf = client.read_terminal(1)
        assert buf.error
        assert buf.lines == []


class TestWaitTerminal:
    def test_echo_trap_returns_immediately_without_an_anchor(self):
        """A shell echoes the command before running it, so a marker that appears in
        the command line satisfies the wait without the command having finished.

        Measured live: found in 0.00 s for a command containing a 6-second delay.
        """
        echoed = "C:\\Users>x>ping -n 7 127.0.0.1 >nul & echo TRAP\r\n"
        client = TermClient(FakeElement("root"), buffer_text=echoed)
        r = client.wait_terminal(1, "TRAP", timeout_s=1.0, interval_s=0.01)
        assert r.found is True
        assert r.polls == 1, "matched the echo on the first poll"

    def test_anchor_past_the_echo_makes_the_wait_real(self):
        echoed = "C:\\Users>x>ping -n 7 127.0.0.1 >nul & echo TRAP\r\n"
        client = TermClient(FakeElement("root"), buffer_text=echoed)
        r = client.wait_terminal(
            1, "TRAP", timeout_s=0.2, interval_s=0.01,
            after_text="ping -n 7 127.0.0.1 >nul & echo TRAP")
        assert r.found is False, "the real output has not arrived yet"
        assert r.message and r.polls > 1

    def test_anchor_still_finds_real_output(self):
        text = ("C:\\x>echo TRAP\r\nTRAP\r\nC:\\x>")
        client = TermClient(FakeElement("root"), buffer_text=text)
        r = client.wait_terminal(1, "TRAP", timeout_s=1.0, interval_s=0.01,
                                 after_text="echo TRAP")
        assert r.found is True

    def test_absence_is_reported_with_a_message(self):
        client = TermClient(FakeElement("root"), buffer_text="some output\r\n")
        r = client.wait_terminal(1, "NEVER", timeout_s=0.2, interval_s=0.01)
        assert r.found is False
        assert "NEVER" in r.message
        assert r.elapsed_s >= 0.2

    def test_last_echo_wins_when_the_command_ran_twice(self):
        """rfind, not find: a repeated command leaves earlier echoes in the buffer,
        and anchoring on the first one would search stale output."""
        text = ("C:\\x>echo TRAP\r\nold\r\nC:\\x>echo TRAP\r\n")
        client = TermClient(FakeElement("root"), buffer_text=text)
        r = client.wait_terminal(1, "old", timeout_s=0.2, interval_s=0.01,
                                 after_text="echo TRAP")
        assert r.found is False, "anchored past the last echo, 'old' is behind it"


class TestTerminalHostClassification:
    def test_conhost_and_terminal_are_distinguished(self):
        for host_class, expected in HOSTS.items():
            root = FakeElement("root", class_name=host_class)
            assert FakeClient(root).terminal_host(1) == expected

    def test_unknown_class_is_not_guessed(self):
        root = FakeElement("root", class_name="SomeOtherClass")
        assert FakeClient(root).terminal_host(1) == "unknown"


def test_post_chars_to_a_dead_window_reports_zero():
    """An invalid handle must not raise into the caller."""
    assert uc._post_chars(0xDEADBEEF, "text", True) == 0


def test_find_child_by_class_returns_none_when_absent():
    assert uc._find_child_by_class(0xDEADBEEF, uc._INPUT_SITE_CLASS) is None
