from __future__ import annotations

from io import BytesIO
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.models import Account, Chat, MinuteSlot
from app.utils.minutes import format_minute, period_for_interval, suggest_minute


HEADER_FILL = PatternFill("solid", fgColor="1F2937")
HEADER_FONT = Font(color="FFFFFF", bold=True)
ALT_FILL = PatternFill("solid", fgColor="F3F4F6")


def export_tables(
    chats: list[Chat],
    accounts: list[Account],
    slots: list[MinuteSlot],
) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)

    by_chat: dict[int, list[MinuteSlot]] = {}
    for slot in slots:
        by_chat.setdefault(slot.chat_pk, []).append(slot)

    accounts_by_id = {a.id: a for a in accounts}
    schedule_chats = [c for c in chats if c.kind == "schedule"]
    if not schedule_chats:
        ws = wb.create_sheet("empty")
        ws["A1"] = "Нет schedule-чатов"
    else:
        for chat in schedule_chats:
            name = _sheet_name(chat)
            ws = wb.create_sheet(name)
            ws.append(
                [
                    "account_id",
                    "label",
                    "start_minute",
                    "minute_display",
                    "chat_id",
                    "chat_title",
                    "interval_min",
                ]
            )
            for cell in ws[1]:
                cell.fill = HEADER_FILL
                cell.font = HEADER_FONT
            chat_slots = sorted(by_chat.get(chat.id, []), key=lambda s: s.start_minute)
            for i, slot in enumerate(chat_slots, start=2):
                acc = accounts_by_id.get(slot.account_id)
                ws.append(
                    [
                        slot.account_id,
                        acc.label if acc else slot.account_label,
                        slot.start_minute,
                        format_minute(slot.start_minute),
                        chat.chat_id,
                        chat.display_name,
                        chat.interval_minutes,
                    ]
                )
                if i % 2 == 0:
                    for cell in ws[i]:
                        cell.fill = ALT_FILL
            occupied = [s.start_minute for s in chat_slots]
            period = period_for_interval(chat.interval_minutes)
            try:
                nxt = suggest_minute(period, occupied)
                hint = format_minute(nxt)
            except Exception:
                hint = "full"
            ws.append([])
            ws.append(["next_free_minute", hint, "period", period])
            for col in range(1, 8):
                ws.column_dimensions[get_column_letter(col)].width = 18
            ws.freeze_panes = "A2"

    grid = wb.create_sheet("grid", 0)
    minutes = sorted({s.start_minute for s in slots if any(c.id == s.chat_pk for c in schedule_chats)})
    header = ["Минута / чат"] + [c.display_name for c in schedule_chats]
    grid.append(header)
    for cell in grid[1]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center")
    cell_at = {(s.chat_pk, s.start_minute): s for s in slots}
    if not minutes:
        minutes = [0, 15, 30, 45]
    for minute in minutes:
        row = [minute]
        for chat in schedule_chats:
            slot = cell_at.get((chat.id, minute))
            if slot:
                acc = accounts_by_id.get(slot.account_id)
                row.append(acc.label if acc else slot.account_label)
            else:
                row.append("")
        grid.append(row)
        for cell in grid[grid.max_row]:
            if cell.column > 1 and cell.value:
                cell.fill = PatternFill("solid", fgColor="BDD7EE")
    grid.column_dimensions["A"].width = 16
    for col in range(2, max(len(header), 2) + 1):
        grid.column_dimensions[get_column_letter(col)].width = 14
    grid.freeze_panes = "B2"

    overview = wb.create_sheet("overview", 1)
    overview.append(["chat_title", "chat_id", "interval", "account", "account_id", "minute"])
    for cell in overview[1]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center")
    row_i = 2
    for chat in schedule_chats:
        for slot in sorted(by_chat.get(chat.id, []), key=lambda s: s.start_minute):
            acc = accounts_by_id.get(slot.account_id)
            overview.append(
                [
                    chat.display_name,
                    chat.chat_id,
                    chat.interval_minutes,
                    acc.label if acc else slot.account_label,
                    slot.account_id,
                    slot.start_minute,
                ]
            )
            row_i += 1
    for col in range(1, 7):
        overview.column_dimensions[get_column_letter(col)].width = 22
    overview.freeze_panes = "A2"

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def parse_import(data: bytes) -> list[tuple[str, int, int]]:
    """Возвращает (account_id, start_minute, chat_tg_or_skip) нет — пары по sheet.

    Формат: колонки account_id, start_minute. chat_id в строке или имя листа.
    Возвращает список dict-подобных кортежей: (chat_id_str, account_id, minute)
    """
    wb = load_workbook(BytesIO(data), data_only=True)
    rows_out: list[tuple[str, int, int]] = []
    for ws in wb.worksheets:
        if ws.title.lower() in {"grid", "empty"}:
            continue
        if ws.title.lower() == "overview":
            for row in ws.iter_rows(min_row=2, values_only=True):
                if not row or row[1] is None or row[4] is None or row[5] is None:
                    continue
                chat_id = str(row[1]).strip()
                account_id = int(row[4])
                minute = int(row[5]) % 60
                rows_out.append((chat_id, account_id, minute))
            continue
        headers = [str(c.value).strip().lower() if c.value is not None else "" for c in ws[1]]
        if "account_id" not in headers or "start_minute" not in headers:
            continue
        idx = {name: i for i, name in enumerate(headers)}
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or row[idx["account_id"]] in (None, "") or row[idx["start_minute"]] in (None, ""):
                continue
            if str(row[idx["account_id"]]).lower().startswith("next_"):
                continue
            try:
                account_id = int(row[idx["account_id"]])
                minute = int(row[idx["start_minute"]]) % 60
            except (TypeError, ValueError):
                continue
            chat_id = ""
            if "chat_id" in idx and row[idx["chat_id"]] not in (None, ""):
                chat_id = str(row[idx["chat_id"]]).strip()
            rows_out.append((chat_id, account_id, minute))
    return rows_out


def save_xlsx(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)


def _sheet_name(chat: Chat) -> str:
    raw = f"{chat.id}_{chat.title}"
    bad = set(r"[]:*?/\\")
    cleaned = "".join("_" if ch in bad else ch for ch in raw)
    return cleaned[:31] or f"chat_{chat.id}"
