"""Packaging contract: an installed wheel must contain every runtime module.

Editable installs import from the repository root, so a missing ``py-modules``
entry is invisible to the test suite. The wheel check caught exactly that: the
first build omitted the safety, event, cursor and virtual-desktop modules.
"""
from __future__ import annotations

import tomllib
from pathlib import Path


def test_every_root_runtime_module_is_declared_for_packaging():
    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    declared = set(config["tool"]["setuptools"]["py-modules"])
    present = {path.stem for path in root.glob("*.py")}
    assert declared == present, (
        f"packaging mismatch: undeclared={sorted(present - declared)}, "
        f"declared but absent={sorted(declared - present)}"
    )
