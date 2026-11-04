"""Risk classification for actions, per ADR-0004.

Three levels: reversible, medium, irreversible. The point of the split is not to
label actions for a report -- it is that batch execution may only contain
reversible steps and must stop at the first non-reversible one, so the
classification is what makes batching safe to offer at all.

The decision that shapes everything here
----------------------------------------
The level comes from the element the identifier actually resolved to, not from the
intent the model stated. A mis-bind is the single most likely way this system does
damage: the model asks for the Cancel button, the path resolves one element over to
Send, and every downstream check that trusted the stated intent passes. Reading the
resolved control's own name is the only check that can catch it, because it is the
only input that reflects what is actually about to be acted on.

This is also why classification runs after target resolution and inside the daemon.
The gateway does not know what a path resolved to.

Matching
--------
Word boundaries rather than substrings, and the two scripts need different rules.
Latin labels are closed on both sides, with an optional plural: a substring rule for
"drop" classifies every Dropdown as irreversible, and one for "send" catches
"Sender". Cyrillic uses a stem prefix with an open right edge, because UI labels
vary between infinitive and noun forms ("Отправить" / "Отправка" / "Отправит") and an
exact-word list would miss most of them.

The cost of that asymmetry is a known over-match on Cyrillic: the stem "удал" also
fires on "Удалённые файлы", a folder name that is mere navigation. It is kept,
because the failure is in the safe direction -- an over-classified action is
refused or kept out of a batch, while an under-classified one is executed -- and
because the only way to separate them is to carry a lexicon of every noun form in
every application. The over-match is pinned by a test so it stays a known limit
rather than a surprise.

Policy may tighten a level but never loosen it. That is enforced structurally by
taking the maximum of the contributions rather than letting a later rule overwrite
an earlier one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

REVERSIBLE = "reversible"
MEDIUM = "medium"
IRREVERSIBLE = "irreversible"

_ORDER = {REVERSIBLE: 0, MEDIUM: 1, IRREVERSIBLE: 2}


def tighter(a: str, b: str) -> str:
    """The stricter of two levels. Policy tightens; it never loosens."""
    return a if _ORDER.get(a, 0) >= _ORDER.get(b, 0) else b


# Stems that mean something cannot be taken back. Matched with a word boundary at
# the left so "Отправить" does not match inside an unrelated word, and an open right
# edge so the infinitive and the noun form both land.
IRREVERSIBLE_STEMS = (
    # Russian
    "отправ", "удал", "стереть", "уничтож", "оплат", "куп", "заказ",
    "опубликов", "публикац", "разместить", "отчисл", "перевест", "перевод",
    "форматиров", "очист", "сброс", "отозв", "аннулир", "расторг",
    "подтверд", "соглас", "принять условия", "заверш", "увол",
    # English
    "send", "delete", "remove", "purchase", "buy", "submit", "publish",
    "format", "uninstall", "drop", "destroy", "wipe", "terminate", "revoke",
    "transfer", "pay", "checkout", "place order", "confirm",
)

# Stems that mean navigation or lookup: no state changes, or changes that are
# trivially undone. These can lower a level only for verbs that are themselves
# neutral; they never rescue a destructive stem on the same control.
REVERSIBLE_STEMS = (
    "поиск", "найти", "фильтр", "сортиров", "адресн", "перейт", "открыт",
    "закрыть меню", "назад", "вперёд", "меню", "вкладк", "разверн", "сверн",
    "search", "find", "filter", "sort", "address bar", "navigate", "back",
    "forward", "menu", "tab", "expand", "collapse", "scroll", "view",
    "отмена", "cancel", "закрыть", "close", "настройк", "settings",
)

# Verbs whose effect is bounded and undone by doing the opposite.
REVERSIBLE_VERBS = frozenset({
    "select", "expand", "collapse", "scroll_into_view", "focus",
    "set_window_state", "move_window", "resize_window",
})

# Verbs that write state. Cancelable in principle, not in fact once submitted.
MEDIUM_VERBS = frozenset({"set_value", "set_range_value", "toggle", "write_terminal"})

# Keys that commit whatever is in a field. In a messenger this is the send action,
# which is exactly the harm ADR-0004 was written about.
COMMIT_KEYS = frozenset({"enter", "return"})


def _is_cyrillic(text: str) -> bool:
    return any("\u0400" <= ch <= "\u04ff" for ch in text)


def _compile(stems):
    """One pattern per stem, closed on the right for Latin and open for Cyrillic.

    Latin labels are finite in form ("Send", "Sends"), so closing the right edge is
    what keeps "Sender" and "Dropdown" out. Cyrillic labels are not: the same stem
    appears as an infinitive, a noun and a third-person form, so the right edge stays
    open and the over-match it causes is documented at the top of this module.
    """
    out = []
    for stem in stems:
        escaped = re.escape(stem)
        left = r"(?<![\w-])"
        if _is_cyrillic(stem) or " " in stem:
            out.append(re.compile(left + escaped, re.IGNORECASE))
        else:
            # The right edge rejects a hyphen as well as a word character, because
            # UI labels hyphenate compounds: "Drop-down list" is a widget, not a
            # DROP TABLE. Word characters alone would let it through.
            out.append(re.compile(left + escaped + r"(?:s|es)?(?![\w-])",
                                  re.IGNORECASE))
    return out


_IRREVERSIBLE_RE = _compile(IRREVERSIBLE_STEMS)
_REVERSIBLE_RE = _compile(REVERSIBLE_STEMS)


@dataclass
class Risk:
    """A classified action, with the reason it got that level.

    ``reason`` is not decoration. An escalation the operator cannot explain is an
    escalation they will disable, so every level carries the rule that produced it.
    """

    level: str
    reason: str
    verb: str = ""
    control: str = ""
    app: str = ""
    critical_app: bool = False

    @property
    def batchable(self) -> bool:
        """Only reversible steps may appear in a batch (ADR-0004)."""
        return self.level == REVERSIBLE


def _match(stem_patterns, text: str) -> str | None:
    if not text:
        return None
    for pat in stem_patterns:
        m = pat.search(text)
        if m:
            return m.group(0)
    return None


def classify_action(verb: str, control_name: str = "", control_role: str = "",
                    app: str = "", critical_apps: tuple[str, ...] = ()) -> Risk:
    """Levels one element action from the resolved control, not from stated intent.

    ``control_name`` must be the Name of the element the path resolved to. Passing
    the label the model asked for instead defeats the only check that can catch a
    mis-bind.
    """
    verb = (verb or "").strip().lower()
    app_l = (app or "").lower()
    critical = any(tok and tok in app_l for tok in critical_apps)

    # Start from the verb: what kind of change is this at all.
    if verb in REVERSIBLE_VERBS:
        level, reason = REVERSIBLE, f"глагол {verb!r} обратим"
    elif verb in MEDIUM_VERBS:
        level, reason = MEDIUM, f"глагол {verb!r} изменяет состояние"
    elif verb == "invoke":
        # A press with no readable state. Its level is decided entirely by what is
        # being pressed, so it starts at the middle and the name moves it.
        level, reason = MEDIUM, "invoke: эффект зависит от нажимаемого элемента"
    elif verb in ("close", "close_window"):
        level, reason = MEDIUM, "закрытие окна может потерять несохранённое"
    else:
        level, reason = MEDIUM, f"неизвестный глагол {verb!r}, принят средний уровень"

    # The resolved control's own name can only tighten.
    hit = _match(_IRREVERSIBLE_RE, control_name)
    if hit:
        level = tighter(level, IRREVERSIBLE)
        reason = f"элемент {control_name!r} содержит {hit!r} — необратимое действие"

    if critical:
        # In a messenger or mail client a neutral press can still commit whatever is
        # in the composer, so nothing here is treated as freely repeatable.
        level = tighter(level, MEDIUM)
        reason += f"; приложение {app!r} в списке критичных"

    return Risk(level=level, reason=reason, verb=verb, control=control_name,
                app=app, critical_app=critical)


def classify_hotkey(hotkey: str, app: str = "",
                    critical_apps: tuple[str, ...] = ()) -> Risk:
    """Levels a key combination.

    Enter is the interesting case: on its own it is navigation, but in a critical
    application it is the send action, and the harm ADR-0004 describes is precisely
    text typed into the wrong chat followed by Enter.
    """
    keys = {k.strip().lower() for k in (hotkey or "").replace("+", " ").split() if k.strip()}
    app_l = (app or "").lower()
    critical = any(tok and tok in app_l for tok in critical_apps)

    if keys & COMMIT_KEYS:
        if critical:
            return Risk(IRREVERSIBLE,
                        f"Enter в критичном приложении {app!r} отправляет содержимое поля",
                        verb=hotkey, app=app, critical_app=True)
        return Risk(MEDIUM, "Enter подтверждает или отправляет содержимое поля",
                    verb=hotkey, app=app)
    if keys & {"f4"} and "alt" in keys:
        return Risk(MEDIUM, "Alt+F4 закрывает окно и может потерять несохранённое",
                    verb=hotkey, app=app)
    if "delete" in keys or ("d" in keys and "ctrl" in keys and "shift" in keys):
        return Risk(IRREVERSIBLE, "Delete уничтожает выделенное",
                    verb=hotkey, app=app)
    return Risk(REVERSIBLE, "комбинация не подтверждает и не уничтожает состояние",
                verb=hotkey, app=app)


def classify_click(app: str = "", critical_apps: tuple[str, ...] = ()) -> Risk:
    """Levels a coordinate click, which carries no element semantics at all.

    A click by coordinate is the one action whose target is not resolved to a
    control before it happens, so its level cannot come from a name. It is medium by
    default, and medium in a critical application too: the coordinate may land on a
    send button, and nothing in the request says otherwise.
    """
    app_l = (app or "").lower()
    critical = any(tok and tok in app_l for tok in critical_apps)
    reason = "клик по координате: семантика элемента не разрешается до нажатия"
    if critical:
        reason += f"; приложение {app!r} в списке критичных"
    return Risk(MEDIUM, reason, verb="click", app=app, critical_app=critical)
