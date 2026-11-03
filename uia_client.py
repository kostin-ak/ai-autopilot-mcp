"""UI Automation client: bounded, measured, retried.

Implements the snapshot layer decided in ADR-0007 (as revised) and ADR-0008.

Why each rule exists, in one place:

* ``CacheRequest.TreeScope`` is never set. The scope belongs to the FindAll call
  only; with it set, every ``Cached*`` access returns E_INVALIDARG while
  ``FindAllBuildCache`` still reports correct element counts, so the failure is
  invisible in the numbers.
* The primary budget is wall-clock time, not node count. Cost per node varied by
  a factor of 100 across targets (203 us on a browser window, 22 ms on Progman),
  so any fixed node cap either lets a multi-second walk through or truncates a
  cheap target early.
* Chromium and Electron expose a shallow tree until a client asks, so every
  target gets a warm-up pass before anything is measured.
* Failed queries are counted and surfaced. One Firefox query in the measurement
  returned zero nodes in 1 ms; a silent ``continue`` makes that look like an
  empty tree.
* Element text is taken from Name first, then ClassName. ValuePattern and
  TextPattern would need a round trip per element, which is most of the budget on
  a 400-node tree, so availability is recorded as a cached flag and the value is
  fetched on demand for one node at a time.
* Qt sometimes reports a widget's internal class chain as its Name. Such a value
  is not text and must not be shown as a label or used for a recipient check.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from dataclasses import dataclass, field

import comtypes
import comtypes.client
from comtypes import COMError

logger = logging.getLogger(__name__)

# Stop thresholds. 150 ms is the target from ADR-0007; the node and depth limits
# are secondary guards for the case where the time budget cannot be honoured
# because a single query is itself slow.
DEFAULT_TIME_BUDGET_S = 0.150
DEFAULT_MAX_NODES = 400
# Depth is a runaway guard, not the working limit. Measured coverage showed the
# real work is bounded by time and node count; a low depth just loses subtrees
# without buying speed, because the time budget already stops a wide walk.
DEFAULT_MAX_DEPTH = 20

# A single FindAll that takes longer than this is treated as a pathological
# target (Progman took seconds), and the walk stops rather than retrying.
SINGLE_QUERY_LIMIT_S = 1.0

# WindowVisualState
_WINDOW_NORMAL = 0

# Availability flags for patterns that carry text.
_PATTERN_FLAGS = {
    "Invoke": "UIA_IsInvokePatternAvailablePropertyId",
    "Value": "UIA_IsValuePatternAvailablePropertyId",
    "Toggle": "UIA_IsTogglePatternAvailablePropertyId",
    "SelectionItem": "UIA_IsSelectionItemPatternAvailablePropertyId",
    "ExpandCollapse": "UIA_IsExpandCollapsePatternAvailablePropertyId",
    "ScrollItem": "UIA_IsScrollItemPatternAvailablePropertyId",
    "RangeValue": "UIA_IsRangeValuePatternAvailablePropertyId",
    "Text": "UIA_IsTextPatternAvailablePropertyId",
    "GridItem": "UIA_IsGridItemPatternAvailablePropertyId",
    "TableItem": "UIA_IsTableItemPatternAvailablePropertyId",
    "Window": "UIA_IsWindowPatternAvailablePropertyId",
    "Transform": "UIA_IsTransformPatternAvailablePropertyId",
}


# Win32 reports a minimized window's rect at roughly (-32000, -32000), offset by
# its border, so observed values look like (-31981, -31981). No real display sits
# that far out, so anything below this bound is the sentinel rather than a
# secondary monitor to the left or above the primary.
SENTINEL_BOUND = -16000


def _is_sentinel(x: int, y: int) -> bool:
    """True for coordinates belonging to a minimized window, not to a display."""
    return x < SENTINEL_BOUND or y < SENTINEL_BOUND


@dataclass(frozen=True)
class NodeSnapshot:
    """One element in a bounded snapshot."""

    depth: int
    role: str
    text: str
    text_source: str  # name | class | none
    automation_id: str
    class_name: str
    bounds: tuple[int, int, int, int] | None
    # Rect centre from cached bounds. UIA's own clickable point is resolved on
    # demand by UIAClient.clickable_point, because it is a live round trip and
    # costs 162 us per node if taken for the whole tree.
    clickable: tuple[int, int] | None
    enabled: bool
    offscreen: bool
    focusable: bool
    has_focus: bool
    patterns: tuple[str, ...]
    path: str
    text_usable: bool

    @property
    def identity(self) -> str:
        """Stable within a snapshot, and stable across snapshots while the tree is.

        Built from the sibling-index path. AutomationId is deliberately not part
        of it: it is populated on 0-24% of elements in real applications.
        """
        return self.path


@dataclass
class Snapshot:
    """Result of a bounded walk."""

    nodes: list[NodeSnapshot]
    truncated: bool
    truncated_reason: str  # "time" | "nodes" | "depth" | ""
    next_token: str | None
    elapsed_ms: float
    queries: int
    failed_queries: int
    warnings: list[str] = field(default_factory=list)
    # A minimized window reports coordinates around (-32000, -32000) and captures
    # as black, so its clickable points are not usable even though the tree is
    # readable. ADR-0006 makes restoring it a precondition for observation.
    minimized: bool = False
    auto_restored: bool = False


@dataclass
class TargetProbe:
    """What a target actually offers, before choosing a path."""

    handle: int
    title: str
    class_name: str
    pid: int
    iconic: bool
    child_count: int
    us_per_node: float | None
    classification: str  # terminal | rich | partial | opaque | expensive
    patterns: tuple[str, ...]
    notes: list[str] = field(default_factory=list)


class UIAClient:
    """Apartment-threaded UI Automation access with a bounded snapshot walk."""

    def __init__(self) -> None:
        self._local = threading.local()
        self._module = None
        self._module_lock = threading.Lock()
        # Memoised lookups. Rebuilding these per node means a dir() scan of the
        # type library on every element, which is measurable on a 400-node walk.
        self._maps_lock = threading.Lock()
        self._ct_names: dict[int, str] | None = None
        self._pattern_map: dict[str, int] | None = None
        self._flag_ids: dict[str, int] | None = None

    # ---------------------------------------------------------------- plumbing
    def _typelib(self):
        """Loads (once, process-wide) the comtypes wrapper for UIAutomationCore."""
        if self._module is None:
            with self._module_lock:
                if self._module is None:
                    self._module = comtypes.client.GetModule("UIAutomationCore.dll")
        return self._module

    def _automation(self):
        """Per-thread IUIAutomation. COM apartments are thread-affine."""
        auto = getattr(self._local, "uia", None)
        if auto is None:
            comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
            auto = comtypes.client.CreateObject(
                self._typelib().CUIAutomation, interface=self._typelib().IUIAutomation
            )
            self._local.uia = auto
        return auto

    def _retry(self, fn, attempts: int = 3, delay: float = 0.05):
        """UIA calls fail sporadically; a transient COMError is not a verdict."""
        last = None
        for attempt in range(attempts):
            try:
                return fn()
            except COMError as exc:
                last = exc
                if attempt + 1 < attempts:
                    time.sleep(delay * (attempt + 1))
        raise last  # type: ignore[misc]

    # ------------------------------------------------------------------ maps
    def control_type_names(self) -> dict[int, str]:
        """Control type id -> name, read from the type library rather than hardcoded."""
        if self._ct_names is not None:
            return self._ct_names
        with self._maps_lock:
            if self._ct_names is None:
                mod = self._typelib()
                mapping = {}
                for name in dir(mod):
                    if name.startswith("UIA_") and name.endswith("ControlTypeId"):
                        pretty = name[len("UIA_"): -len("ControlTypeId")]
                        try:
                            mapping[int(getattr(mod, name))] = pretty
                        except (TypeError, ValueError):
                            continue
                self._ct_names = mapping
        return self._ct_names

    def patterns(self) -> dict[str, int]:
        if self._pattern_map is not None:
            return self._pattern_map
        with self._maps_lock:
            if self._pattern_map is None:
                mod = self._typelib()
                self._pattern_map = {
                    n[len("UIA_"): -len("PatternId")]: int(getattr(mod, n))
                    for n in dir(mod)
                    if n.startswith("UIA_") and n.endswith("PatternId")
                }
        return self._pattern_map

    def pattern_flags(self) -> dict[str, int]:
        """Availability-flag property ids, keyed by short pattern name."""
        if self._flag_ids is not None:
            return self._flag_ids
        with self._maps_lock:
            if self._flag_ids is None:
                mod = self._typelib()
                ids = {}
                for short, const in _PATTERN_FLAGS.items():
                    try:
                        ids[short] = int(getattr(mod, const))
                    except AttributeError:
                        continue
                self._flag_ids = ids
        return self._flag_ids

    # ---------------------------------------------------------------- windows
    def element_from_handle(self, hwnd: int):
        """Root element for a top-level window."""
        auto = self._automation()
        return self._retry(lambda: auto.ElementFromHandle(hwnd))

    def target_info(self, hwnd: int) -> dict:
        el = self.element_from_handle(hwnd)
        return {
            "handle": hwnd,
            "title": self._retry(lambda: el.CurrentName or ""),
            "class_name": self._retry(lambda: el.CurrentClassName or ""),
            "pid": self._retry(lambda: int(el.CurrentProcessId)),
        }

    def is_minimized(self, hwnd: int) -> bool:
        """Minimized windows capture as black, so this gates observation."""
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            user32.IsIconic.argtypes = [wintypes.HWND]
            user32.IsIconic.restype = wintypes.BOOL
            return bool(user32.IsIconic(hwnd))
        except Exception:
            return False

    def ensure_restored(self, hwnd: int) -> bool:
        """Restores a minimized window through WindowPattern, a Class B action.

        Returns True if the window is not minimized afterwards. Does nothing if it
        was never minimized, so it is safe to call unconditionally before a
        capture.
        """
        if not self.is_minimized(hwnd):
            return True
        try:
            el = self.element_from_handle(hwnd)
            pats = self.patterns()
            unk = self._retry(lambda: el.GetCurrentPattern(pats["Window"]))
            if not unk:
                return False
            win = unk.QueryInterface(self._typelib().IUIAutomationWindowPattern)
            self._retry(lambda: win.SetWindowVisualState(_WINDOW_NORMAL))
            time.sleep(0.2)
            return not self.is_minimized(hwnd)
        except (COMError, AttributeError, KeyError) as exc:
            logger.debug("ensure_restored failed for %s: %s", hwnd, exc)
            return False

    # ----------------------------------------------------------------- probe
    def probe_target(self, hwnd: int, sample_nodes: int = 40) -> TargetProbe:
        """Classifies what a target offers, measuring cost rather than assuming it.

        Cost per node spans two orders of magnitude between targets, so the
        classification has to come from a measurement on this window.
        """
        info = self.target_info(hwnd)
        auto = self._automation()
        mod = self._typelib()
        notes: list[str] = []

        iconic = self.is_minimized(hwnd)
        if iconic:
            notes.append(
                "Окно свёрнуто: захват вернёт чёрный кадр. Требуется разворот "
                "через WindowPattern перед наблюдением."
            )

        el = self.element_from_handle(hwnd)
        true_cond = auto.CreateTrueCondition()

        # Warm-up: Chromium/Electron build their tree only once a client asks, so the
        # first query on those apps returns a shallow tree. The result is discarded;
        # the call exists to make the provider populate itself.
        t0 = time.perf_counter()
        with contextlib.suppress(COMError):
            self._retry(
                lambda: el.FindAllBuildCache(
                    mod.TreeScope_Children, true_cond, self._cache_request()
                )
            )
        warm_ms = (time.perf_counter() - t0) * 1000

        # Size the first level, then time a bounded sample to get cost per node.
        try:
            kids = self._retry(
                lambda: el.FindAllBuildCache(
                    mod.TreeScope_Children, true_cond, self._cache_request()
                )
            )
            child_count = kids.Length
        except COMError as exc:
            return TargetProbe(
                handle=hwnd, title=info["title"], class_name=info["class_name"],
                pid=info["pid"], iconic=iconic, child_count=0, us_per_node=None,
                classification="opaque", patterns=(),
                notes=notes + [f"FindAll(Children) failed: {exc}"],
            )

        us_per_node = None
        if child_count:
            t0 = time.perf_counter()
            read = 0
            for i in range(min(child_count, sample_nodes)):
                e = kids.GetElement(i)
                try:
                    # Touching the cached properties is itself the measurement:
                    # this is what one snapshot node costs on this target.
                    _ = (e.CachedName, e.CachedControlType, e.CachedBoundingRectangle)
                    read += 1
                except COMError:
                    pass
            if read:
                us_per_node = (time.perf_counter() - t0) * 1_000_000 / read

        # Pattern inventory from the focused element, per ADR-0007: this is the
        # documented probe path and does not need a tree walk.
        pattern_names: tuple[str, ...] = ()
        try:
            focus = self._retry(auto.GetFocusedElement)
            ids, names = self._retry(
                lambda: auto.PollForPotentialSupportedPatterns(focus)
            )
            pattern_names = tuple(str(n) for n in names)
        except COMError:
            pass

        classification = self._classify(
            child_count=child_count, us_per_node=us_per_node,
            iconic=iconic, el=el, notes=notes,
        )
        if us_per_node is not None and us_per_node > 2000:
            notes.append(
                f"Стоимость узла {us_per_node:.0f} мкс — цель патологически дорогая "
                "(Progman в замере давал 22 000 мкс). Снимок будет заведомо неполным."
            )
        if warm_ms > 300:
            notes.append(f"Прогревочный запрос занял {warm_ms:.0f} мс.")

        return TargetProbe(
            handle=hwnd, title=info["title"], class_name=info["class_name"],
            pid=info["pid"], iconic=iconic, child_count=child_count,
            us_per_node=us_per_node, classification=classification,
            patterns=pattern_names, notes=notes,
        )

    def _classify(self, *, child_count, us_per_node, iconic, el, notes) -> str:
        """Thresholds come from the measurements, not from taste."""
        if us_per_node is not None and us_per_node > 2000:
            return "expensive"

        # Terminals: tiny tree with one large TextPattern buffer. Detected by the
        # window class, which is stable, rather than by tree shape alone.
        try:
            cls = (self._retry(lambda: el.CurrentClassName) or "").lower()
        except COMError:
            cls = ""
        if "cascadia_hosting" in cls or "consolewindowclass" in cls:
            return "terminal"

        if child_count == 0:
            return "opaque"
        if child_count < 20:
            notes.append(
                f"Первый уровень содержит {child_count} элементов; для большого "
                "окна это признак непрозрачной цели."
            )
            return "opaque"
        named = self._named_fraction(el)
        if named is not None and named < 0.4:
            notes.append(f"Имена заполнены у {named*100:.0f}% первого уровня.")
            return "partial"
        return "rich"

    def _named_fraction(self, el) -> float | None:
        auto = self._automation()
        mod = self._typelib()
        try:
            kids = self._retry(
                lambda: el.FindAllBuildCache(
                    mod.TreeScope_Children, auto.CreateTrueCondition(),
                    self._cache_request(),
                )
            )
        except COMError:
            return None
        if not kids.Length:
            return None
        named = 0
        for i in range(min(kids.Length, 60)):
            try:
                if (kids.GetElement(i).CachedName or "").strip():
                    named += 1
            except COMError:
                pass
        return named / min(kids.Length, 60)

    # ---------------------------------------------------------------- snapshot
    def _cache_request(self):
        """Builds a cache request. TreeScope is deliberately left untouched."""
        auto = self._automation()
        mod = self._typelib()
        req = auto.CreateCacheRequest()
        for prop in (
            "UIA_NamePropertyId", "UIA_ControlTypePropertyId",
            "UIA_BoundingRectanglePropertyId", "UIA_AutomationIdPropertyId",
            "UIA_ClassNamePropertyId", "UIA_IsEnabledPropertyId",
            "UIA_IsOffscreenPropertyId", "UIA_IsKeyboardFocusablePropertyId",
            "UIA_HasKeyboardFocusPropertyId",
        ):
            try:
                req.AddProperty(int(getattr(mod, prop)))
            except (AttributeError, COMError):
                continue
        for flag in _PATTERN_FLAGS.values():
            try:
                req.AddProperty(int(getattr(mod, flag)))
            except (AttributeError, COMError):
                continue
        return req

    def snapshot(
        self,
        hwnd: int,
        *,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_nodes: int = DEFAULT_MAX_NODES,
        time_budget_s: float = DEFAULT_TIME_BUDGET_S,
        token: str | None = None,
        auto_restore: bool = False,
    ) -> Snapshot:
        """Bounded DFS over a window's tree.

        The continuation token means exactly one thing: the path of the next node
        to visit, not yet emitted. That makes paging checkable -- consecutive
        pages must cover every path once, in order, with no duplicates and no
        gaps.

        Truncation by depth is terminal and carries no token, because the frontier
        cannot express "descend into a node that was skipped", so paging would
        silently lose that subtree; the caller has to raise max_depth instead.
        Truncation by time or node count is resumable.

        A per-level walk costs about 1.5x a single whole-subtree query on the same
        window (measured 316 ms versus 200 ms for 804 nodes), but it is the only
        shape that can be stopped mid-way and that yields real depth and paths.
        The time budget does the real limiting; depth is only a runaway guard.
        """
        started = time.monotonic()
        auto = self._automation()
        mod = self._typelib()
        true_cond = auto.CreateTrueCondition()
        req = self._cache_request()

        nodes: list[NodeSnapshot] = []
        queries = 0
        failed = 0
        warnings: list[str] = []
        truncated_reason = ""

        # A minimized window still yields a readable tree, but every coordinate it
        # reports sits at the (-32000, -32000) sentinel and a capture of it is
        # black. Restoring changes the user's desktop, so it is opt-in; the default
        # is to say plainly that the coordinates are unusable.
        minimized = self.is_minimized(hwnd)
        restored = False
        if minimized and auto_restore:
            restored = self.ensure_restored(hwnd)
            minimized = self.is_minimized(hwnd)
        if minimized:
            warnings.append(
                "Окно свёрнуто: координаты недействительны, Win32 сообщает "
                "(-32000, -32000), а захват вернёт чёрный кадр. Дерево при этом "
                "читается. Разверните окно (ensure_restored или auto_restore=True) "
                "перед любым действием по координатам."
            )

        def over_deadline() -> bool:
            return (time.monotonic() - started) >= time_budget_s

        def children_of(el):
            nonlocal queries, failed
            t0 = time.monotonic()
            try:
                arr = el.FindAllBuildCache(mod.TreeScope_Children, true_cond, req)
                queries += 1
                if (time.monotonic() - t0) > SINGLE_QUERY_LIMIT_S:
                    warnings.append(
                        f"Один запрос занял {(time.monotonic()-t0)*1000:.0f} мс — "
                        "цель патологически дорогая."
                    )
                return arr
            except COMError:
                failed += 1
                return None

        root_el = self.element_from_handle(hwnd)

        # stack entries: (element, depth, path, children_array, next_child_index)
        stack: list[list] = []
        if token:
            rebuilt = self._rebuild(root_el, token, children_of)
            if rebuilt is None:
                warnings.append(
                    f"Токен продолжения {token!r} не разрешён: дерево изменилось. "
                    "Снимок начат заново."
                )
            else:
                stack = rebuilt
        if not stack:
            kids = children_of(root_el)
            if kids is None:
                return Snapshot(
                    nodes=[], truncated=False, truncated_reason="",
                    next_token=None,
                    elapsed_ms=(time.monotonic() - started) * 1000,
                    queries=queries, failed_queries=failed,
                    warnings=warnings + ["Не удалось получить дочерние элементы окна."],
                    minimized=minimized, auto_restored=restored,
                )
            stack = [[root_el, 0, "", kids, 0]]

        next_token = None
        role_map = self.control_type_names()
        flag_ids = self.pattern_flags()
        while stack:
            if over_deadline():
                truncated_reason = "time"
                next_token = self._token_from(stack)
                break
            if len(nodes) >= max_nodes:
                truncated_reason = "nodes"
                next_token = self._token_from(stack)
                break

            frame = stack[-1]
            _el, depth, path, kids, idx = frame
            if idx >= kids.Length:
                stack.pop()
                continue
            frame[4] = idx + 1

            try:
                node = kids.GetElement(idx)
            except COMError:
                failed += 1
                continue

            node_path = f"{path}.{idx}" if path else str(idx)
            snap = self._snapshot_node(
                node, depth + 1, node_path, warnings, role_map, flag_ids
            )
            nodes.append(snap)

            if depth + 1 >= max_depth:
                # Terminal, not resumable: the frontier cannot express "descend
                # into a node that was skipped", so paging would drop this
                # subtree without saying so. Report it; the caller raises the
                # limit instead.
                sub = children_of(node)
                if sub is not None and sub.Length:
                    truncated_reason = "depth"
                    warnings.append(
                        f"Бюджет глубины {max_depth} исчерпан на узле {node_path}; "
                        "продолжение невозможно, увеличьте max_depth."
                    )
                    break
                continue

            sub = children_of(node)
            if sub is not None and sub.Length:
                stack.append([node, depth + 1, node_path, sub, 0])

        if truncated_reason == "depth":
            # Depth truncation is terminal by contract; see the docstring.
            next_token = None

        return Snapshot(
            nodes=nodes,
            truncated=bool(truncated_reason),
            truncated_reason=truncated_reason,
            next_token=next_token,
            elapsed_ms=(time.monotonic() - started) * 1000,
            queries=queries,
            failed_queries=failed,
            warnings=warnings,
            minimized=minimized,
            auto_restored=restored,
        )

    # ------------------------------------------------------------------ nodes
    def _snapshot_node(
        self, node, depth: int, path: str, warnings: list[str],
        role_map: dict[int, str], flag_ids: dict[str, int],
    ):
        """Reads cached properties only; no extra round trips.

        role_map and flag_ids are passed in because rebuilding them per node
        would put a type-library scan inside the walk loop.
        """
        def cached(attr, default=None):
            try:
                return getattr(node, attr)
            except COMError:
                return default

        name = (cached("CachedName") or "").strip()
        class_name = (cached("CachedClassName") or "").strip()
        automation_id = (cached("CachedAutomationId") or "").strip()

        text, source, usable = self._resolve_text(name, class_name)
        if not usable and name:
            warnings.append(
                f"Узел {path}: имя похоже на внутреннюю цепочку классов Qt, "
                "показано не будет."
            )

        rect_raw = cached("CachedBoundingRectangle")
        bounds = None
        clickable = None
        if rect_raw is not None:
            try:
                bounds = (rect_raw.left, rect_raw.top, rect_raw.right, rect_raw.bottom)
                # A minimized window reports the sentinel rect, which lies on no
                # monitor, so it must not be published as a place to click.
                if (bounds[2] > bounds[0] and bounds[3] > bounds[1]
                        and not _is_sentinel(bounds[0], bounds[1])):
                    clickable = self._centre(bounds)
            except (AttributeError, COMError):
                bounds = None

        ct_id = cached("CachedControlType") or 0
        patterns = []
        for pname, pid in flag_ids.items():
            try:
                if node.GetCachedPropertyValue(pid):
                    patterns.append(pname)
            except COMError:
                pass

        return NodeSnapshot(
            depth=depth,
            role=role_map.get(int(ct_id), f"Type{ct_id}"),
            text=text,
            text_source=source,
            automation_id=automation_id,
            class_name=class_name,
            bounds=bounds,
            clickable=clickable,
            enabled=bool(cached("CachedIsEnabled", True)),
            offscreen=bool(cached("CachedIsOffscreen", False)),
            focusable=bool(cached("CachedIsKeyboardFocusable", False)),
            has_focus=bool(cached("CachedHasKeyboardFocus", False)),
            patterns=tuple(sorted(patterns)),
            path=path,
            text_usable=usable,
        )

    @staticmethod
    def _resolve_text(name: str, class_name: str) -> tuple[str, str, bool]:
        """Name, then ClassName; reports the source and whether it is real content.

        Qt reports a widget's internal class chain as its Name when no accessible
        name was set -- for example
        "class MainWindow.class Ui::RpWidget.class MainWidget.class ...". Such a
        value must be neither shown as a label nor used for a recipient check, but
        rejecting it must not also discard a usable ClassName, so the garbage case
        falls through instead of returning empty.

        The third value is True only for text that came from Name. A ClassName is
        a display hint, not content: enough to tell elements apart in a snapshot,
        not enough to identify a message recipient.

        Qt writes its C++ widget type into ClassName as well, observed live as
        "class Ui::RpWidget", so the fallback is checked against the same detector
        rather than trusted.
        """
        if name and not _looks_like_class_chain(name):
            return name, "name", True
        if class_name and not _looks_like_class_chain(class_name):
            return class_name, "class", False
        # Qt also writes the C++ widget type into ClassName, so the fallback can
        # carry the same garbage the Name did. Rejecting it leaves the role as the
        # only honest description, which is what an unnamed container deserves.
        return "", "none", False

    @staticmethod
    def _centre(bounds):
        """Rect centre from cached bounds.

        UIA's own GetClickablePoint is more correct where a child overlays the
        parent, but it is a live round trip rather than a cached read: measured at
        162 us per node, 131 ms across an 807-node tree, which is 26% of the walk.
        Paying that for every node to benefit the handful that get acted on is the
        wrong trade, so the snapshot carries the centre and ``clickable_point``
        resolves the precise one on demand -- at the moment the agent is about to
        act, which is also fresher.
        """
        return ((bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2)

    def clickable_point(self, hwnd: int, path: str) -> tuple[int, int] | None:
        """Resolves UIA's own clickable point for one element, by snapshot path.

        The path is the sibling-index path from a snapshot, so this is how a node
        from an earlier snapshot is turned into a coordinate for an action.
        Returns None if the path no longer resolves, which means the tree changed
        and the caller should re-snapshot rather than click a stale location.
        """
        try:
            segments = [int(s) for s in path.strip(".").split(".") if s != ""]
        except ValueError:
            return None
        if not segments:
            return None

        auto = self._automation()
        mod = self._typelib()
        true_cond = auto.CreateTrueCondition()
        req = self._cache_request()
        current = self.element_from_handle(hwnd)

        for seg in segments:
            try:
                kids = self._retry(
                    lambda node=current: node.FindAllBuildCache(
                        mod.TreeScope_Children, true_cond, req
                    )
                )
            except COMError:
                return None
            if seg >= kids.Length:
                return None
            try:
                current = kids.GetElement(seg)
            except COMError:
                return None

        # An offscreen element has no clickable point. Returning its degenerate bounds
        # centre would hand the caller a coordinate that is not on any monitor, and
        # (-32000, -32000) is the Win32 minimized-window sentinel.
        try:
            if current.CurrentIsOffscreen:
                return None
        except COMError:
            pass

        point = None
        try:
            ok, raw = current.GetClickablePoint()
            if ok and raw:
                point = (int(raw.x), int(raw.y))
        except (COMError, AttributeError, TypeError):
            point = None
        if point is None:
            try:
                r = current.CurrentBoundingRectangle
                point = ((r.left + r.right) // 2, (r.top + r.bottom) // 2)
            except COMError:
                return None
        if _is_sentinel(*point):
            return None
        return point

    # ------------------------------------------------------- continuation
    @staticmethod
    def _token_from(stack: list[list]) -> str | None:
        """Path of the next node to visit, or None when the walk is complete.

        Scans from the top because the uppermost frames may already be exhausted.
        Frames carry the path of the element being enumerated, so the token is
        that path plus the pending child index: a path to a node that has not been
        emitted yet.
        """
        for _el, _depth, path, kids, idx in reversed(stack):
            if idx < kids.Length:
                return f"{path}.{idx}" if path else str(idx)
        return None

    def _rebuild(self, root_el, token: str, children_of):
        """Rebuilds the DFS stack so that ``token`` is the next node to visit.

        The last segment names a child that has not been emitted yet, so its frame
        is left pointing at it. Every earlier segment names a child already emitted
        on a previous page, so those frames continue after it.

        Frames always carry the path of the element being enumerated, not of the
        child. Storing the child's path instead shifts every subsequently emitted
        path by one segment, which is what made the first implementation emit
        duplicates and then loop between two tokens forever.

        Returns None when the token no longer resolves, meaning the tree changed
        between pages. The caller restarts rather than guessing.
        """
        try:
            segments = [int(s) for s in token.strip(".").split(".") if s != ""]
        except ValueError:
            return None
        if not segments:
            return None

        stack: list[list] = []
        current = root_el
        current_path = ""
        for depth, seg in enumerate(segments[:-1]):
            kids = children_of(current)
            if kids is None or seg >= kids.Length:
                return None
            stack.append([current, depth, current_path, kids, seg + 1])
            try:
                current = kids.GetElement(seg)
            except COMError:
                return None
            current_path = f"{current_path}.{seg}" if current_path else str(seg)

        kids = children_of(current)
        if kids is None or segments[-1] > kids.Length:
            return None
        stack.append([current, len(segments) - 1, current_path, kids, segments[-1]])
        return stack

    # ------------------------------------------------------------ formatting
    def format_snapshot(self, snap: Snapshot, *, max_lines: int = 120) -> str:
        """Compact text rendering of a snapshot for a model to read."""
        lines = [
            f"Снимок: {len(snap.nodes)} узлов за {snap.elapsed_ms:.0f} мс, "
            f"запросов {snap.queries}, отказов {snap.failed_queries}"
        ]
        if snap.truncated:
            reason = {
                "time": "исчерпан бюджет времени",
                "nodes": "исчерпан потолок узлов",
                "depth": "исчерпан бюджет глубины",
            }.get(snap.truncated_reason, snap.truncated_reason)
            lines.append(f"СНИМОК НЕПОЛОН: {reason}.")
            if snap.next_token:
                lines.append(f"Продолжение: token={snap.next_token}")
        for w in snap.warnings[:6]:
            lines.append(f"Внимание: {w}")

        for shown, n in enumerate(snap.nodes):
            if shown >= max_lines:
                lines.append(f"... ещё {len(snap.nodes) - shown} узлов не показано")
                break
            indent = "  " * min(n.depth - 1, 10)
            bits = [f"{indent}{n.path} [{n.role}]"]
            if n.text:
                bits.append(repr(n.text[:60]))
            else:
                bits.append("(без текста)")
            if n.clickable:
                bits.append(f"@{n.clickable[0]},{n.clickable[1]}")
            if not n.enabled:
                bits.append("disabled")
            if n.offscreen:
                bits.append("offscreen")
            if n.has_focus:
                bits.append("FOCUS")
            if n.patterns:
                bits.append("{" + ",".join(n.patterns) + "}")
            lines.append(" ".join(bits))
        return "\n".join(lines)


def _looks_like_class_chain(text: str) -> bool:
    """True for Qt's internal widget-name chains masquerading as an accessible name.

    Two observed forms, both from Qt when no accessible name was set:

        class MainWindow.class Ui::RpWidget.class MainWidget.class ...
        class Ui::RpWidget

    The single-segment form was missed by an earlier version that required two
    occurrences of "class ", so the C++ namespace separator is checked as well.
    Ordinary labels that merely begin with the word are still accepted: "class
    2024 schedule" has no "::", one "class ", and no ".class ".
    """
    if not text.startswith("class "):
        return False
    return "::" in text or text.count("class ") >= 2 or ".class " in text
