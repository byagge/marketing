"""
Режет PNG-логотип на подряд идущие тайлы 100x100.

Когда эмодзи ставятся в ряд по порядку — снова складываются в одно фото.
Без разбивки по буквам: просто сетка по 100 px.

Использование:
  python create_emoji_pack.py
  python create_emoji_pack.py path/to/logo.png --out out_dir --size 100
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from pathlib import Path

import numpy as np
from PIL import Image

PACK_TITLE = "arix coding"
PACK_USERNAME = "arxixx"
DEFAULT_INPUT = Path(__file__).with_name("53b27f8e-165a-4e31-9736-e71dde54b035.png")
DEFAULT_EMOJI_SIZE = 100


def alpha_mask(arr: np.ndarray, threshold: int = 16) -> np.ndarray:
    alpha = arr[:, :, 3]
    if int(alpha.min()) < 250:
        return alpha > threshold

    rgb = arr[:, :, :3].astype(np.int16)
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    return (r > 35) & (r >= g) & (r >= b) & ((r + g + b) > 50)


def crop_logo(arr: np.ndarray, mask: np.ndarray, pad: int = 8) -> Image.Image:
    ys, xs = np.where(mask)
    if len(xs) == 0:
        raise RuntimeError("На изображении нет непрозрачных пикселей логотипа.")

    h, w = mask.shape
    x0 = max(0, int(xs.min()) - pad)
    x1 = min(w - 1, int(xs.max()) + pad)
    y0 = max(0, int(ys.min()) - pad)
    y1 = min(h - 1, int(ys.max()) + pad)

    region = arr[y0 : y1 + 1, x0 : x1 + 1].copy()
    region_mask = mask[y0 : y1 + 1, x0 : x1 + 1]
    region[~region_mask, 3] = 0
    return Image.fromarray(region, "RGBA")


def make_strip(logo: Image.Image, tile: int) -> Image.Image:
    """Высота = tile, ширина кратна tile; логотип по центру."""
    if logo.height == 0 or logo.width == 0:
        raise RuntimeError("Пустой кроп логотипа.")

    scale = tile / logo.height
    nw = max(1, int(round(logo.width * scale)))
    nh = tile
    scaled = logo.resize((nw, nh), Image.Resampling.LANCZOS)

    cols = max(1, math.ceil(nw / tile))
    strip_w = cols * tile
    strip = Image.new("RGBA", (strip_w, tile), (0, 0, 0, 0))
    x = (strip_w - nw) // 2
    strip.paste(scaled, (x, 0), scaled)
    return strip


def slice_tiles(strip: Image.Image, tile: int) -> list[Image.Image]:
    assert strip.height == tile
    assert strip.width % tile == 0
    cols = strip.width // tile
    return [strip.crop((i * tile, 0, (i + 1) * tile, tile)) for i in range(cols)]


def build_pack(input_path: Path, out_dir: Path, tile: int) -> dict:
    im = Image.open(input_path).convert("RGBA")
    arr = np.array(im)
    mask = alpha_mask(arr)

    logo = crop_logo(arr, mask)
    strip = make_strip(logo, tile)
    tiles = slice_tiles(strip, tile)

    if out_dir.exists():
        shutil.rmtree(out_dir)
    emoji_dir = out_dir / "emoji"
    emoji_dir.mkdir(parents=True)

    strip_path = out_dir / "strip.png"
    strip.save(strip_path, "PNG")

    # превью: тайлы снова в ряд
    preview = Image.new("RGBA", strip.size, (0, 0, 0, 0))
    catalog: list[dict] = []

    print(f"Источник: {input_path}")
    print(f"Пак: {PACK_TITLE} @{PACK_USERNAME}")
    print(f"Тайл: {tile}x{tile}")
    print(f"Полоса: {strip.width}x{strip.height} -> {len(tiles)} эмодзи")
    print("Тайлы (слева направо):")

    for i, piece in enumerate(tiles, start=1):
        name = f"tile_{i:02d}"
        path = emoji_dir / f"{name}.png"
        piece.save(path, "PNG")
        preview.paste(piece, ((i - 1) * tile, 0), piece)
        catalog.append(
            {
                "file": f"emoji/{name}.png",
                "name": name,
                "index": i,
                "emoji": "🟥",
                "order": i,
            }
        )
        print(f"  + {path.name}")

    preview_path = out_dir / "preview_reassembled.png"
    preview.save(preview_path, "PNG")

    meta = {
        "title": PACK_TITLE,
        "name": PACK_USERNAME,
        "short_name": PACK_USERNAME,
        "link_hint": f"https://t.me/addemoji/{PACK_USERNAME}",
        "source": input_path.name,
        "emoji_size": tile,
        "tile_count": len(tiles),
        "strip": "strip.png",
        "preview": "preview_reassembled.png",
        "usage": "Ставь эмодзи подряд слева направо: tile_01 tile_02 ... — сложится в логотип",
        "stickers": catalog,
    }
    meta_path = out_dir / "pack.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nГотово: {out_dir}")
    print(f"Всего тайлов: {len(tiles)}")
    print(f"Полоса: {strip_path}")
    print(f"Превью сборки: {preview_path}")
    return meta


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Нарезать логотип на эмодзи 100x100, которые складываются в одно фото"
    )
    parser.add_argument("input", nargs="?", type=Path, default=DEFAULT_INPUT)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).with_name("arix_coding_emoji"),
    )
    parser.add_argument("--size", type=int, default=DEFAULT_EMOJI_SIZE, help="Сторона тайла")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"Файл не найден: {args.input}", file=sys.stderr)
        sys.exit(1)

    build_pack(args.input, args.out, args.size)


if __name__ == "__main__":
    main()
