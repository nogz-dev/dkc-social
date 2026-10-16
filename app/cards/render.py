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

from . import fotos, recorte

AQUI = Path(__file__).resolve().parent
MODELOS = ("noticia", "jogador", "rodada", "resultado", "heroi", "destaque", "confronto", "clube", "editorial", "arte", "tabela")

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
        LATIN = "U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD"
        EXT = "U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+0304,U+0308,U+0329,U+1D00-1DBF,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF"
        for peso, estilo in ((900, "italic"), (800, "italic"), (700, "normal"), (600, "normal"), (500, "normal")):
            for sub, rng in (("latin", LATIN), ("latin-ext", EXT)):
                arq = AQUI / "fonts" / f"barlow-condensed-{sub}-{peso}-{estilo}.woff2"
                b64 = base64.b64encode(arq.read_bytes()).decode()
                partes.append(f"@font-face{{font-family:Barlow;font-weight:{peso};font-style:{estilo};"
                              f"src:url(data:font/woff2;base64,{b64}) format('woff2');unicode-range:{rng}}}")
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
def cor_dominante(data_uri: str | None) -> str | None:
    """Cor principal de um escudo (ignora branco, preto e cinza)."""
    if not data_uri or "," not in data_uri:
        return None
    try:
        im = Image.open(io.BytesIO(base64.b64decode(data_uri.split(",", 1)[1]))).convert("RGBA")
        im.thumbnail((64, 64))
        cont: dict[tuple, int] = {}
        for r, g, b, a in im.getdata():
            if a < 200:
                continue
            mx, mn = max(r, g, b), min(r, g, b)
            if mx - mn < 40 or mx < 50:  # cinza, branco ou preto
                continue
            k = (r // 24 * 24, g // 24 * 24, b // 24 * 24)
            cont[k] = cont.get(k, 0) + 1
        if not cont:
            return None
        r, g, b = max(cont, key=cont.get)
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return None


def _escura(hex_: str) -> bool:
    h = hex_.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return (0.299 * r + 0.587 * g + 0.114 * b) < 150


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
    for r in d.get("linhas") or []:
        tarefas.append(opcional(r, "escudo", 120))
    await asyncio.gather(*tarefas)
    # cor da faixa: a do escudo do protagonista (vencedor/casa), senão a marca
    if not d.get("cor_faixa"):
        for k in ("escudo_destaque", "escudo_fora" if d.get("lado_destaque") == "fora" else "escudo_casa", "escudo"):
            c = cor_dominante(d.get(k)) if d.get(k) else None
            if c:
                d["cor_faixa"] = c
                break
    if d.get("cor_faixa"):
        d["faixa_escura"] = _escura(d["cor_faixa"])
    # confronto: cada lado na cor do seu escudo
    if modelo == "confronto":
        d.setdefault("cor_casa", cor_dominante(d.get("escudo_casa")) if d.get("escudo_casa") else None)
        d.setdefault("cor_fora", cor_dominante(d.get("escudo_fora")) if d.get("escudo_fora") else None)
    # escudo do protagonista em destaque (formato herói)
    if modelo == "heroi":
        d["escudo_destaque_img"] = d.get("escudo_fora") if d.get("lado_destaque") == "fora" else d.get("escudo_casa")
    # jogador recortado sobre o nosso cenário
    if d.get("foto") and not d.get("visual_ia") and (modelo in ("heroi", "destaque") or d.get("recortar")):
        d["recorte"] = await recorte.recortar(d["foto"])
        if not d["recorte"]:
            avisos.append("recorte do jogador falhou: foi usada a foto inteira")
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
        if d.get("linhas"):
            d["linhas"] = [dict(r) for r in d["linhas"]]
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
