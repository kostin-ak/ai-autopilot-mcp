"""End-to-end demonstration against the local service-request form.

Run the form first, then pass its hwnd. The driver uses the same MCP functions an
agent would call: semantic snapshot, Class B field edits, selection, slider, submit,
and a final snapshot assertion. No browser, terminal, files or other application is
touched. allow_host_input is intentional here: the demo process is launched from this
workspace and therefore belongs to the agent's host tree; it is a dedicated toy form.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mcp_server  # noqa: E402


def node_path(snapshot: str, role: str, text: str) -> str:
    """Extracts the sibling-index path for one named role from a snapshot."""
    pattern = rf"^\s*(\d+(?:\.\d+)*)\s+\[{re.escape(role)}\]\s+'{re.escape(text)}'"
    match = re.search(pattern, snapshot, flags=re.MULTILINE)
    if not match:
        raise RuntimeError(f"snapshot node not found: {role} {text!r}")
    return match.group(1)


async def run(hwnd: int):
    snapshot = await mcp_server.get_snapshot(hwnd=hwnd, max_nodes=120, time_budget_ms=1000)
    paths = {
        "name": node_path(snapshot, "Edit", "Full name"),
        "email": node_path(snapshot, "Edit", "Email"),
        "notes": node_path(snapshot, "Edit", "Notes"),
        "urgency": node_path(snapshot, "Slider", "Urgency"),
        "notify": node_path(snapshot, "CheckBox", "Notify me by email"),
        "priority": node_path(snapshot, "List", "Priority"),
        "submit": node_path(snapshot, "Button", "Submit request"),
    }
    print("1. Semantic addresses resolved:", paths)

    writes = [
        ("set_value", paths["name"], {"text": "Александр Костин"}),
        ("set_value", paths["email"], {"text": "demo@example.com"}),
        ("set_value", paths["notes"], {"text": "Заявка создана автоматически через UIA."}),
        ("set_range_value", paths["urgency"], {"value": 7}),
        ("toggle", paths["notify"], {}),
    ]
    for verb, path, extra in writes:
        result = await mcp_server.act_on_element(
            verb, path, hwnd=hwnd, allow_host_input=True, **extra
        )
        print(f"2. {verb} {path}: {result.splitlines()[-1]}")

    print("3. Selecting High priority")
    high = node_path(snapshot, "ListItem", "High")
    print(await mcp_server.act_on_element("select", high, hwnd=hwnd,
                                          allow_host_input=True))

    print("4. Resizing and moving the window")
    await mcp_server.window_action("resize", hwnd=hwnd, width=700, height=560,
                                   allow_host_input=True)
    await mcp_server.window_action("move", hwnd=hwnd, x=820, y=150,
                                   allow_host_input=True)

    print("5. Submitting the request")
    await mcp_server.act_on_element("invoke", paths["submit"], hwnd=hwnd,
                                    allow_host_input=True)
    # Invoke has no state of its own. Wait for the form's own change notifications
    # to go quiet before asserting the status, otherwise the read races the Qt slot.
    await mcp_server.wait_for_event(quiet_ms=150, hwnd=hwnd, timeout_s=3.0)
    final = await mcp_server.get_snapshot(hwnd=hwnd, max_nodes=160, time_budget_ms=1000)
    status = next((line.strip() for line in final.splitlines()
                   if "Status: submitted" in line), None)
    if status is None:
        raise RuntimeError("submission was not observed in the semantic snapshot")
    print("6. OBSERVED RESULT:", status)
    print("7. HUD diagnostics:")
    print(await mcp_server.get_hud_state())


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("hwnd", type=int)
    args = parser.parse_args()
    try:
        await run(args.hwnd)
    finally:
        # A demo must leave the operator's desktop as it found it.
        print(await mcp_server.release_control())


if __name__ == "__main__":
    asyncio.run(main())
