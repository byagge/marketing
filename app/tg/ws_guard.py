"""Флоу @WsGuardBot: Mini App terms → Помощь → ссылка на чат.

Шаги (по реальному UI бота):
1. /start → кнопка «Условия пользования» (Mini App / WebView)
2. В Mini App: «I accept the terms»
3. Меню → «Помощь» → «Запросить ссылку»
4. «Получить ссылку на чат» → URL-кнопка «Перейти в канал» (часто через edit сообщения)
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from telethon import TelegramClient, events
from telethon.tl import functions, types

from app.utils.captcha import extract_inline_buttons, fold_text

log = logging.getLogger(__name__)

WS_GUARD_BOTS = frozenset({"wsguardbot", "ws_guard_bot", "wsgarantbot"})

_TERMS_BTN = (
    "услови",
    "terms",
    "пользован",
    "соглашен",
)
_HELP_BTN = ("помощь", "help", "справк")
_REQUEST_LINK_BTN = (
    "запросить ссылку",
    "request link",
)
_GET_LINK_BTN = (
    "получить ссылку на чат",
    "получить ссылку",
    "get chat link",
    "get link",
    "ссылку на чат",
)
_OPEN_CHAT_BTN = (
    "перейти в канал",
    "перейти в чат",
    "открыть чат",
    "вступить",
    "join",
    "go to",
)
_ACCEPT_API_PATHS = (
    "/api/accept",
    "/api/terms/accept",
    "/api/terms",
    "/accept",
    "/terms/accept",
    "/api/v1/accept",
    "/api/webview/accept",
    "/api/user/accept",
    "/api/agreement/accept",
)


def is_ws_guard_bot(username: str) -> bool:
    raw = (username or "").strip().lstrip("@").casefold()
    if not raw:
        return False
    if raw in WS_GUARD_BOTS:
        return True
    return "wsguard" in raw or (raw.startswith("ws") and "guard" in raw)


def _btn_label(btn: Any) -> str:
    return getattr(btn, "text", None) or ""


def _btn_web_url(btn: Any) -> str | None:
    url = getattr(btn, "url", None)
    if url:
        return str(url)
    web = getattr(btn, "web_app", None)
    if web is not None and getattr(web, "url", None):
        return str(web.url)
    return None


def _is_webapp_button(btn: Any) -> bool:
    if isinstance(btn, (types.KeyboardButtonWebView, types.KeyboardButtonSimpleWebView)):
        return True
    return getattr(btn, "web_app", None) is not None


def _label_match(label: str, needles: tuple[str, ...]) -> bool:
    # fold_text снимает combining marks (й→и), поэтому needles тоже нормализуем
    folded = fold_text(label)
    return any(fold_text(n) in folded for n in needles)


def _find_button(
    message: Any,
    needles: tuple[str, ...],
) -> tuple[int, int, str, Any] | None:
    for ri, ci, label, btn in extract_inline_buttons(message):
        if _label_match(label, needles):
            return ri, ci, label, btn
    return None


def _is_invite_like(url: str) -> bool:
    low = (url or "").lower()
    if "t.me/" not in low and "telegram.me/" not in low:
        return False
    # одноразовые инвайты / joinchat / addlist — приоритет
    if any(x in low for x in ("/+", "joinchat/", "/addlist/", "start=")):
        return True
    # публичный канал тоже ок как финальная кнопка «Перейти в канал»
    return True


def extract_invite_from_message(message: Any) -> str | None:
    """Достать t.me URL: сначала «Перейти в канал», потом любой invite-like."""
    from app.tg.join_flows import _collect_urls_from_message, _is_invite_url

    open_url: str | None = None
    any_url: str | None = None
    for _, _, label, btn in extract_inline_buttons(message):
        u = _btn_web_url(btn)
        if not u or "t.me/" not in u.lower():
            continue
        if _label_match(label, _OPEN_CHAT_BTN):
            open_url = u
            break
        if _is_invite_url(u) or _is_invite_like(u):
            any_url = any_url or u
    if open_url:
        return open_url
    if any_url:
        return any_url
    for u in _collect_urls_from_message(message):
        if _is_invite_url(u) or ("t.me/" in u.lower() and ("+" in u or "joinchat" in u.lower())):
            return u
    return None


def _parse_webapp_init(url: str) -> tuple[str, str]:
    """Вернуть (origin, initData) из URL Mini App."""
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    frag = parsed.fragment or ""
    qs = parse_qs(frag.replace("?", "&"))
    qs2 = parse_qs(parsed.query)
    for bucket in (qs, qs2):
        if bucket.get("tgWebAppData"):
            return origin, unquote(bucket["tgWebAppData"][0])
    m = re.search(r"tgWebAppData=([^&]+)", frag)
    if m:
        return origin, unquote(m.group(1))
    return origin, ""


async def _click_button(message: Any, ri: int, ci: int, label: str) -> bool:
    try:
        await message.click(i=ri, j=ci)
        return True
    except Exception:
        try:
            await message.click(text=label)
            return True
        except Exception:
            return False


async def _request_webapp_url(
    client: TelegramClient,
    bot: Any,
    btn: Any,
    message: Any | None = None,
) -> str | None:
    url = _btn_web_url(btn) or ""
    if url:
        for req in (
            lambda: functions.messages.RequestWebViewRequest(
                peer=bot,
                bot=bot,
                platform="android",
                from_bot_menu=False,
                url=url,
            ),
            lambda: functions.messages.RequestSimpleWebViewRequest(
                bot=bot,
                platform="android",
                from_switch_webview=False,
                url=url,
            ),
        ):
            try:
                result = await client(req())
                if getattr(result, "url", None):
                    return str(result.url)
            except Exception:
                continue
    if message is not None:
        label = _btn_label(btn)
        hit = _find_button(message, _TERMS_BTN)
        if hit:
            await _click_button(message, hit[0], hit[1], hit[2])
            await asyncio.sleep(0.6)
        elif label:
            try:
                await message.click(text=label)
                await asyncio.sleep(0.6)
            except Exception:
                pass
    return url or None


async def _accept_terms_http(webapp_url: str) -> bool:
    """Эмулирует нажатие «I accept the terms» через API Mini App."""
    if not webapp_url:
        return False
    origin, init_data = _parse_webapp_init(webapp_url)
    headers_base = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Origin": origin,
        "Referer": webapp_url.split("#")[0],
    }
    bodies: list[dict[str, Any]] = []
    if init_data:
        bodies.extend(
            [
                {"initData": init_data},
                {"init_data": init_data},
                {"accepted": True, "initData": init_data},
                {"accept": True, "initData": init_data},
                {"terms_accepted": True, "initData": init_data},
                {"action": "accept", "initData": init_data},
            ]
        )
    bodies.extend([{"accepted": True}, {"accept": True}, {"action": "accept"}])

    async with httpx.AsyncClient(follow_redirects=True, timeout=25.0) as http:
        html = ""
        try:
            page = await http.get(webapp_url.split("#")[0] or webapp_url, headers=headers_base)
            html = page.text or ""
        except Exception:
            pass

        extra_paths = list(_ACCEPT_API_PATHS)
        for m in re.finditer(
            r"[\"'`](/api/[^\"'`]*(?:accept|terms|agree)[^\"'`]*)[\"'`]",
            html,
            re.I,
        ):
            path = m.group(1)
            if path not in extra_paths:
                extra_paths.insert(0, path)
        # data-action / fetch url в скриптах
        for m in re.finditer(
            r"(?:fetch|axios|post)\s*\(\s*[\"'`]([^\"'`]*(?:accept|terms)[^\"'`]*)[\"'`]",
            html,
            re.I,
        ):
            path = m.group(1)
            if path.startswith("http"):
                try:
                    p = urlparse(path).path
                    if p and p not in extra_paths:
                        extra_paths.insert(0, p)
                except Exception:
                    pass
            elif path.startswith("/") and path not in extra_paths:
                extra_paths.insert(0, path)

        for path in extra_paths:
            endpoint = origin.rstrip("/") + path
            for body in bodies:
                auth_variants: list[dict[str, str]] = [{}]
                if init_data:
                    auth_variants = [
                        {"Authorization": f"tma {init_data}"},
                        {"X-Telegram-Init-Data": init_data},
                        {},
                    ]
                for auth in auth_variants:
                    try:
                        headers = {**headers_base, **auth}
                        r = await http.post(endpoint, json=body, headers=headers)
                        if 200 <= r.status_code < 300:
                            return True
                        r2 = await http.post(endpoint, data=body, headers=headers)
                        if 200 <= r2.status_code < 300:
                            return True
                    except Exception:
                        continue

        if init_data:
            try:
                r = await http.get(
                    origin.rstrip("/") + "/api/accept",
                    params={"initData": init_data, "accepted": "1"},
                    headers=headers_base,
                )
                if 200 <= r.status_code < 300:
                    return True
            except Exception:
                pass
    return False


async def _accept_terms_send_data(
    client: TelegramClient,
    bot: Any,
    button_text: str,
) -> bool:
    """Fallback: messages.sendWebViewData с типичными payload Mini App."""
    payloads = (
        "accepted",
        "accept",
        "I accept the terms",
        '{"accepted":true}',
        '{"accept":true}',
        '{"action":"accept"}',
        "terms_accepted",
    )
    for data in payloads:
        try:
            await client(
                functions.messages.SendWebViewDataRequest(
                    bot=bot,
                    button_text=(button_text or "Условия пользования")[:64],
                    data=data,
                )
            )
            await asyncio.sleep(0.9)
            return True
        except Exception:
            continue
    return False


async def _scan_click(
    client: TelegramClient,
    bot: Any,
    needles: tuple[str, ...],
    *,
    limit: int = 15,
) -> str | None:
    async for msg in client.iter_messages(bot, limit=limit):
        found = _find_button(msg, needles)
        if not found:
            continue
        ri, ci, label, _btn = found
        if await _click_button(msg, ri, ci, label):
            return label
    return None


async def _send_texts(client: TelegramClient, bot: Any, texts: tuple[str, ...]) -> str | None:
    for text in texts:
        try:
            await client.send_message(bot, text)
            await asyncio.sleep(1.0)
            return text
        except Exception:
            continue
    return None


async def _has_terms_gate(client: TelegramClient, bot: Any) -> bool:
    async for msg in client.iter_messages(bot, limit=8):
        if _find_button(msg, _TERMS_BTN):
            return True
    return False


async def _has_menu_help(client: TelegramClient, bot: Any) -> bool:
    async for msg in client.iter_messages(bot, limit=10):
        if _find_button(msg, _HELP_BTN):
            return True
    return False


async def _wait_for_invite(
    client: TelegramClient,
    bot: Any,
    *,
    wait_sec: float = 14.0,
) -> str | None:
    """Ждать ссылку: NewMessage + MessageEdited + периодический рескан (бот часто edit'ит)."""
    found: list[str] = []

    async def _capture(message: Any) -> None:
        if found:
            return
        url = extract_invite_from_message(message)
        if url:
            found.append(url)

    async def _on_new(event: events.NewMessage.Event) -> None:
        await _capture(event.message)

    async def _on_edit(event: events.MessageEdited.Event) -> None:
        await _capture(event.message)

    client.add_event_handler(_on_new, events.NewMessage(chats=bot))
    client.add_event_handler(_on_edit, events.MessageEdited(chats=bot))
    try:
        # уже есть в истории?
        async for msg in client.iter_messages(bot, limit=12):
            await _capture(msg)
            if found:
                return found[0]

        deadline = asyncio.get_event_loop().time() + wait_sec
        while not found and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.8)
            async for msg in client.iter_messages(bot, limit=8):
                await _capture(msg)
                if found:
                    return found[0]
    finally:
        for cb in (_on_new, _on_edit):
            try:
                client.remove_event_handler(cb)
            except Exception:
                pass
    return found[0] if found else None


async def _accept_terms_flow(
    client: TelegramClient,
    bot: Any,
) -> bool:
    terms_msg = None
    terms_btn = None
    terms_label = ""
    terms_ri = terms_ci = 0
    async for msg in client.iter_messages(bot, limit=12):
        hit = _find_button(msg, _TERMS_BTN)
        if hit:
            terms_ri, terms_ci, terms_label, terms_btn = hit
            terms_msg = msg
            break

    if terms_btn is None or terms_msg is None:
        return not await _has_terms_gate(client, bot)

    webapp_url = await _request_webapp_url(client, bot, terms_btn, terms_msg)
    accepted = False
    if webapp_url:
        accepted = await _accept_terms_http(webapp_url)
        if accepted:
            log.info("ws_guard: terms accepted via http")

    if not accepted:
        await _click_button(terms_msg, terms_ri, terms_ci, terms_label)
        await asyncio.sleep(0.5)
        accepted = await _accept_terms_send_data(client, bot, terms_label)
        if accepted:
            log.info("ws_guard: terms accepted via sendWebViewData")

    if not accepted and webapp_url:
        # иногда initData появляется только после RequestWebView + клика
        try:
            result = await client(
                functions.messages.RequestWebViewRequest(
                    peer=bot,
                    bot=bot,
                    platform="android",
                    from_bot_menu=False,
                    url=_btn_web_url(terms_btn) or webapp_url.split("#")[0],
                )
            )
            if getattr(result, "url", None):
                webapp_url = str(result.url)
        except Exception:
            pass
        accepted = await _accept_terms_http(webapp_url)
        if accepted:
            log.info("ws_guard: terms accepted via http retry")

    await asyncio.sleep(1.4)

    # после accept бот шлёт главное меню; если всё ещё terms — ещё /start
    if await _has_terms_gate(client, bot) and not await _has_menu_help(client, bot):
        try:
            await client.send_message(bot, "/start")
            await asyncio.sleep(1.3)
        except Exception:
            pass
        # повторный sendData
        if not accepted:
            await _accept_terms_send_data(client, bot, terms_label or "Условия пользования")
            await asyncio.sleep(1.0)

    return (not await _has_terms_gate(client, bot)) or await _has_menu_help(client, bot)


async def find_invite_from_ws_guard(
    client: TelegramClient,
    bot_username: str = "WsGuardBot",
) -> str | None:
    """
    Полный сценарий WsGuardBot → одноразовая ссылка на чат.
    """
    bot = await client.get_entity(bot_username.lstrip("@"))

    # 1) /start
    try:
        await client.send_message(bot, "/start")
    except Exception:
        await asyncio.sleep(1.0)
        await client.send_message(bot, "/start")
    await asyncio.sleep(1.6)

    # 2) Mini App terms → I accept the terms
    await _accept_terms_flow(client, bot)

    # убедиться что меню доступно
    if not await _has_menu_help(client, bot):
        try:
            await client.send_message(bot, "/start")
            await asyncio.sleep(1.4)
        except Exception:
            pass
        if await _has_terms_gate(client, bot):
            await _accept_terms_flow(client, bot)

    # 3) Помощь
    help_clicked = await _scan_click(client, bot, _HELP_BTN)
    if not help_clicked:
        await _send_texts(client, bot, ("❔ Помощь", "Помощь", "Help"))
    else:
        await asyncio.sleep(1.3)

    # 4) Запросить ссылку (подменю помощи)
    req = await _scan_click(client, bot, _REQUEST_LINK_BTN)
    if not req:
        await _send_texts(
            client,
            bot,
            ("Запросить ссылку", "🔗 Запросить ссылку", "Request link"),
        )
        await asyncio.sleep(0.8)
        req = await _scan_click(client, bot, _REQUEST_LINK_BTN)
    else:
        await asyncio.sleep(1.4)

    # 5) «Получить ссылку на чат»
    got = await _scan_click(client, bot, _GET_LINK_BTN)
    if not got:
        # иногда кнопка появляется с задержкой / после edit
        await asyncio.sleep(1.5)
        got = await _scan_click(client, bot, _GET_LINK_BTN)
    if got:
        await asyncio.sleep(1.6)
    else:
        await _send_texts(
            client,
            bot,
            ("Получить ссылку на чат", "🔗 Получить ссылку на чат"),
        )
        await asyncio.sleep(1.4)

    # 6) URL «Перейти в канал» (часто edit того же сообщения)
    invite = await _wait_for_invite(client, bot, wait_sec=12.0)
    if invite:
        return invite

    # финальный проход: клик по «Перейти…» если url уже есть / появится
    async for msg in client.iter_messages(bot, limit=12):
        url = extract_invite_from_message(msg)
        if url:
            return url
        hit = _find_button(msg, _OPEN_CHAT_BTN)
        if hit:
            ri, ci, label, btn = hit
            u = _btn_web_url(btn)
            if u and "t.me/" in u.lower():
                return u
            await _click_button(msg, ri, ci, label)
            await asyncio.sleep(1.2)

    return await _wait_for_invite(client, bot, wait_sec=6.0)
