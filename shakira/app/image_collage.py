"""Monta varias imagens num unico JPEG (grid) para envio no WhatsApp."""

from __future__ import annotations

import math
import os
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

_CELL_MAX_PX = int(os.environ.get("COLLAGE_CELL_MAX_PX", "1280"))
_GAP_PX = 8
_BG_RGB = (24, 24, 24)
_JPEG_QUALITY = int(os.environ.get("COLLAGE_JPEG_QUALITY", "90"))


def _load_font(size: int = 24) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Carrega fonte DejaVu ou Arial se disponivel, caso contrario usa default."""
    for path in [
        "DejaVuSans-Bold.ttf",
        "DejaVuSans.ttf",
        "arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
    ]:
        try:
            return ImageFont.truetype(path, size)
        except IOError:
            continue
    return ImageFont.load_default()


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
    for img_bytes, label in items:
        img = Image.open(BytesIO(img_bytes))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        elif img.mode == "L":
            img = img.convert("RGB")
        img.thumbnail((_CELL_MAX_PX, _CELL_MAX_PX), Image.Resampling.LANCZOS)

        # Desenha o nome da câmera diretamente no painel para orientação perfeita da IA e do usuário
        if label:
            draw = ImageDraw.Draw(img)
            font = _load_font(28) # Tamanho ideal para células de 1280px
            try:
                bbox = draw.textbbox((0, 0), label, font=font)
                tw = bbox[2] - bbox[0]
                th = bbox[3] - bbox[1]
            except AttributeError:
                tw, th = draw.textsize(label, font=font)
            
            padx, pady = 12, 8
            box_w = tw + padx * 2
            box_h = th + pady * 2
            
            # Caixa preta sólida e texto amarelo para contraste máximo
            draw.rectangle([0, 0, box_w, box_h], fill=(0, 0, 0))
            draw.text((padx, pady), label, fill=(255, 255, 0), font=font)

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
