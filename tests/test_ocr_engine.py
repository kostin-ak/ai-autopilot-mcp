"""Matching logic for OCR output, tested without an OCR runtime.

The recognizer is the unreliable part and cannot be a unit-test gate, but the
matching rules can be, and they are where the real risk sits: a Russian engine reads
Latin labels as Cyrillic homoglyphs and drops punctuation inconsistently, so naive
substring matching reports "not found" for text that is plainly on screen. These
tests use hand-built pages so the rules are checked deterministically.
"""
from __future__ import annotations

import pytest

import ocr_engine as oe


def word(text, x, y, w=40, h=16):
    return oe.OcrWord(text=text, x=x, y=y, w=w, h=h)


def line(text, words):
    x = min(w.x for w in words)
    y = min(w.y for w in words)
    right = max(w.x + w.w for w in words)
    bottom = max(w.y + w.h for w in words)
    return oe.OcrLine(text=text, words=tuple(words), x=x, y=y,
                      w=right - x, h=bottom - y)


def page(*lines):
    return oe.OcrPage(lines=list(lines), text="\n".join(ln.text for ln in lines),
                      language="ru", elapsed_ms=1.0, width=1280, height=720)


# ------------------------------------------------------------------ normalize
@pytest.mark.parametrize("raw,expected", [
    ("OK", "ok"),
    ("ОК", "ok"),               # Cyrillic О and К, what a ru engine returns
    ("Cancel", "cancel"),
    ("MARKER_4711", "marker4711"),
    ("MARkER 4711", "marker4711"),
    ("Save  changes", "savechanges"),
    ("", ""),
    ("___", ""),
])
def test_normalize(raw, expected):
    assert oe.normalize(raw) == expected


def test_normalize_folds_the_homoglyphs_that_ocr_actually_produces():
    """Measured output from a ru engine on Latin text.

    The look-alikes are per character: Latin C reads as Cyrillic С (U+0421), not as
    Н. An earlier version of this test used the wrong substitution and failed, which
    is worth keeping visible -- guessing homoglyphs is how this table goes wrong.
    """
    for cyr, lat in (("ОК", "OK"), ("Саncel", "Cancel"), ("Аррlу", "Apply")):
        assert oe.normalize(cyr) == oe.normalize(lat), f"{cyr!r} vs {lat!r}"


def test_normalize_is_not_human_readable_and_that_is_fine():
    """Folding mangles genuine Cyrillic into Latin look-alikes.

    "Сохранить" does not survive as itself. That is acceptable because both sides of
    a comparison are folded identically, so a Cyrillic needle still matches Cyrillic
    screen text. The normalized form is an internal key, never something to show.
    """
    assert oe.normalize("Сохранить") != "сохранить"
    assert oe.normalize("Сохранить") == oe.normalize("Сохранить")
    page_ = page(line("Сохранить изменения", [word("Сохранить", 21, 75)]))
    assert page_.find("Сохранить"), "a Cyrillic needle must match Cyrillic text"


def test_folding_creates_no_false_matches_between_different_words():
    """Collisions require visually identical strings, which is the intended case."""
    assert oe.normalize("сок") == oe.normalize("cok"), "visually identical"
    assert oe.normalize("сок") != oe.normalize("сух"), "visually different"
    assert oe.normalize("файл") != oe.normalize("file"), "different word"


def test_normalize_with_map_tracks_source_positions():
    hay, to_src = oe._normalize_with_map("A_b C")
    assert hay == "abc"
    assert to_src == [0, 2, 4]


# ----------------------------------------------------------------------- find
def test_finds_a_word_despite_homoglyphs():
    """'OK' rendered by a ru engine comes back as Cyrillic 'ОК'."""
    p = page(line("ОК", [word("ОК", 21, 125, 36, 19)]))
    hits = p.find("OK")
    assert len(hits) == 1
    assert hits[0].kind == "word"
    assert hits[0].center == (21 + 18, 125 + 9)


def test_finds_a_word_despite_dropped_underscore():
    """The recognizer kept the underscore in one place and dropped it in another."""
    p = page(line("MARkER 4711", [word("MARkER", 22, 25, 108, 19),
                                   word("4711", 243, 25, 52, 19)]))
    assert p.find("MARKER_4711")
    assert p.find("4711")
    assert p.find("marker")


def test_finds_cyrillic_needle_in_cyrillic_text():
    p = page(line("Сохранить изменения",
                  [word("Сохранить", 21, 75, 127, 24),
                   word("изменения", 157, 80, 128, 14)]))
    hits = p.find("Сохранить")
    assert len(hits) == 1
    assert hits[0].text == "Сохранить"


def test_multiword_needle_unions_the_word_boxes():
    words = [word("Сохранить", 21, 75, 127, 24), word("изменения", 157, 80, 128, 14)]
    p = page(line("Сохранить изменения", words))
    hits = p.find("Сохранить изменения")
    span_hits = [h for h in hits if h.kind in ("span", "line")]
    assert span_hits, f"expected a span match, got {[h.kind for h in hits]}"
    m = span_hits[0]
    assert m.x == 21 and m.w == (157 + 128) - 21
    assert m.text == "Сохранить изменения"


def test_absent_text_is_not_found():
    p = page(line("ОК Cancel Apply",
                  [word("ОК", 21, 125), word("Cancel", 72, 125),
                   word("Apply", 165, 125)]))
    assert p.find("НЕТ_ТАКОГО") == []


def test_empty_needle_returns_nothing():
    p = page(line("ОК", [word("ОК", 0, 0)]))
    assert p.find("") == []
    assert p.find("   ") == []
    assert p.find("_") == [], "a needle that normalizes to empty must not match all"


def test_every_occurrence_is_returned():
    p = page(
        line("ОК ОК", [word("ОК", 10, 10), word("ОК", 90, 10)]),
        line("ОК", [word("ОК", 10, 50)]),
    )
    hits = [h for h in p.find("OK") if h.kind == "word"]
    assert len(hits) == 3
    assert len({h.center for h in hits}) == 3, "each occurrence needs its own box"


def test_exact_mode_does_not_fold_homoglyphs():
    """fuzzy=False is for proving what the recognizer literally returned."""
    p = page(line("ОК", [word("ОК", 21, 125)]))
    assert p.find("OK", fuzzy=False) == []
    assert p.find("ОК", fuzzy=False)


def test_match_carries_the_line_for_context():
    p = page(line("ОК Cancel Apply", [word("Cancel", 72, 125, 77, 19)]))
    hits = p.find("Cancel")
    assert hits[0].line_text == "ОК Cancel Apply"
    assert hits[0].needle == "Cancel"


# ------------------------------------------------------------------- geometry
def test_scaled_maps_boxes_into_frame_space():
    """Recognition runs on a monitor-relative native image, so scaling uses origin
    (0, 0). A live run that subtracted the monitor's desktop offset produced x=1400
    and y=-153 inside a 1280x720 frame; a bounds check caught it, and comparing
    against read_screen_text did not, because both used the same wrong offset."""
    p = page(line("x", [word("x", 180, 121, 60, 20)]))
    scaled = p.scaled(0, 0, 1280 / 1920, 720 / 1080)
    w = scaled.lines[0].words[0]
    assert (w.x, w.y) == (120, 81)
    assert w.w == 40 and w.h == 13
    assert 0 <= scaled.lines[0].center[0] < 1280
    assert 0 <= scaled.lines[0].center[1] < 720


def test_scaled_keeps_text_and_reports_new_dimensions():
    p = page(line("Сохранить", [word("Сохранить", 100, 200, 300, 30)]))
    scaled = p.scaled(0, 0, 0.5, 0.5)
    assert scaled.lines[0].text == "Сохранить"
    assert scaled.width == p.width // 2
    assert scaled.height == p.height // 2


def test_scaled_never_collapses_a_box_to_zero_size():
    """A one-pixel box must survive heavy downscaling, or a match becomes unclickable."""
    p = page(line("i", [word("i", 5, 5, 1, 1)]))
    w = p.scaled(0, 0, 0.1, 0.1).lines[0].words[0]
    assert w.w >= 1 and w.h >= 1


def test_word_center_is_the_box_centre():
    assert word("x", 100, 200, 40, 16).center == (120, 208)


def test_line_box_is_the_union_of_words():
    ln = line("a b", [word("a", 10, 20, 30, 12), word("b", 60, 22, 30, 12)])
    assert (ln.x, ln.y, ln.w, ln.h) == (10, 20, 80, 14)
    assert ln.center == (50, 27)


# ------------------------------------------------------------------- engine
def test_candidate_languages_are_restricted_to_what_is_installed():
    """Asking for an uninstalled language returns None rather than raising, so the
    preference list has to be intersected with availability or every attempt fails
    silently."""

    class Stub(oe.OcrReader):
        @property
        def available_languages(self):
            return ["en-US", "de"]

    # Default preference is ru then en-US; ru is not installed here, so en-US leads
    # and the remaining available language is appended as a last resort.
    assert Stub()._candidate_languages() == ["en-US", "de"]
    # A requested language that is not installed falls back to everything available.
    assert Stub("fr")._candidate_languages() == ["en-US", "de"]
    # A requested language that IS installed leads.
    assert Stub("de")._candidate_languages() == ["de", "en-US"]


def test_recognize_reports_an_error_page_instead_of_raising():
    """A caller that cannot OCR must learn that, not crash mid-task."""
    import asyncio

    reader = oe.OcrReader()

    class Bad:
        def convert(self, *_a, **_k):
            raise RuntimeError("not an image")

        width = 0
        height = 0

    result = asyncio.run(reader.recognize_image(Bad()))
    assert isinstance(result, oe.OcrPage)
    assert result.error
    assert result.lines == []
