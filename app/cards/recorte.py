"""Recorte do jogador (remove o fundo da foto).

A foto conferida vira só matéria-prima: o jogador recortado é composto sobre um
cenário nosso (cor do clube, tipografia, grafismos). Modelo isnet-general-use
(rembg), baixado no build da imagem Docker.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import os

from PIL import Image, ImageFilter

log = logging.getLogger("dkc.recorte")
MODELO = os.getenv("RECORTE_MODELO", "isnet-general-use")
_sessao = None
_lock = asyncio.Lock()


def _remover(dados: bytes) -> bytes | None:
    global _sessao
    from rembg import new_session, remove
    if _sessao is None:
        _sessao = new_session(MODELO)
    im = Image.open(io.BytesIO(dados)).convert("RGB")
    im.thumbnail((1400, 1400))
    out = remove(im, session=_sessao, post_process_mask=True)
    alfa = out.split()[3]
    # recorte ruim (quase nada ou quase tudo) = descarta
    hist = alfa.histogram()
    opaco = sum(hist[200:]) / max(1, sum(hist))
    if opaco < 0.06 or opaco > 0.85:
        return None
    # borda um pouco mais macia e corte no contorno da pessoa
    alfa = alfa.filter(ImageFilter.GaussianBlur(0.8))
    out.putalpha(alfa)
    bbox = alfa.point(lambda v: 255 if v > 40 else 0).getbbox()
    if bbox:
        out = out.crop(bbox)
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    return buf.getvalue()


async def recortar(data_uri: str | None) -> str | None:
    """Recebe a foto (data URI) e devolve o jogador recortado (PNG transparente) ou None."""
    if not data_uri or "," not in data_uri:
        return None
    dados = base64.b64decode(data_uri.split(",", 1)[1])
    async with _lock:  # um recorte por vez: o modelo é pesado
        try:
            png = await asyncio.to_thread(_remover, dados)
        except Exception:
            log.exception("[RECORTE] falhou")
            return None
    return f"data:image/png;base64,{base64.b64encode(png).decode()}" if png else None
