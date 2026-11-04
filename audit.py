"""Append-only JSONL trace of every action, its target and its verification.

ADR-0004 requires escalation to be auditable. Until now there was no file log at
all: logging.basicConfig writes to stdout, and start_daemon.bat closes the console,
so the only record of what the agent did to the desktop was whatever the daemon
happened to return in a response. That is not an audit trail -- it is a transcript
of the parts that were reported.

Three properties matter more than field coverage:

Writing must never break an action. A trace that raises on a full disk or a locked
file would turn a bookkeeping failure into a failed click, and the operator would
learn about it as an unexplained refusal. Every write is wrapped and every failure
is counted rather than propagated.

Writing must be cheap enough to sit on every action. One formatted line and one
buffered append; no fsync per record, because an action already costs tens of
milliseconds and the trace must not be the reason it costs twice that.

It must be bounded. An unbounded log on a machine left running for weeks is a disk
fill waiting to happen, so records rotate by size and only a fixed number of
generations are kept.

Frame hashes
------------
ADR-0004 asks for the frame before and after. The "after" hash is free: mutating
actions already capture a frame to return. The "before" hash is not -- it needs a
second capture, roughly doubling the cost of every action. So it is recorded only
when TRACE_CAPTURE_BEFORE is on, and a record says explicitly which of the two it
carries rather than leaving a null to be read as "no frame existed".
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("audit")

# Action classes. Kept here rather than in the daemon because the trace is what
# makes the distinction visible after the fact, and a class string invented in two
# places eventually disagrees.
CLASS_A = "A"  # consumes the session cursor or keyboard
CLASS_B = "B"  # pattern or message based; does not touch the input queue


def frame_hash(data: bytes | None) -> str | None:
    """Short stable digest of a frame, or None when there was no frame.

    Truncated because the hash is an identifier for correlating records, not a
    security boundary: 16 hex characters is 64 bits, far past accidental collision
    for a single session's worth of frames.
    """
    if not data:
        return None
    return hashlib.sha256(data).hexdigest()[:16]


@dataclass
class TraceRecord:
    """One action, as it will appear in the log.

    ``verified`` is the tri-state the action layer already uses: True and False mean
    the effect was read back and did or did not match, None means the action has no
    readable state and success is only "the call was accepted". Flattening that to a
    boolean here would destroy the one distinction the trace exists to preserve.
    """

    action: str
    kind: str = ""
    target: str = ""
    element: str = ""
    level: str = ""
    preset: str = ""
    klass: str = ""
    ok: bool | None = None
    verified: bool | None = None
    status: str = ""
    message: str = ""
    frame_before: str | None = None
    frame_after: str | None = None
    elapsed_ms: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self, seq: int) -> str:
        payload: dict[str, Any] = {
            "ts": round(time.time(), 3),
            "seq": seq,
            "action": self.action,
        }
        for name in ("kind", "target", "element", "level", "preset", "klass",
                     "status", "message", "frame_before", "frame_after"):
            value = getattr(self, name)
            if value not in ("", None):
                payload[name] = value
        if self.ok is not None:
            payload["ok"] = self.ok
        # None is meaningful here and must survive: it is the difference between
        # "not verified" and "unverifiable".
        payload["verified"] = self.verified
        if self.elapsed_ms is not None:
            payload["elapsed_ms"] = round(self.elapsed_ms, 1)
        if self.extra:
            payload.update(self.extra)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                          default=str)


class AuditTrail:
    """Size-rotated append-only JSONL writer.

    One lock for the whole writer: actions are already serialized by the daemon's
    input lock, so contention is not the concern, and an interleaved partial line
    would corrupt the file for every reader after it.
    """

    def __init__(self, path: Path | str, max_bytes: int = 8 * 1024 * 1024,
                 keep: int = 5, enabled: bool = True) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.keep = max(1, keep)
        self.enabled = enabled
        self._lock = threading.Lock()
        self._seq = 0
        self.write_failures = 0
        self.records = 0

    def record(self, rec: TraceRecord) -> None:
        """Appends one record. Never raises."""
        if not self.enabled:
            return
        with self._lock:
            self._seq += 1
            line = rec.to_json(self._seq)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate_if_needed(len(line) + 1)
                with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(line + "\n")
                self.records += 1
            except OSError as exc:
                # Counted, not raised: a full or locked disk must not fail a click.
                self.write_failures += 1
                if self.write_failures <= 3:
                    logger.warning("audit write failed (%d so far): %s",
                                   self.write_failures, exc)

    def _rotate_if_needed(self, incoming: int) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size + incoming <= self.max_bytes:
            return
        # Oldest generation is dropped rather than kept forever; the point of the
        # bound is that a machine left running for weeks cannot fill a disk.
        oldest = self.path.with_suffix(f".{self.keep - 1}.jsonl")
        try:
            if oldest.exists():
                oldest.unlink()
        except OSError:
            pass
        for i in range(self.keep - 2, -1, -1):
            src = self.path if i == 0 else self.path.with_suffix(f".{i}.jsonl")
            dst = self.path.with_suffix(f".{i + 1}.jsonl")
            try:
                if src.exists():
                    os.replace(src, dst)
            except OSError:
                pass

    def read_back(self, limit: int = 50) -> list[dict[str, Any]]:
        """Recent records, oldest first. Used by diagnostics and by tests."""
        if not self.path.exists():
            return []
        try:
            with open(self.path, encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            return []
        out = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out
