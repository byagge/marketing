"""Автопилот: сам вступает в чаты, настраивает отправку, фиксирует баны и пишет админу.

Каждый проход по каждому Telethon-аккаунту:
1. проверяет членство в чатах каталога (кроме выключенных для пары и забаненных);
2. вступает в недостающие (лимит за проход, паузы, бэкофф при неудачах);
3. настраивает schedule/sender для вновь вступивших и ещё не настроенных чатов;
4. вылет из чата (2 проверки подряд) и бан при вступлении заносит в базу банов;
5. админу уходит одна сводка — только если было что сообщить.
"""

from __future__ import annotations

import asyncio
import logging
import random
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

from aiogram import Bot

from app.config import get_settings
from app.jobs import runtime
from app.jobs.advisor import demote_sender_on_spam
from app.jobs.bans import admin_target, format_ban_notice, register_ban
from app.jobs.gate import gate_step
from app.jobs.parallel import map_batches, setup_parallel_defaults
from app.jobs.perf import describe_stats, perf_step
from app.jobs.setup import run_sender_refresh, run_setup_chats_only
from app.jobs.spam import account_load, run_spam_check, sync_sender_load
from app.jobs.stoplist import sweep_stoplist
from app.models import Account, Chat, JoinState
from app.sender_api import SenderAPI, SenderAPIError
from app.store import Store
from app.tg.client import telethon_client
from app.tg.join import check_membership, is_closed_chat, join_one, sort_chats_for_join
from app.tg.linkharvest import harvest_join_link, is_no_link_error
from app.tg.resolve import lookup_entity
from app.utils.chat_ids import chat_ids_match
from app.utils.send_policy import classify_failure, is_no_post, join_due, removal_confirmed
from app.utils.errfmt import short_error

log = logging.getLogger("marketing.autopilot")

_BUSY_KINDS = ("setup", "join_chats", "folder_join", "leave", "setup_retry")


@dataclass
class AccountResult:
    label: str
    joined: list[str] = field(default_factory=list)
    requested: list[str] = field(default_factory=list)
    manual: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    setup_ok: list[str] = field(default_factory=list)
    spam: list[str] = field(default_factory=list)
    gates: list[str] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    redesign: list[str] = field(default_factory=list)  # не приносят клиентов → переоформить
    redesign_new: bool = False  # есть свежие/напоминания — нужен mention в сводке
    notes: list[str] = field(default_factory=list)
    sender_refreshed: bool = False
    skipped: str = ""
    error: str = ""

    @property
    def happened(self) -> bool:
        return bool(
            self.joined
            or self.requested
            or self.manual
            or self.failed
            or self.removed
            or self.setup_ok
            or self.spam
            or self.gates
            or self.stopped
            or self.redesign
            or self.notes
            or self.sender_refreshed
            or self.error
        )


@asynccontextmanager
async def _open_client(acc: Account):
    async with telethon_client(acc.telethon_session) as client:
        yield client


def _is_enabled(value: str) -> bool:
    return str(value).strip().casefold() not in {"0", "false", "off", ""}


async def autopilot_enabled(store: Store) -> bool:
    return _is_enabled(await store.get_setting("autopilot_enabled", "1"))


async def _sender_needs_refresh(
    store: Store,
    acc: Account,
    sender_chats: list[Chat],
    new_pks: set[int],
) -> bool:
    """Нужно ли перенастроить sender: новые чаты, либо чат есть, но в Autoposter не активен."""
    if not sender_chats or not acc.sender_on or not acc.has_sender:
        return False
    if any(c.id in new_pks for c in sender_chats):
        return True
    sid = (acc.sender_account_id or "").strip()
    if not sid:
        return True
    cfg = get_settings()
    last_raw = await store.get_setting(f"autopilot_sender_refresh_{acc.id}", "")
    if last_raw:
        try:
            last = datetime.fromisoformat(last_raw)
            if (datetime.now(timezone.utc) - last).total_seconds() < (
                cfg.autopilot_sender_refresh_hours * 3600
            ):
                return False
        except ValueError:
            pass
    try:
        live = await SenderAPI(cfg.sender_api_url, cfg.sender_api_key).list_chats(sid)
    except SenderAPIError:
        return False
    for chat in sender_chats:
        entry = next((c for c in live if chat_ids_match(c.get("chat_id"), chat.chat_id)), None)
        if entry is None or entry.get("active") is False:
            return True
    return False


async def process_account(
    store: Store,
    acc: Account,
    chats: list[Chat],
    bot: Bot | None,
    *,
    is_cancelled=lambda: False,
) -> AccountResult:
    cfg = get_settings()
    res = AccountResult(label=acc.label)

    busy = [k for k in _BUSY_KINDS if runtime.is_running(k, acc.id)]
    if busy:
        res.skipped = f"занят: {', '.join(busy)}"
        return res

    prefs = await store.prefs_for_account(acc.id)
    banned = {b.chat_pk for b in await store.list_bans(account_id=acc.id)}
    stoplist = await store.get_stoplist()
    eligible = [
        c
        for c in chats
        if (prefs.get(c.id) is None or prefs[c.id].enabled)
        and c.id not in banned
        and not is_no_post(c, stoplist)  # «Отзывы» и т. п.: ни вступать, ни писать
        and not (acc.is_outreach and c.kind == "sender")  # аутрич: только schedule-чаты
    ]

    new_pks: set[int] = set()
    member_pks: set[int] = set()
    joined_budget = max(0, int(cfg.autopilot_join_per_tick))
    prev_state = await store.get_spam_state(acc.id)

    try:
        async with _open_client(acc) as client:
            await _spam_step(store, acc, client, res)
            # В спамблоке открытые чаты недоступны, а закрытые (инвайт/гарант) работают:
            # открытые откладываем до снятия блока, закрытые вступаем как обычно.
            spam_limited = (await store.get_spam_state(acc.id)).is_limited
            report = await check_membership(client, eligible)
            member_pks = {c.id for c in report.joined}
            for chat in report.joined:
                await store.mark_member(acc.id, chat.id)
            if not is_cancelled():
                await _harvest_links(store, client, report.joined, res)

            candidates: list[Chat] = []
            no_link_ids = {c.id for c in report.no_link}
            fresh: dict[int, JoinState] = {}
            for chat in report.missing + report.no_link:
                state = await store.mark_not_member(acc.id, chat.id)
                if (
                    state.status == "manual"
                    and chat.id not in no_link_ids
                    and is_no_link_error(state.last_error)
                ):
                    # раньше способа вступить не было, теперь есть (ссылку нашли/добавили)
                    state = await store.record_join_result(
                        acc.id, chat.id, status="pending", error=""
                    )
                fresh[chat.id] = state
                if removal_confirmed(state):
                    await register_ban(
                        store,
                        acc,
                        chat,
                        "removed",
                        "аккаунт был в чате, но больше не участник",
                        bot,
                        notify=False,
                    )
                    res.removed.append(chat.display_name)
                    continue
                if state.last_member_at:
                    # раньше был в чате: не вступаем заново, пока вылет не подтвердится
                    continue
                if chat.id in no_link_ids:
                    if state.status != "manual":
                        await store.record_join_result(
                            acc.id, chat.id, status="manual", error="нет способа вступления"
                        )
                        res.manual.append(f"{chat.display_name}: нет ссылки/гаранта")
                    continue
                candidates.append(chat)

            due = [
                c
                for c in sort_chats_for_join(candidates)
                if (not spam_limited or is_closed_chat(c))
                and join_due(
                    fresh.get(c.id),
                    retry_hours=cfg.autopilot_retry_hours,
                    max_attempts=cfg.autopilot_max_join_attempts,
                    abandoned_retry_hours=cfg.autopilot_abandoned_retry_hours,
                )
            ]
            attempts = 0
            for chat in due:
                if attempts >= joined_budget or is_cancelled():
                    break
                if attempts:
                    await asyncio.sleep(
                        random.uniform(
                            cfg.autopilot_join_pause_min_sec,
                            max(
                                cfg.autopilot_join_pause_min_sec,
                                cfg.autopilot_join_pause_max_sec,
                            ),
                        )
                    )
                attempts += 1
                result = await join_one(client, chat)
                title = chat.display_name
                if result.status in {"joined", "already", "captcha_ok"}:
                    await store.mark_member(acc.id, chat.id)
                    new_pks.add(chat.id)
                    member_pks.add(chat.id)
                    if result.status != "already":
                        res.joined.append(title)
                elif result.status == "request_sent":
                    await store.record_join_result(
                        acc.id, chat.id, status="requested", error=result.detail
                    )
                    res.requested.append(title)
                elif result.status == "needs_manual":
                    await store.record_join_result(
                        acc.id, chat.id, status="manual", error=result.detail
                    )
                    res.manual.append(f"{title}: {result.detail}")
                elif result.status == "failed":
                    kind = classify_failure(result.detail)
                    if kind == "ban":
                        await register_ban(
                            store, acc, chat, "join_ban", result.detail, bot, notify=False
                        )
                    elif kind == "flood":
                        await store.record_join_result(
                            acc.id, chat.id, status="pending", error=result.detail
                        )
                        res.failed.append(f"{title}: FloodWait — пауза для аккаунта")
                        break
                    elif kind == "bad_invite":
                        await store.record_join_result(
                            acc.id, chat.id, status="manual", error=result.detail
                        )
                        res.manual.append(f"{title}: {result.detail}")
                    else:
                        st = await store.record_join_result(
                            acc.id,
                            chat.id,
                            status="pending",
                            error=result.detail,
                            fail=True,
                            max_attempts=cfg.autopilot_max_join_attempts,
                        )
                        tail = " — сдался, нужна помощь" if st.status == "abandoned" else ""
                        res.failed.append(f"{title}: {result.detail}{tail}")

            # Результативность: кто пишет аккаунту в личку, сколько раз ответил клоакинг.
            if not is_cancelled():
                try:
                    ev = await perf_step(store, client, acc)
                except Exception as e:
                    ev = None
                    res.failed.append(f"оценка результативности: {short_error(e)}")
                if ev is not None:
                    if ev.kind == "recovered":
                        res.notes.append(f"{acc.label}: снова приносит клиентов — из списка убран")
                    else:
                        res.redesign.append(f"{acc.label}: {describe_stats(ev.perf)}")
                        res.redesign_new = True

            # «Ворота подписки»: бот удаляет сообщение и просит подписаться на каналы.
            if not is_cancelled():
                members = [c for c in eligible if c.id in (member_pks | new_pks)]
                try:
                    for ev in await gate_step(
                        store,
                        client,
                        acc,
                        members,
                        budget=cfg.gate_checks_per_tick,
                        recheck_hours=cfg.gate_recheck_hours,
                    ):
                        if ev.kind == "resolved":
                            res.gates.append(f"{ev.chat}: {ev.detail}")
                        else:
                            res.manual.append(f"{ev.chat}: {ev.detail}")
                except Exception as e:
                    res.failed.append(f"ворота подписки: {short_error(e)}")
    except Exception as e:
        res.error = f"{short_error(e)}"
        log.exception("autopilot: %s", acc.label)
        return res

    # --- спамблок / dead: привести sender к нужной нагрузке -----------------------
    acc = await store.get_account(acc.id) or acc
    if await demote_sender_on_spam(store, acc):
        res.spam.append(
            f"{acc.label}: спамблок при включённом sender → перевёл в dead: sender выключен, "
            f"schedule продолжает работать"
        )
        acc = await store.get_account(acc.id) or acc
    load = await account_load(store, acc)
    refreshed = False
    if load != prev_state.applied_load or (
        acc.sender_forbidden and prev_state.applied_load != 0
    ):
        msg = await sync_sender_load(store, acc.id, None, None)
        refreshed = bool(acc.has_sender)
        res.sender_refreshed = refreshed
        if acc.has_sender and not acc.is_outreach:  # без sender / аутрич — не шумим
            res.spam.append(
                f"{acc.label}: нагрузка sender {prev_state.applied_load}% → {load}% ({msg})"
            )
        res.sender_refreshed = False  # уже отражено строкой про нагрузку

    # --- настройка отправки там, где аккаунт уже в чате ---------------------
    in_chat = member_pks | new_pks
    ok_setup = {s.chat_pk for s in await store.list_setup_states(acc.id, statuses=["ok"])}
    known_setup = {s.chat_pk for s in await store.list_setup_states(acc.id)}
    schedule_need = [
        c.id
        for c in eligible
        if c.is_schedule
        and c.id in in_chat
        and (c.id in new_pks or (c.id not in known_setup and c.id not in ok_setup))
    ]
    # Спамблок не мешает закрытым чатам — schedule настраиваем как обычно. Открытые
    # (публичные) чаты в блоке недоступны: их откладываем, чтобы не жечь попытки.
    if (await store.get_spam_state(acc.id)).is_limited:
        by_pk = {c.id: c for c in eligible}
        schedule_need = [pk for pk in schedule_need if is_closed_chat(by_pk[pk])]
    if schedule_need and not is_cancelled():
        try:
            buckets = await run_setup_chats_only(
                store,
                acc.id,
                schedule_need,
                None,
                None,
                job_kind="autopilot_setup",
                skip_abandoned=False,
            )
            res.setup_ok.extend(buckets.get("ok", []))
            for title in buckets.get("skipped", []) + buckets.get("config_error", []):
                res.failed.append(f"{title}: schedule не настроился (см. логи)")
        except Exception as e:
            res.failed.append(f"schedule: {short_error(e)}")

    sender_chats = [c for c in eligible if c.kind == "sender" and c.id in in_chat]
    if (
        sender_chats
        and not refreshed
        and not acc.sender_forbidden
        and load > 0
        and not is_cancelled()
        and await _sender_needs_refresh(store, acc, sender_chats, new_pks)
    ):
        n = await run_sender_refresh(store, acc.id, None, None, job_kind="autopilot_sender")
        await store.set_setting(
            f"autopilot_sender_refresh_{acc.id}",
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        res.sender_refreshed = True
        if n:
            res.setup_ok.append(f"sender×{n}")

    # Чаты «писать нельзя» не должны оставаться активными в Autoposter ни у кого.
    if (
        (acc.sender_account_id or "").strip()
        and acc.sender_on
        and not acc.sender_forbidden
        and load > 0
    ):
        res.stopped.extend(await sweep_stoplist(store, acc))
    return res


HARVEST_COOLDOWN_HOURS = 24.0
HARVEST_PER_TICK = 2


async def _harvest_links(store: Store, client, joined: list[Chat], res: AccountResult) -> None:
    """Чат без @username и invite-ссылки, а аккаунт в нём: берём ссылку у него для остальных."""
    now = datetime.now(timezone.utc)
    tried = 0
    for chat in joined:
        if tried >= HARVEST_PER_TICK:
            break
        if (chat.username or "").strip() or (chat.invite_link or "").strip():
            continue
        if (chat.garant_bot or "").strip() or chat.join_mode == "garant":
            continue
        key = f"harvest:{chat.id}"
        last = await store.get_setting(key, "")
        if last:
            try:
                age = (now - datetime.fromisoformat(last)).total_seconds() / 3600
            except ValueError:
                age = HARVEST_COOLDOWN_HOURS
            if age < HARVEST_COOLDOWN_HOURS:
                continue
        tried += 1
        await store.set_setting(key, now.isoformat(timespec="seconds"))
        try:
            entity = await lookup_entity(client, chat)
            if entity is None:
                continue
            link = await harvest_join_link(client, entity)
        except Exception:  # noqa: BLE001
            log.exception("autopilot: не удалось взять ссылку чата %s", chat.display_name)
            continue
        if not link.found:
            continue
        fields = {"username": link.username} if link.username else {"invite_link": link.invite}
        await store.update_chat(chat.id, **fields)
        revived = await store.revive_chat_joins(chat.id)
        extra = f", разбудил {revived} аккаунтов" if revived else ""
        res.notes.append(
            f"«{chat.display_name}»: взял {link.describe()} у аккаунта в чате — "
            f"остальные вступят сами{extra}"
        )


async def _spam_step(store: Store, acc: Account, client, res: AccountResult) -> None:
    """Проверка @SpamBot по расписанию; события → в сводку."""
    check = await run_spam_check(store, acc, client)
    if acc.is_outreach:
        return  # аутрич: статус пишем в базу, в сводки не шумим
    if check.error and not check.skipped:
        res.failed.append(f"@SpamBot: {check.error}")
    state = await store.get_spam_state(acc.id)
    cfg = get_settings()
    for ev in check.events:
        if ev == "limited_new":
            until = f" (до {state.limited_until[:16].replace('T', ' ')} UTC)" if state.limited_until else ""
            res.spam.append(
                f"{acc.label}: 🛡 спамблок{until}, повтор {state.strikes}/{cfg.spam_dead_strikes} "
                f"— снижаю нагрузку sender, schedule не трогаю"
            )
        elif ev == "cleared":
            res.spam.append(f"{acc.label}: ✅ спамблок снят — возвращаю нагрузку постепенно")
        elif ev == "dead":
            res.spam.append(
                f"{acc.label}: ☠ {state.strikes} спамблока подряд — перевожу в dead: "
                f"sender выключен навсегда, остаётся только schedule"
            )


def format_digest(results: list[AccountResult], mention: str = "") -> str:
    lines = ["🤖 Автопилот"]
    if mention and any(r.redesign_new for r in results):
        n = sum(len(r.redesign) for r in results if r.redesign_new)
        lines.insert(
            0,
            f"{mention} аккаунты не приносят клиентов — нужно переоформить: {n}",
        )

    def block(title: str, rows: list[str]) -> None:
        if rows:
            lines.append(f"\n{title}")
            lines.extend(f"· {r}" for r in rows[:25])
            if len(rows) > 25:
                lines.append(f"· … ещё {len(rows) - 25}")

    block("✓ Вступил", [f"{r.label} → {', '.join(r.joined)}" for r in results if r.joined])
    block(
        "📩 Заявки отправлены (ждём одобрения)",
        [f"{r.label} → {', '.join(r.requested)}" for r in results if r.requested],
    )
    block(
        "⚙ Настроил отправку",
        [f"{r.label}: {', '.join(r.setup_ok)}" for r in results if r.setup_ok],
    )
    block(
        "🎨 Аккаунты для переоформления (мало пишут / не пишут)",
        [m for r in results for m in r.redesign],
    )
    block("✅ Хорошие новости", [m for r in results for m in r.notes])
    block("🛡 Спамблок / нагрузка sender", [m for r in results for m in r.spam])
    block(
        "🔗 Подписался на обязательные каналы (ворота чата)",
        [f"{r.label} → {m}" for r in results for m in r.gates],
    )
    block(
        "⛔ Выключил чаты «писать нельзя»",
        [f"{r.label}: {', '.join(r.stopped)}" for r in results if r.stopped],
    )
    block(
        "🚪 Вылетели из чата (подтверждено двумя проверками)",
        [f"{r.label} ← {', '.join(r.removed)}" for r in results if r.removed],
    )
    block(
        "✋ Нужна помощь",
        [f"{r.label}: {m}" for r in results for m in r.manual],
    )
    block(
        "⚠ Не получилось",
        [f"{r.label}: {m}" for r in results for m in r.failed]
        + [f"{r.label}: {r.error}" for r in results if r.error],
    )
    return "\n".join(lines)


async def run_autopilot(
    store: Store,
    bot: Bot | None = None,
    admin_chat_id: int | None = None,
    *,
    force: bool = False,
) -> str:
    """Один проход автопилота по всем аккаунтам. Возвращает текст сводки."""
    if not force and not await autopilot_enabled(store):
        return "Автопилот выключен"
    if runtime.is_running("reconfigure_all", 0) or runtime.is_running("fix_minutes", 0):
        return "Автопилот: идёт массовая перенастройка — пропускаю проход"

    cfg = get_settings()
    accounts = [a for a in await store.list_accounts() if a.telethon_session]
    chats = await store.list_chats(enabled_only=True)
    if not accounts or not chats:
        return "Автопилот: нет аккаунтов с Telethon или нет чатов"

    size, pause = setup_parallel_defaults()

    async def _one(acc: Account) -> AccountResult:
        return await process_account(
            store,
            acc,
            chats,
            bot,
            is_cancelled=lambda: runtime.cancelled("autopilot", 0),
        )

    raw = await map_batches(
        accounts,
        _one,
        batch_size=min(size, 4),
        batch_pause=pause,
        is_cancelled=lambda: runtime.cancelled("autopilot", 0),
    )
    results: list[AccountResult] = []
    for acc, item in zip(accounts, raw):
        if isinstance(item, BaseException):
            results.append(AccountResult(label=acc.label, error=f"{type(item).__name__}: {item}"))
        else:
            results.append(item)

    unnotified = [b for b in await store.list_bans(active_only=True) if not b.notified]
    happened = [r for r in results if r.happened]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    skipped = sum(1 for r in results if r.skipped)
    if not happened and not unnotified:
        summary = f"Автопилот: всё в порядке ({len(accounts)} акк., пропущено занятых: {skipped})"
        await store.set_setting("autopilot_last_at", now)
        await store.set_setting("autopilot_last_summary", summary)
        return summary

    text = format_digest(happened, cfg.notify_mention)
    target = admin_chat_id or admin_target()
    messages = [text] if happened else []
    if unnotified:
        messages.append(await format_ban_notice(store, unnotified))
    if bot is not None and target:
        for msg in messages:
            try:
                await bot.send_message(target, msg[:4000])
            except Exception:
                log.exception("autopilot: не удалось отправить сводку")
        await store.mark_ban_notified([b.id for b in unnotified])
    summary = "\n\n".join(messages)
    await store.set_setting("autopilot_last_at", now)
    await store.set_setting("autopilot_last_summary", summary[:3500])
    return summary

