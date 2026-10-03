"""Необязательный слой «ИИ»: короткий вывод по УЖЕ посчитанным фактам.

Модель ничего не считает и не придумывает: получает готовые числа и список действий,
возвращает 2–3 предложения «что главное». Нет ключа / ошибка сети — молча без вывода.
"""

from __future__ import annotations

import logging

import httpx

from app.config import get_settings

log = logging.getLogger("marketing.operator.llm")

SYSTEM = (
    "Ты оператор рассылок в Telegram. Тебе дали ТОЛЬКО проверенные факты. "
    "Напиши вывод на русском: 2–3 коротких предложения — что главное сегодня и что сделать "
    "в первую очередь. Используй только числа и названия из фактов, ничего не выдумывай и не "
    "оценивай «по ощущениям». Если данных мало — так и скажи. Без вступлений и списков."
)


async def summarize(facts: str, *, timeout: float = 25.0) -> str | None:
    s = get_settings()
    if not s.anthropic_api_key.strip():
        return None
    payload = {
        "model": s.operator_llm_model,
        "max_tokens": 300,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": facts[:6000]}],
    }
    headers = {
        "x-api-key": s.anthropic_api_key.strip(),
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post("https://api.anthropic.com/v1/messages", json=payload, headers=headers)
        if r.status_code != 200:
            log.info("llm status %s", r.status_code)
            return None
        parts = r.json().get("content") or []
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
        return text[:600] or None
    except Exception as e:  # noqa: BLE001
        log.info("llm failed: %s", e)
        return None
