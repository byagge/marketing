"""Разбор и решение капч при вступлении в чаты.

Поддерживает:
- текстовая математика («1+1=», «сколько будет 6×7»)
- quiz-опрос с примером в вопросе и вариантами-числами
- inline-кнопка подтверждения («Я не бот», и вариации с/без эмодзи)
- кнопки с числами = ответ на пример из текста сообщения
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

# 1+1=, 2 + 3 = ?, какой ответ в этом примере 6 + 8?
_MATH_RE = re.compile(
    r"(?:"
    r"(?:сколько\s+(?:будет\s+)?)?"
    r"(?:какой\s+ответ[^.?\n]{0,40}?)?"
    r"(?:пример[еу]?\s+)?"
    r"(\d{1,3})\s*([+\-*/×÷xх:])\s*(\d{1,3})"
    r"\s*(?:=|равно|=?\s*\?|\?)?"
    r")",
    re.IGNORECASE | re.DOTALL,
)

_OP = {
    "+": lambda a, b: a + b,
    "-": lambda a, b: a - b,
    "*": lambda a, b: a * b,
    "×": lambda a, b: a * b,
    "x": lambda a, b: a * b,
    "х": lambda a, b: a * b,
    "/": lambda a, b: a // b if b else None,
    "÷": lambda a, b: a // b if b else None,
    ":": lambda a, b: a // b if b else None,
}

# кнопка «я не бот» и близкие формулировки
_VERIFY_PHRASES = (
    "не бот",
    "не робот",
    "я человек",
    "я живой",
    "not a bot",
    "not bot",
    "i'm human",
    "im human",
    "i am human",
    "human",
    "verify",
    "verification",
    "подтверд",
    "подтвердить",
    "пройти проверку",
    "я не бот",
    "я не робот",
    "captcha",
    "anti-bot",
    "antibot",
)

_CAPTCHA_HINTS = (
    "нажмите",
    "нажми",
    "кнопк",
    "секунд",
    "чтобы писать",
    "иметь возможность",
    "проверк",
    "капч",
    "captcha",
    "verify",
    "не бот",
    "quiz",
    "пример",
    "ответ",
)


def fold_text(text: str) -> str:
    """Убрать эмодзи/fancy unicode → простой нижний регистр для матчинга."""
    if not text:
        return ""
    # NFKD разложит часть «математических» букв; остальное — как есть
    norm = unicodedata.normalize("NFKD", text)
    chars: list[str] = []
    for ch in norm:
        if unicodedata.category(ch) in {"Mn", "Me", "Cf"}:
            continue
        # выкинуть эмодзи и символы (So) кроме базовой пунктуации
        cat = unicodedata.category(ch)
        if cat == "So":
            continue
        chars.append(ch)
    out = "".join(chars).lower()
    out = re.sub(r"\s+", " ", out).strip()
    return out


def solve_math_captcha(text: str) -> str | None:
    """Вернуть ответ (строка-число) или None."""
    raw = (text or "").strip()
    if not raw or len(raw) > 500:
        return None
    # стилизованный текст тоже нормализуем
    folded = fold_text(raw)
    hay = raw if re.search(r"\d\s*[+\-*/×÷xх:]\s*\d", raw, re.I) else folded
    if not re.search(r"\d\s*[+\-*/×÷xх:]\s*\d", hay, re.IGNORECASE):
        return None
    match = _MATH_RE.search(hay) or _MATH_RE.search(raw)
    if not match:
        return None
    a, op, b = int(match.group(1)), match.group(2), int(match.group(3))
    fn = _OP.get(op)
    if not fn:
        return None
    result = fn(a, b)
    if result is None:
        return None
    return str(int(result))


def looks_like_captcha(text: str) -> bool:
    if solve_math_captcha(text) is not None:
        return True
    folded = fold_text(text)
    return any(h in folded for h in _CAPTCHA_HINTS)


def is_verify_button(label: str) -> bool:
    folded = fold_text(label)
    if not folded:
        return False
    return any(p in folded for p in _VERIFY_PHRASES)


def button_verify_score(label: str) -> int:
    """Чем выше — тем вероятнее кнопка подтверждения."""
    folded = fold_text(label)
    if not folded:
        return 0
    score = 0
    for p in _VERIFY_PHRASES:
        if p in folded:
            score += 10 + len(p)
    # одна короткая кнопка «ок» / «yes» рядом с текстом про капчу
    if folded in {"ok", "ок", "yes", "да", "готово", "continue", "далее"}:
        score += 3
    return score


def pick_numeric_option(options: list[str], answer: str) -> int | None:
    """Индекс варианта, совпадающего с числом-ответом."""
    want = str(answer).strip()
    for i, opt in enumerate(options):
        digits = re.sub(r"[^\d\-]", "", fold_text(opt) or opt)
        if digits == want:
            return i
    # мягкое сравнение: «14» внутри «= 14»
    for i, opt in enumerate(options):
        if re.search(rf"(?<!\d){re.escape(want)}(?!\d)", opt or ""):
            return i
    return None


def poll_question_text(poll: Any) -> str:
    q = getattr(poll, "question", None)
    if q is None:
        return ""
    if isinstance(q, str):
        return q
    # TextWithEntities
    return getattr(q, "text", None) or str(q)


def poll_option_texts(poll: Any) -> list[str]:
    out: list[str] = []
    for ans in getattr(poll, "answers", None) or []:
        text = getattr(ans, "text", None)
        if text is None:
            out.append("")
            continue
        if isinstance(text, str):
            out.append(text)
        else:
            out.append(getattr(text, "text", None) or str(text))
    return out


def solve_poll_captcha(poll: Any) -> tuple[int, str] | None:
    """
    Вернуть (index, answer_str) для quiz/poll с математикой.
    """
    if poll is None:
        return None
    question = poll_question_text(poll)
    answer = solve_math_captcha(question)
    if answer is None:
        return None
    options = poll_option_texts(poll)
    idx = pick_numeric_option(options, answer)
    if idx is None:
        return None
    return idx, answer


def extract_inline_buttons(message: Any) -> list[tuple[int, int, str, Any]]:
    """Список (row, col, label, button) из reply_markup."""
    markup = getattr(message, "reply_markup", None)
    if markup is None:
        return []
    rows = getattr(markup, "rows", None) or []
    out: list[tuple[int, int, str, Any]] = []
    for ri, row in enumerate(rows):
        buttons = getattr(row, "buttons", None) or []
        for ci, btn in enumerate(buttons):
            label = getattr(btn, "text", None) or ""
            out.append((ri, ci, label, btn))
    return out


def pick_verify_button(
    message: Any,
    *,
    math_answer: str | None = None,
) -> tuple[int, int, str] | None:
    """
    Выбрать кнопку для клика: (row, col, label).
    Приоритет: число = ответ → фразы «не бот» → единственная кнопка при тексте-капче.
    """
    buttons = extract_inline_buttons(message)
    if not buttons:
        return None

    text = getattr(message, "message", None) or getattr(message, "raw_text", None) or ""
    answer = math_answer or solve_math_captcha(text)

    if answer is not None:
        for ri, ci, label, _ in buttons:
            digits = re.sub(r"[^\d\-]", "", fold_text(label) or label)
            if digits == answer:
                return ri, ci, label

    scored = sorted(
        ((button_verify_score(label), ri, ci, label) for ri, ci, label, _ in buttons),
        reverse=True,
    )
    if scored and scored[0][0] > 0:
        _, ri, ci, label = scored[0]
        return ri, ci, label

    # одна кнопка + текст похож на капчу → жмём
    if len(buttons) == 1 and looks_like_captcha(text):
        ri, ci, label, _ = buttons[0]
        return ri, ci, label

    # 2–4 кнопки, все короткие числа, есть пример в тексте
    if answer is not None and 2 <= len(buttons) <= 6:
        numeric = []
        for ri, ci, label, _ in buttons:
            digits = re.sub(r"[^\d\-]", "", fold_text(label) or label)
            if digits.lstrip("-").isdigit():
                numeric.append((ri, ci, label, digits))
        if len(numeric) == len(buttons):
            for ri, ci, label, digits in numeric:
                if digits == answer:
                    return ri, ci, label

    return None
