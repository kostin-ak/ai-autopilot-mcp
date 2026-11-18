"""Browser landmarks from a bounded UIA snapshot, without a browser protocol.

This is the UIA/MSAA mode from ADR-0005: an already-running browser is used as it
is, with no debug port and no restart. Firefox publishes stable AutomationIds for
the browser chrome even when its localized labels change. Web content remains a
normal UIA tree; these addresses are only for the browser chrome around it.
"""
from __future__ import annotations

from collections.abc import Callable


def _descends(path: str, prefix: str) -> bool:
    """True when a sibling-index path is inside another path's subtree."""
    if path == prefix:
        return True
    return path.startswith(prefix + ".")


def _node_summary(node) -> dict:
    """Keeps the stable identity separate from the snapshot-relative action path."""
    return {
        "path": node.path,
        "automation_id": node.automation_id,
        "text": node.text,
        "role": node.role,
        "offscreen": node.offscreen,
        "clickable": list(node.clickable) if node.clickable else None,
        "patterns": list(node.patterns),
    }


def find_browser_landmarks(
    snapshot,
    selected_reader: Callable[[str], bool | None] | None = None,
    address_value: str | None = None,
) -> dict:
    """Finds browser chrome landmarks and lists tabs without exposing close buttons.

    The returned ``path`` values are valid only for this snapshot, exactly like any
    other snapshot path. ``automation_id`` is the stable hint used to find them again.
    A selected tab is read through SelectionItem, not inferred from text or focus.
    """
    nodes = list(snapshot.nodes)
    by_id = {node.automation_id: node for node in nodes if node.automation_id}

    address = (
        by_id.get("urlbar-input")
        or by_id.get("address and search bar")
        or by_id.get("Address and search bar")
    )
    tab_strip = by_id.get("tabbrowser-tabs")
    if tab_strip is not None:
        tabs = [node for node in nodes
                if node.role == "TabItem" and _descends(node.path, tab_strip.path)]
    else:
        # A page can expose its own TabItem widgets. Without the browser chrome's
        # tab-strip identity those are not browser tabs, so absence is the honest answer.
        tabs = []

    firefox = bool(by_id.get("TabsToolbar") or by_id.get("nav-bar")
                   or by_id.get("urlbar-input"))
    browser_kind = "firefox" if firefox else ("chromium" if address else "unknown")
    warnings: list[str] = []
    if address is None:
        warnings.append("Адресная строка не найдена в этом снимке.")
    if tab_strip is None and not tabs:
        warnings.append("Полоса вкладок не найдена в этом снимке.")

    tab_items = []
    selected = None
    for tab in tabs:
        is_selected = selected_reader(tab.path) if selected_reader else None
        item = _node_summary(tab)
        item["selected"] = is_selected
        tab_items.append(item)
        if is_selected:
            selected = item

    bookmarks_toolbar = by_id.get("PersonalToolbar")
    if bookmarks_toolbar is None:
        bookmarks = []
        warnings.append("Панель закладок не найдена в этом снимке: она может быть скрыта.")
    else:
        bookmarks = [
            _node_summary(node) for node in nodes
            if _descends(node.path, bookmarks_toolbar.path)
            and node.role in {"Hyperlink", "Button", "MenuItem"}
            and node.text
        ]

    result = {
        "browser": browser_kind,
        "address_bar": _node_summary(address) if address else None,
        "address_value": address_value,
        "tab_strip": _node_summary(tab_strip) if tab_strip else None,
        "selected_tab": selected,
        "tabs": tab_items,
        "bookmarks_toolbar": _node_summary(bookmarks_toolbar) if bookmarks_toolbar else None,
        "bookmarks": bookmarks,
        "warnings": warnings,
    }
    return result


def format_browser_landmarks(data: dict) -> str:
    """Compact text for the MCP response; paths are directly usable with act_on_element."""
    lines = [f"Браузер: {data.get('browser', 'unknown')}"]
    address = data.get("address_bar")
    if address:
        lines.append(f"Адресная строка: path={address['path']} id={address['automation_id']}")
        if data.get("address_value"):
            lines.append(f"Адрес: {data['address_value']}")
    else:
        lines.append("Адресная строка: не найдена")
    tabs = data.get("tabs") or []
    lines.append(f"Вкладок: {len(tabs)}")
    for tab in tabs:
        selected = " [ТЕКУЩАЯ]" if tab.get("selected") else ""
        offscreen = " [ВНЕ ВИДИМОЙ ОБЛАСТИ]" if tab.get("offscreen") else ""
        patterns = ",".join(tab.get("patterns") or [])
        lines.append(f"  path={tab['path']} {tab['text']!r}{selected}{offscreen} "
                     f"{{{patterns}}}")
    selected_tab = data.get("selected_tab")
    if selected_tab:
        lines.append(f"Текущая вкладка: {selected_tab['text']!r}")
    bookmarks = data.get("bookmarks") or []
    if bookmarks:
        lines.append("Закладки:")
        for bookmark in bookmarks:
            lines.append(f"  path={bookmark['path']} {bookmark['text']!r}")
    else:
        lines.append("Закладки: не найдены в этом снимке (панель может быть скрыта).")
    for warning in data.get("warnings") or []:
        lines.append(f"Внимание: {warning}")
    return "\n".join(lines)
