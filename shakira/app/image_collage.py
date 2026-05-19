"""Monta varias imagens num unico JPEG (grid) para envio no WhatsApp."""

from __future__ import annotations

import math
from io import BytesIO

from PIL import Image

_CELL_MAX_PX = 720
_GAP_PX = 8
_BG_RGB = (24, 24, 24)
_JPEG_QUALITY = 85


def build_image_grid(items: list[tuple[bytes, str]]) -> bytes:
    """
    Combina imagens JPEG/PNG em um unico JPEG.
    items: lista (bytes_da_imagem, rotulo_opcional).
    """
    if not items:
        raise ValueError("Nenhuma imagem para montar o collage.")
    if len(items) == 1:
        return items[0][0]

    cells: list[Image.Image] = []
    for img_bytes, _label in items:
        img = Image.open(BytesIO(img_bytes))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        elif img.mode == "L":
            img = img.convert("RGB")
        img.thumbnail((_CELL_MAX_PX, _CELL_MAX_PX), Image.Resampling.LANCZOS)
        cells.append(img)

    count = len(cells)
    cols = min(3, count) if count <= 9 else 3
    rows = math.ceil(count / cols)

    cell_w = max(img.width for img in cells)
    cell_h = max(img.height for img in cells)

    canvas_w = cols * cell_w + (cols + 1) * _GAP_PX
    canvas_h = rows * cell_h + (rows + 1) * _GAP_PX
    canvas = Image.new("RGB", (canvas_w, canvas_h), _BG_RGB)

    for index, img in enumerate(cells):
        row, col = divmod(index, cols)
        x = _GAP_PX + col * (cell_w + _GAP_PX) + (cell_w - img.width) // 2
        y = _GAP_PX + row * (cell_h + _GAP_PX) + (cell_h - img.height) // 2
        canvas.paste(img, (x, y))

    buf = BytesIO()
    canvas.save(buf, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
    return buf.getvalue()
