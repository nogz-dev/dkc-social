"""Gerador de cards 1080×1350 (formato retrato do Instagram).

Modelos HTML (Jinja2) em app/cards/modelos, renderizados com Chromium headless
(Playwright). Fotos e escudos são baixados no servidor e embutidos no HTML.

Modelos: noticia · jogador · rodada · resultado
"""
from __future__ import annotations

import asyncio
import base64
import html
import io
import re
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from PIL import Image

from . import fotos

AQUI = Path(__file__).resolve().parent
MODELOS = ("noticia", "jogador", "rodada", "resultado")

# ------------------------------------------------------------------ marcas --
# Cada perfil posta com a própria identidade. Chave = @ do Instagram.
MARCAS: dict[str, dict[str, str]] = {
    "seutipster.app": {
        "nome": "SeuTipster", "cor": "#95FF50", "cor_ink": "#0A0C09",
        "bg": "#0A0C09", "ink": "#F2F2F2", "mute": "#8C9188", "logo": "seutipster.png",
    },
}
MARCA_PADRAO = {
    "nome": "DKC", "cor": "#FDD918", "cor_ink": "#0A0A0A",
    "bg": "#0A0A0A", "ink": "#FAFAFA", "mute": "#8C8C8C", "logo": "",
}

_env = Environment(loader=FileSystemLoader(AQUI / "modelos"), autoescape=select_autoescape(["html"]))


def _destaque(txt: Any) -> Markup:
    """*palavra* vira destaque na cor da marca."""
    s = html.escape(str(txt or ""))
    s = re.sub(r"\*(.+?)\*", r"<em>\1</em>", s)
    return Markup(s.replace("\n", "<br>"))


_env.filters["destaque"] = _destaque

_fontes_css: str | None = None
_logos: dict[str, str] = {}


def _fontes() -> str:
    global _fontes_css
    if _fontes_css is None:
        partes = []
        for peso, arq in ((900, "Black"), (800, "ExtraBold"), (700, "Bold"), (600, "SemiBold"), (500, "Medium"), (400, "Regular")):
            b64 = base64.b64encode((AQUI / "fonts" / f"Inter-{arq}.otf").read_bytes()).decode()
            partes.append(f"@font-face{{font-family:CardInter;font-weight:{peso};src:url(data:font/otf;base64,{b64})}}")
        _fontes_css = "\n".join(partes)
    return _fontes_css


def marca_de(handle: str | None) -> dict[str, str]:
    h = (handle or "").lstrip("@").lower()
    m = dict(MARCAS.get(h, MARCA_PADRAO))
    m["handle"] = h or "dkc"
    arq = m.get("logo")
    if arq:
        if arq not in _logos:
            _logos[arq] = "data:image/png;base64," + base64.b64encode((AQUI / "marcas" / arq).read_bytes()).decode()
        m["logo"] = _logos[arq]
    return m


# --------------------------------------------------------------- imagens --
async def _resolver_imagens(modelo: str, d: dict) -> list[str]:
    """Troca URLs por data URIs. Foto principal é obrigatória quando informada;
    escudos e logos que falharem só somem do card. Devolve avisos."""
    avisos: list[str] = []

    async def opcional(obj: dict, chave: str, lado: int = 400):
        if obj.get(chave):
            try:
                obj[chave] = await fotos.data_uri(obj[chave], lado)
            except Exception as e:
                avisos.append(f"{chave}: {e}")
                obj[chave] = None

    tarefas = []
    if d.get("foto"):
        async def principal():
            d["foto"] = await fotos.data_uri(d["foto"], 1600)
        tarefas.append(principal())
    for k in ("escudo", "logo_liga", "escudo_casa", "escudo_fora"):
        tarefas.append(opcional(d, k))
    for j in d.get("jogos") or []:
        tarefas.append(opcional(j, "escudo_casa"))
        tarefas.append(opcional(j, "escudo_fora"))
    await asyncio.gather(*tarefas)
    if not d.get("foto"):
        avisos.append("card sem foto")
    return avisos


# ----------------------------------------------------------------- render --
class Renderizador:
    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(2)

    async def _navegador(self):
        async with self._lock:
            if self._browser is None or not self._browser.is_connected():
                from playwright.async_api import async_playwright
                if self._pw is None:
                    self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            return self._browser

    async def fechar(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    async def html(self, modelo: str, dados: dict, handle: str | None, fonte: str = "") -> tuple[str, list[str]]:
        if modelo not in MODELOS:
            raise ValueError(f"modelo desconhecido: {modelo} (use {', '.join(MODELOS)})")
        d = dict(dados)
        if d.get("jogos"):
            d["jogos"] = [dict(j) for j in d["jogos"]]
        avisos = await _resolver_imagens(modelo, d)
        tpl = _env.get_template(f"{modelo}.html")
        return tpl.render(d=d, marca=marca_de(handle), fonte=fonte or d.get("fonte", ""), fontes=Markup(_fontes())), avisos

    async def png(self, modelo: str, dados: dict, handle: str | None, fonte: str = "") -> tuple[bytes, list[str]]:
        doc, avisos = await self.html(modelo, dados, handle, fonte)
        browser = await self._navegador()
        async with self._sem:
            ctx = await browser.new_context(viewport={"width": 1080, "height": 1350}, device_scale_factor=1)
            try:
                page = await ctx.new_page()
                await page.set_content(doc, wait_until="load", timeout=30000)
                await page.wait_for_function("window.__pronto === true", timeout=20000)
                shot = await page.screenshot(type="png", clip={"x": 0, "y": 0, "width": 1080, "height": 1350})
            finally:
                await ctx.close()
        # JPEG de alta qualidade: arquivo bem menor para o Publora/Instagram
        out = io.BytesIO()
        Image.open(io.BytesIO(shot)).convert("RGB").save(out, "JPEG", quality=93, optimize=True)
        return out.getvalue(), avisos


renderizador = Renderizador()
