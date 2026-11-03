"""OCR through Windows.Media.Ocr, for targets that expose no accessibility tree.

This is a fallback, not the primary path. The accessibility tree gives exact text,
exact roles and stable addresses; OCR gives glyphs with bounding boxes and no
semantics. It exists because some targets are genuinely opaque -- Explorer's content
pane, Java AWT applications, canvas surfaces -- and for those, pixels plus OCR are
the only route.

Two properties of Windows.Media.Ocr shape this module, and both were measured rather
than assumed.

The recognizer is language-specific, and a Russian engine renders Latin text with
Cyrillic homoglyphs. On a rendered sample, "OK" came back as "ОК" with Cyrillic О
and К, and "MARKER_ALPHA_4711" came back as "MARkER АЕРНА 4711" -- the underscore
dropped and ALPHA folded into Cyrillic. Naive substring matching therefore reports
"not found" for text that is plainly on screen, which is worse than failing loudly,
because an agent concludes the control does not exist. Matching here normalizes case,
folds homoglyphs and discards punctuation before comparing.

Recognition is fast: 760x260 in about 13 ms, and the spike measured a 400x200 region
at about 3 ms. A full 1280-wide frame is well within budget, so there is no reason to
restrict OCR to small regions for performance.

Coordinates returned are in the frame's own space -- the captured image is already
scaled to a fixed width -- so a match centre can be handed to mouse_action directly.
"""
from __future__ import annotations

import base64
import io
import time
from dataclasses import dataclass, field

# Cyrillic and other look-alikes folded to their Latin equivalents. The recognizer
# picks glyphs by language, not by what the pixels actually were, so a Latin label
# read by a Russian engine arrives as visually identical Cyrillic.
_HOMOGLYPHS = {
    "а": "a", "в": "b", "е": "e", "ё": "e", "к": "k", "м": "m", "н": "h",
    "о": "o", "р": "p", "с": "c", "т": "t", "у": "y", "х": "x", "і": "i",
    "ї": "i", "ѕ": "s", "ґ": "r", "ԁ": "d", "ԛ": "q", "ԝ": "w", "ɑ": "a",
    "ᴄ": "c", "ᴅ": "d", "ᴇ": "e", "ᴏ": "o", "ᴘ": "p", "ᴛ": "t", "ᴜ": "u",
    "ν": "v", "ο": "o", "ρ": "p", "τ": "t", "υ": "y", "α": "a", "ε": "e",
}


def normalize(text: str) -> str:
    """Casefolded, homoglyph-folded, alphanumeric-only form used for matching.

    Punctuation is discarded because the recognizer drops it inconsistently: an
    underscore inside a marker survived on one line and vanished on another. Keeping
    it would make matching depend on a detail OCR does not guarantee.
    """
    out = []
    for ch in text.casefold():
        folded = _HOMOGLYPHS.get(ch, ch)
        if folded.isalnum():
            out.append(folded)
    return "".join(out)


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """normalize() plus a map from each output character to its input index.

    The map is what makes a match locatable: without it a normalized hit could not be
    traced back to the words it came from, and so not to a bounding box.
    """
    out: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(text):
        folded = _HOMOGLYPHS.get(ch.casefold(), ch.casefold())
        if folded.isalnum():
            out.append(folded)
            idx.append(i)
    return "".join(out), idx


@dataclass(frozen=True)
class OcrWord:
    """One recognized word with its box in the coordinates it was recognized in."""

    text: str
    x: int
    y: int
    w: int
    h: int

    @property
    def center(self) -> tuple[int, int]:
        return (self.x + self.w // 2, self.y + self.h // 2)

    def scaled(self, ox: int, oy: int, sx: float, sy: float) -> OcrWord:
        return OcrWord(self.text, round((self.x - ox) * sx),
                       round((self.y - oy) * sy), max(round(self.w * sx), 1),
                       max(round(self.h * sy), 1))


@dataclass(frozen=True)
class OcrLine:
    """One recognized line: the text, its words, and the union of their boxes."""

    text: str
    words: tuple[OcrWord, ...]
    x: int
    y: int
    w: int
    h: int

    @property
    def center(self) -> tuple[int, int]:
        return (self.x + self.w // 2, self.y + self.h // 2)

    def scaled(self, ox: int, oy: int, sx: float, sy: float) -> OcrLine:
        return OcrLine(self.text, tuple(w.scaled(ox, oy, sx, sy) for w in self.words),
                       round((self.x - ox) * sx), round((self.y - oy) * sy),
                       max(round(self.w * sx), 1), max(round(self.h * sy), 1))


@dataclass
class OcrPage:
    """Result of recognizing one image."""

    lines: list[OcrLine] = field(default_factory=list)
    text: str = ""
    language: str = ""
    elapsed_ms: float = 0.0
    width: int = 0
    height: int = 0
    error: str | None = None

    def scaled(self, ox: int, oy: int, sx: float, sy: float) -> OcrPage:
        """A copy with every coordinate mapped by ``(p - origin) * scale``.

        Recognition runs on a native-resolution capture, because the frame sent to a
        model is scaled down and that costs OCR roughly half its characters: on a
        1920x1080 monitor native recognition returned 76 lines and 2555 characters
        while the 1280-wide frame returned 51 lines and 1358, with visible
        corruption. The caller still acts in frame space, so the boxes are converted
        here rather than per match.
        """
        return OcrPage(
            lines=[ln.scaled(ox, oy, sx, sy) for ln in self.lines],
            text=self.text, language=self.language, elapsed_ms=self.elapsed_ms,
            width=round(self.width * sx), height=round(self.height * sy),
            error=self.error,
        )

    def find(self, needle: str, *, fuzzy: bool = True) -> list[OcrMatch]:
        """Locates every occurrence of needle, word-level first.

        A whole-word match yields that word's box, which is the tightest and most
        useful target for a click. A match spanning part of a line yields the union
        of the words it covers. ``fuzzy`` controls homoglyph and punctuation folding;
        turning it off asks for an exact casefolded substring, which is rarely what
        you want from OCR but is sometimes what you need to prove.
        """
        if not needle.strip():
            return []
        want = normalize(needle) if fuzzy else needle.strip().casefold()
        if not want:
            return []

        matches: list[OcrMatch] = []
        for line in self.lines:
            for word in line.words:
                got = normalize(word.text) if fuzzy else word.text.casefold()
                if got == want:
                    matches.append(OcrMatch(
                        needle=needle, text=word.text, kind="word",
                        x=word.x, y=word.y, w=word.w, h=word.h,
                        line_text=line.text,
                    ))
            # Span matches inside the line, so a multi-word needle still resolves.
            for m in _spans_in_line(line, want, fuzzy, needle):
                if not any(x.text == m.text and x.x == m.x and x.y == m.y
                           for x in matches):
                    matches.append(m)
        return matches


@dataclass(frozen=True)
class OcrMatch:
    """A located occurrence of the requested text."""

    needle: str
    text: str
    kind: str  # "word" | "span" | "line"
    x: int
    y: int
    w: int
    h: int
    line_text: str = ""

    @property
    def center(self) -> tuple[int, int]:
        return (self.x + self.w // 2, self.y + self.h // 2)


def _spans_in_line(line: OcrLine, want: str, fuzzy: bool,
                   needle: str) -> list[OcrMatch]:
    """Finds needle occurrences that span words, and maps them back to boxes."""
    hay, to_src = _normalize_with_map(line.text) if fuzzy else (
        line.text.casefold(), list(range(len(line.text))))
    if not hay or want not in hay:
        return []

    # Character range of each word within the line text, so a normalized hit can be
    # attributed to the words that produced it.
    word_spans: list[tuple[int, int, OcrWord]] = []
    cursor = 0
    for word in line.words:
        start = line.text.find(word.text, cursor)
        if start < 0:
            start = cursor
        word_spans.append((start, start + len(word.text), word))
        cursor = start + len(word.text)

    out: list[OcrMatch] = []
    at = hay.find(want)
    while at >= 0:
        src_start = to_src[at] if at < len(to_src) else 0
        src_end = (to_src[at + len(want) - 1] + 1
                   if at + len(want) - 1 < len(to_src) else len(line.text))
        covered = [w for (a, b, w) in word_spans if a < src_end and b > src_start]
        if covered:
            x = min(w.x for w in covered)
            y = min(w.y for w in covered)
            right = max(w.x + w.w for w in covered)
            bottom = max(w.y + w.h for w in covered)
            out.append(OcrMatch(
                needle=needle, text=" ".join(w.text for w in covered),
                kind="span" if len(covered) > 1 else "line",
                x=x, y=y, w=right - x, h=bottom - y, line_text=line.text,
            ))
        at = hay.find(want, at + 1)
    return out


class OcrReader:
    """Recognizes text in captured frames.

    The engine is created once and reused; creation involves a language lookup that
    is not worth repeating per frame. Recognition itself is async in the Windows API,
    so these methods are coroutines and belong on the daemon's event loop.
    """

    def __init__(self, language: str | None = None) -> None:
        self._language = language
        self._engine = None
        self._engine_lang = ""

    @property
    def available_languages(self) -> list[str]:
        try:
            from winrt.windows.media.ocr import OcrEngine

            return [str(x.language_tag) for x in
                    OcrEngine.available_recognizer_languages]
        except Exception:
            return []

    def _get_engine(self):
        if self._engine is not None:
            return self._engine
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine

        engine = None
        for tag in self._candidate_languages():
            try:
                engine = OcrEngine.try_create_from_language(Language(tag))
            except Exception:
                engine = None
            if engine is not None:
                self._engine_lang = tag
                break
        if engine is None:
            engine = OcrEngine.try_create_from_user_profile_languages()
            self._engine_lang = "user-profile"
        if engine is None:
            raise RuntimeError(
                "Windows.Media.Ocr не смог создать распознаватель ни для одного "
                f"языка из {self._candidate_languages()}. Доступные: "
                f"{self.available_languages}"
            )
        self._engine = engine
        return engine

    def _candidate_languages(self) -> list[str]:
        """Preferred order, restricted to what the system actually offers.

        Asking for a language that is not installed returns None rather than raising,
        so the list has to be intersected with availability or every attempt silently
        fails and the caller cannot tell why.
        """
        wanted = [self._language] if self._language else ["ru", "en-US", "en"]
        available = self.available_languages
        ordered = [w for w in wanted if w in available]
        # Fall back to any available language rather than to nothing.
        ordered += [a for a in available if a not in ordered]
        return ordered or wanted

    async def recognize_image(self, image) -> OcrPage:
        """Recognizes a PIL image. Coordinates come back in the image's own space."""
        t0 = time.perf_counter()
        try:
            engine = self._get_engine()
        except Exception as exc:
            return OcrPage(error=f"{type(exc).__name__}: {exc}",
                           elapsed_ms=(time.perf_counter() - t0) * 1000)

        try:
            from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
            from winrt.windows.storage.streams import DataWriter

            rgba = image.convert("RGBA")
            width, height = rgba.size
            raw = rgba.tobytes()
            writer = DataWriter()
            writer.write_bytes(raw)
            bitmap = SoftwareBitmap.create_copy_from_buffer(
                writer.detach_buffer(), BitmapPixelFormat.BGRA8, width, height)
        except Exception as exc:
            return OcrPage(
                error=f"Не удалось подготовить изображение: {type(exc).__name__}: {exc}",
                elapsed_ms=(time.perf_counter() - t0) * 1000,
                width=image.width if hasattr(image, "width") else 0,
                height=image.height if hasattr(image, "height") else 0,
            )

        try:
            result = await engine.recognize_async(bitmap)
        except Exception as exc:
            return OcrPage(
                error=f"Распознавание не удалось: {type(exc).__name__}: {exc}",
                elapsed_ms=(time.perf_counter() - t0) * 1000,
                width=width, height=height,
            )

        lines: list[OcrLine] = []
        for raw_line in result.lines:
            words = []
            for raw_word in raw_line.words:
                r = raw_word.bounding_rect
                words.append(OcrWord(
                    text=str(raw_word.text),
                    x=int(r.x), y=int(r.y), w=int(r.width), h=int(r.height)))
            if not words:
                continue
            x = min(w.x for w in words)
            y = min(w.y for w in words)
            right = max(w.x + w.w for w in words)
            bottom = max(w.y + w.h for w in words)
            lines.append(OcrLine(
                text=str(raw_line.text), words=tuple(words),
                x=x, y=y, w=right - x, h=bottom - y))

        return OcrPage(
            lines=lines,
            text=str(result.text),
            language=self._engine_lang,
            elapsed_ms=(time.perf_counter() - t0) * 1000,
            width=width, height=height,
        )

    async def recognize_base64(self, payload: str) -> OcrPage:
        """Recognizes a base64-encoded image, as capture_screen produces."""
        from PIL import Image

        try:
            image = Image.open(io.BytesIO(base64.b64decode(payload)))
            image.load()
        except Exception as exc:
            return OcrPage(error=f"Не удалось декодировать изображение: "
                                 f"{type(exc).__name__}: {exc}")
        return await self.recognize_image(image)
