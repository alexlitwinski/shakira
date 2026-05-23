"""Monta varias imagens num unico JPEG (grid) para envio no WhatsApp."""

from __future__ import annotations

import math
import os
import logging
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

# Reduzidos os limites padrões para evitar erro 413 do Nginx (Payload Too Large)
_CELL_MAX_PX = int(os.environ.get("COLLAGE_CELL_MAX_PX", "800"))
_GAP_PX = 8
_BG_RGB = (24, 24, 24)
_JPEG_QUALITY = int(os.environ.get("COLLAGE_JPEG_QUALITY", "80"))


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
            font = _load_font(24)  # Tamanho de fonte ideal para células de 800px
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
    res_bytes = buf.getvalue()

    # Fallback dinâmico para garantir que a imagem caiba no limite de upload do Evolution API (Nginx 413)
    max_bytes = 700 * 1024  # 700 KB para caber com folga em um limite de 1MB (mesmo com codificação Base64)
    if len(res_bytes) > max_bytes:
        log.info(
            "Mosaico gerado possui %d bytes (limite de seguranca=%d). Iniciando compressao...",
            len(res_bytes),
            max_bytes,
        )
        # 1. Tenta reduzir a qualidade JPEG primeiro
        for q in [70, 60, 50]:
            buf = BytesIO()
            canvas.save(buf, format="JPEG", quality=q, optimize=True)
            res_bytes = buf.getvalue()
            if len(res_bytes) <= max_bytes:
                log.info(
                    "Mosaico reduzido com sucesso para %d bytes usando JPEG quality=%d",
                    len(res_bytes),
                    q,
                )
                return res_bytes

        # 2. Se ainda for muito grande, tenta redimensionar o canvas
        current_canvas = canvas
        for scale in [0.75, 0.5, 0.3]:
            new_w = int(canvas_w * scale)
            new_h = int(canvas_h * scale)
            resized = current_canvas.resize((new_w, new_h), Image.Resampling.LANCZOS)
            for q in [75, 60, 45]:
                buf = BytesIO()
                resized.save(buf, format="JPEG", quality=q, optimize=True)
                res_bytes = buf.getvalue()
                if len(res_bytes) <= max_bytes:
                    log.info(
                        "Mosaico reduzido para %d bytes redimensionando grid para %dx%d (escala=%0.2f, qualidade=%d)",
                        len(res_bytes),
                        new_w,
                        new_h,
                        scale,
                        q,
                    )
                    return res_bytes

        log.warning(
            "Nao foi possivel reduzir o mosaico abaixo do limite de seguranca de %d bytes. Tamanho final: %d",
            max_bytes,
            len(res_bytes),
        )

    return res_bytes
