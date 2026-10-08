"""Пометка аккаунтов аутричем списком: сопоставление введённых имён с аккаунтами."""

from __future__ import annotations

import re

from app.models import Account


def split_names(raw: str) -> list[str]:
    """Имена через запятую / точку с запятой / перенос строки."""
    return [p.strip() for p in re.split(r"[,;\n]+", raw or "") if p.strip()]


def match_accounts(
    raw: str, accounts: list[Account]
) -> tuple[list[Account], list[str]]:
    """
    Найти аккаунты по метке, @username, телефону или #id (регистр не важен).
    Возвращает (найденные без дублей, не найденные имена).
    """
    found: dict[int, Account] = {}
    missing: list[str] = []
    for name in split_names(raw):
        key = name.lstrip("@#").casefold()
        hit = [
            a
            for a in accounts
            if key
            and (
                a.label.casefold() == key
                or (a.username or "").casefold() == key
                or (a.phone or "").lstrip("+") == key.lstrip("+")
                or str(a.id) == key
            )
        ]
        if not hit:
            missing.append(name)
        for a in hit:
            found[a.id] = a
    return list(found.values()), missing
