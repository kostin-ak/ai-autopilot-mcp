"""Browser chrome is addressed by stable AutomationIds, not localized labels."""
from __future__ import annotations

from types import SimpleNamespace

import browser_targets


def node(path, role, *, automation_id="", text="", patterns=(), offscreen=False,
         clickable=(10, 10)):
    return SimpleNamespace(
        path=path, role=role, automation_id=automation_id, text=text,
        class_name="", offscreen=offscreen, clickable=clickable,
        patterns=tuple(patterns),
    )


def firefox_snapshot():
    return SimpleNamespace(nodes=[
        node("28", "ToolBar", automation_id="TabsToolbar"),
        node("28.1", "Tab", automation_id="tabbrowser-tabs"),
        node("28.1.1", "TabItem", text="First", patterns=("SelectionItem", "ScrollItem"),
             clickable=(20, 20)),
        node("28.1.1.0", "Button", text="Close tab", patterns=("Invoke",)),
        node("28.1.5", "TabItem", text="Second", patterns=("SelectionItem", "ScrollItem"),
             clickable=(80, 20)),
        node("30", "ToolBar", automation_id="nav-bar"),
        node("30.5.1", "ComboBox", automation_id="urlbar-input",
             patterns=("Value", "Text", "ExpandCollapse")),
    ])


class TestBrowserLandmarks:
    def test_firefox_addresses_are_found_by_automation_id(self):
        data = browser_targets.find_browser_landmarks(
            firefox_snapshot(), address_value="vk.ru/kostin_ak")
        assert data["browser"] == "firefox"
        assert data["address_bar"]["path"] == "30.5.1"
        assert data["address_bar"]["automation_id"] == "urlbar-input"
        assert data["address_value"] == "vk.ru/kostin_ak"
        assert data["tab_strip"]["automation_id"] == "tabbrowser-tabs"

    def test_close_buttons_are_not_tabs_even_inside_the_tab_subtree(self):
        data = browser_targets.find_browser_landmarks(firefox_snapshot())
        assert [tab["path"] for tab in data["tabs"]] == ["28.1.1", "28.1.5"]
        assert "Close tab" not in [tab["text"] for tab in data["tabs"]]

    def test_selected_tab_is_read_not_inferred(self):
        reads = []

        def selected(path):
            reads.append(path)
            return path == "28.1.5"

        data = browser_targets.find_browser_landmarks(firefox_snapshot(), selected)
        assert reads == ["28.1.1", "28.1.5"]
        assert data["selected_tab"]["path"] == "28.1.5"

    def test_a_bookmark_toolbar_is_listed_but_the_hidden_one_is_a_warning(self):
        snap = firefox_snapshot()
        snap.nodes.append(node("31", "ToolBar", automation_id="PersonalToolbar"))
        snap.nodes.extend([
            node("31.0", "Hyperlink", text="Docs"),
            node("32.0", "Hyperlink", text="Not in the toolbar"),
        ])
        data = browser_targets.find_browser_landmarks(snap)
        assert [item["text"] for item in data["bookmarks"]] == ["Docs"]

        hidden = browser_targets.find_browser_landmarks(firefox_snapshot())
        assert hidden["bookmarks"] == []
        assert any("Панель закладок" in warning for warning in hidden["warnings"])

    def test_an_unknown_tree_is_not_claimed_as_a_browser(self):
        snap = SimpleNamespace(nodes=[node("0", "Edit", automation_id="message",
                                           patterns=("Value",))])
        data = browser_targets.find_browser_landmarks(snap)
        assert data["browser"] == "unknown"
        assert data["address_bar"] is None
        assert data["tabs"] == []
        assert any("Адресная строка" in warning for warning in data["warnings"])

    def test_page_tab_widgets_are_not_misidentified_as_browser_tabs(self):
        snap = firefox_snapshot()
        snap.nodes = [n for n in snap.nodes if n.automation_id != "tabbrowser-tabs"]
        snap.nodes.extend([
            node("29.0.0", "Document", text="Page"),
            node("29.0.0.0", "TabItem", text="Page widget",
                 patterns=("SelectionItem",)),
        ])
        data = browser_targets.find_browser_landmarks(snap)
        assert data["tabs"] == []
        assert data["selected_tab"] is None

    def test_formatter_keeps_action_paths_and_selection_state(self):
        data = browser_targets.find_browser_landmarks(
            firefox_snapshot(), selected_reader=lambda path: path == "28.1.1",
            address_value="vk.ru/kostin_ak",
        )
        text = browser_targets.format_browser_landmarks(data)
        assert "path=30.5.1" in text
        assert "vk.ru/kostin_ak" in text
        assert "path=28.1.1 'First' [ТЕКУЩАЯ]" in text
        assert "path=28.1.1.0" not in text
