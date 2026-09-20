from __future__ import annotations

import argparse
from pathlib import Path

from session_maker.api import load_api, normalize_phone, out_dir, sanitize_session_name


def _ask(prompt: str, *, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        raw = input(f"{prompt}{suffix}: ").strip()
        if raw:
            return raw
        if default is not None:
            return default
        print("  пусто — введите значение")


def run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="session_maker",
        description="Создать Telethon или Pyrogram .session по номеру телефона",
    )
    parser.add_argument(
        "kind",
        nargs="?",
        choices=("telethon", "pyrogram", "both"),
        help="Тип session (если не указан — спросит)",
    )
    parser.add_argument(
        "-o",
        "--out",
        type=Path,
        default=None,
        help="Папка для .session (по умолчанию data/session_maker)",
    )
    args = parser.parse_args(argv)

    print()
    print("=== Marketing session_maker ===")
    print("Готовый .session для Telethon / Pyrogram")
    print("(В боте: меню → Session)")
    print()

    kind = args.kind
    if not kind:
        print("1) Telethon")
        print("2) Pyrogram")
        print("3) Оба (одно имя, два файла)")
        choice = _ask("Выбор", default="1")
        kind = {"1": "telethon", "2": "pyrogram", "3": "both"}.get(choice, choice)
        if kind not in {"telethon", "pyrogram", "both"}:
            print("Неизвестный выбор")
            return 2

    session_name = sanitize_session_name(_ask("Имя session (без .session)", default="account"))
    try:
        phone = normalize_phone(_ask("Номер телефона (+998… / +7…)"))
    except ValueError as e:
        print(e)
        return 2

    dest = Path(args.out) if args.out else out_dir()
    dest.mkdir(parents=True, exist_ok=True)
    api_id, api_hash = load_api()

    print()
    print(f"API_ID={api_id}")
    print(f"Папка: {dest}")
    print(f"Имя:   {session_name}")
    print(f"Тел:   {phone}")
    print()

    made: list[Path] = []
    if kind in {"telethon", "both"}:
        from session_maker.telethon_login import create_telethon_session

        path = create_telethon_session(
            out_dir=dest,
            session_name=session_name,
            phone=phone,
            api_id=api_id,
            api_hash=api_hash,
        )
        made.append(path)
        print(f"Telethon: {path}")

    if kind in {"pyrogram", "both"}:
        from session_maker.pyrogram_login import create_pyrogram_session

        path = create_pyrogram_session(
            out_dir=dest,
            session_name=session_name,
            phone=phone,
            api_id=api_id,
            api_hash=api_hash,
        )
        made.append(path)
        print(f"Pyrogram: {path}")

    print()
    print("Готово:")
    for p in made:
        print(f"  {p}  ({p.stat().st_size} bytes)")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
