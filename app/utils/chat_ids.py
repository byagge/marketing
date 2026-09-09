from __future__ import annotations


def chat_id_aliases(cid: str | int | None) -> set[str]:
    raw = str(cid or "").strip()
    if not raw:
        return set()
    keys = {raw}
    if raw.lstrip("-").isdigit():
        n = int(raw)
        keys.add(str(n))
        keys.add(str(abs(n)))
        if raw.startswith("-100") and raw[4:].isdigit():
            keys.add(raw[4:])
        else:
            keys.add(f"-100{abs(n)}")
    return keys


def chat_ids_match(left: str | int | None, right: str | int | None) -> bool:
    a, b = chat_id_aliases(left), chat_id_aliases(right)
    return bool(a and b and a & b)


def any_chat_match(cid: str | int | None, chats) -> bool:
    return any(chat_ids_match(cid, getattr(c, "chat_id", c)) for c in chats)
