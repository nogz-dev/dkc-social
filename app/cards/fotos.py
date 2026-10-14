"""Download seguro de imagens (fotos, escudos) para montar os cards.

- só http/https, nunca IP privado/loopback (anti-SSRF)
- limite de tamanho e tempo
- devolve data URI pronto para o HTML do card (JPEG/PNG reduzido)
"""
from __future__ import annotations

import asyncio
import base64
import io
import ipaddress
import os
import socket
from urllib.parse import urlparse

import httpx
from PIL import Image

MAX_BYTES = 12 * 1024 * 1024
UA = "Mozilla/5.0 (compatible; DKCSocial/1.0; +https://dkc.social)"


class FotoErro(RuntimeError):
    pass


def _host_publico(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise FotoErro(f"não consegui encontrar o site da imagem ({host})")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


async def baixar(url: str, max_lado: int = 1600) -> tuple[bytes, str]:
    """Baixa e normaliza a imagem. Retorna (bytes, mime)."""
    if url.startswith("data:image/"):
        head, b64 = url.split(",", 1)
        return base64.b64decode(b64), head[5:].split(";")[0]
    if url.startswith("file://") and os.getenv("CARD_ALLOW_FILE") == "1":
        with open(url[7:], "rb") as f:
            dados = f.read()
    else:
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise FotoErro(f"endereço de imagem inválido: {url[:80]}")
        permite_privado = os.getenv("CARD_ALLOW_PRIVATE") == "1"
        if not permite_privado and not await asyncio.to_thread(_host_publico, u.hostname):
            raise FotoErro("endereço de imagem não permitido")
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent": UA}) as c:
            async with c.stream("GET", url) as r:
                if r.status_code >= 400:
                    raise FotoErro(f"imagem respondeu {r.status_code}: {url[:80]}")
                if not permite_privado and r.url.host and not await asyncio.to_thread(_host_publico, r.url.host):
                    raise FotoErro("redirecionamento para endereço não permitido")
                partes, total = [], 0
                async for ch in r.aiter_bytes():
                    total += len(ch)
                    if total > MAX_BYTES:
                        raise FotoErro("imagem grande demais")
                    partes.append(ch)
                dados = b"".join(partes)
    try:
        im = Image.open(io.BytesIO(dados))
        im.load()
    except Exception:
        raise FotoErro(f"o arquivo não é uma imagem válida: {url[:80]}")
    tem_alfa = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    im = im.convert("RGBA" if tem_alfa else "RGB")
    im.thumbnail((max_lado, max_lado))
    out = io.BytesIO()
    if tem_alfa:
        im.save(out, "PNG", optimize=True)
        return out.getvalue(), "image/png"
    im.save(out, "JPEG", quality=90)
    return out.getvalue(), "image/jpeg"


async def data_uri(url: str | None, max_lado: int = 1600) -> str | None:
    if not url:
        return None
    dados, mime = await baixar(url, max_lado)
    return f"data:{mime};base64,{base64.b64encode(dados).decode()}"
