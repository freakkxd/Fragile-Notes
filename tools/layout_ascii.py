#!/usr/bin/env python3
"""layout_ascii: PNG скриншот → грубая ASCII-карта яркости и цвета.

Позволяет «видеть» структуру UI (панели, карточки, отступы, акценты)
без визуализации картинки.
Условные обозначения:
  S  — почти чёрный (канвас #06-09)
  .  — тёмная поверхность
  o  — средняя (карточки/возвышение)
  #  — серое/яркое (текст, бордеры)
  @  — светлое (текст)
  B  — синий акцент    P — фиолетовый    G — зелёный    R — красный    W — тёплый
"""
from __future__ import annotations

import sys

import gi

gi.require_version("GdkPixbuf", "2.0")

from gi.repository import GdkPixbuf  # noqa: E402

WIDTH = int(sys.argv[2]) if len(sys.argv) > 2 else 116
COLS = WIDTH
ROWS = int(sys.argv[3]) if len(sys.argv) > 3 else max(8, int(COLS * 0.62))


def classify(r: float, g: float, b: float) -> str:
    L = 0.2126 * r + 0.7152 * g + 0.0722 * b
    mx, mn = max(r, g, b), min(r, g, b)
    sat = (mx - mn) / 255.0 if mx > 0 else 0.0
    if L < 22:
        return "S"
    if sat > 0.12 and L > 30:
        dom = max((r, "R"), (g, "G"), (b, "B"))[1]
        if dom == "B" and b > g * 1.22 and b > r * 1.28:
            return "B"
        if dom == "G" and g > b and g > r:
            return "G"
        if dom == "R" and r > g * 1.45 and r > b * 1.45:
            return "R"
        if b > r > g or b > g >= r:
            return "P"
        if r > b >= g:
            return "W"
        return "#"
    if L < 55:
        return "."
    if L < 120:
        return "o"
    if L < 185:
        return "#"
    return "@"


def main() -> None:
    path = sys.argv[1]
    pb = GdkPixbuf.Pixbuf.new_from_file(path)
    w, h = pb.get_width(), pb.get_height()
    pix = pb.get_pixels()
    nchan = pb.get_n_channels()
    rowstride = pb.get_rowstride()

    def px(x: int, y: int) -> tuple[int, int, int]:
        o = y * rowstride + x * nchan
        return pix[o], pix[o + 1], pix[o + 2]

    bw, bh = w / COLS, h / ROWS
    for r in range(ROWS):
        row = []
        y0, y1 = int(r * bh), max(int((r + 1) * bh), int(r * bh) + 1)
        for c in range(COLS):
            x0, x1 = int(c * bw), max(int((c + 1) * bw), int(c * bw) + 1)
            rs = gs = bs = n = 0
            for y in range(y0, y1, max(1, (y1 - y0) // 2)):
                for x in range(x0, x1, max(1, (x1 - x0) // 2)):
                    r_, g_, b_ = px(x, y)
                    rs += r_; gs += g_; bs += b_; n += 1
            if n:
                row.append(classify(rs / n, gs / n, bs / n))
            else:
                row.append("?")
        print("".join(row))
    print(f"# {w}x{h} → {COLS}x{ROWS}")


if __name__ == "__main__":
    main()
