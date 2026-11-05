"""Gerador de cards 1080×1350 (formato retrato do Instagram).

Modelos HTML (Jinja2) em app/cards/modelos, renderizados com Chromium headless
(Playwright). Fotos e escudos são baixados no servidor e embutidos no HTML.

Direção de arte "AO VIVO" (Nog, 05/11/2026): preto sinal, verde Tipster como
estrutura (placa diagonal, régua, selo), Anton nas manchetes e placares,
Barlow Condensed nas placas e nomes, Inter no apoio. Base: modelos/vivo.html.

Formatos: capa_rodada · rodada · noticia · urgente · resultado · resumo · tabela
(os nomes antigos — destaque, confronto, clube, editorial, jogador, heroi —
continuam aceitos e saem no visual novo).
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
NOVOS = ("capa_rodada", "rodada", "noticia", "urgente", "resultado", "resumo", "tabela")
# nomes antigos: continuam válidos (pauta do chat, posts que já estão na fila) e saem no visual novo
ALIAS = {"destaque": "noticia", "confronto": "noticia", "clube": "noticia", "editorial": "noticia",
         "jogador": "noticia", "heroi": "resultado"}
MODELOS = NOVOS + tuple(ALIAS) + ("arte",)

# ------------------------------------------------------------------ marcas --
# Cada perfil posta com a própria identidade. Chave = @ do Instagram.
MARCAS: dict[str, Any] = {
    "seutipster.app": {
        "nome": "SeuTipster", "cor": "#86FF5C", "cor_ink": "#000000",   # verde Tipster (manual AO VIVO)
        "bg": "#000000", "ink": "#FFFFFF", "mute": "#A3A3A3", "logo": "seutipster.png",
        "assinatura": ("SEU", "TIPSTER"), "selo": "ST",
    },
}
MARCAS["seutipsterapp"] = MARCAS["seutipster.app"]  # @ novo (perfil trocado em 03/11/2026)
MARCA_PADRAO = {
    "nome": "DKC", "cor": "#FFD400", "cor_ink": "#000000",
    "bg": "#000000", "ink": "#FFFFFF", "mute": "#A3A3A3", "logo": "",
    "assinatura": ("DKC", "SOCIAL"), "selo": "DK",
}

_env = Environment(loader=FileSystemLoader(AQUI / "modelos"), autoescape=select_autoescape(["html"]))


def _destaque(txt: Any) -> Markup:
    """*palavra* vira destaque na cor da marca."""
    s = html.escape(str(txt or ""))
    s = re.sub(r"\*(.+?)\*", r"<em>\1</em>", s)
    return Markup(s.replace("\n", "<br>"))


_env.filters["destaque"] = _destaque


def _um_destaque(txt: Any) -> Markup:
    """Manual AO VIVO: UM único trecho em verde. O 1º *trecho* vira destaque; outros asteriscos somem."""
    s = html.escape(str(txt or "")).strip()
    # palavra com hífen não quebra no meio da linha ("ex-Barcelona", "Atlético-MG")
    s = re.sub(r"([^\s*<>]+-[^\s*<>]+)", r'<span class="nb">\1</span>', s)
    s = re.sub(r"\*(.+?)\*", r"<em>\1</em>", s, count=1).replace("*", "")
    return Markup(s.replace("\n", "<br>"))


def _sem_destaque(txt: Any) -> str:
    return str(txt or "").replace("*", "").strip()


def _hora_br(h: Any) -> str:
    """"19:30" → "19h30" (como o torcedor escreve)."""
    s = str(h or "").strip()
    return s.replace(":", "h") if re.fullmatch(r"\d{1,2}:\d{2}", s) else s


def _minutos(txt: Any) -> str:
    return str(txt or "").replace("'", "’")


_env.filters.update(um_destaque=_um_destaque, sem_destaque=_sem_destaque, hora_br=_hora_br, minutos=_minutos)

_fontes_css: str | None = None
_logos: dict[str, str] = {}

LATIN = ("U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,"
         "U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD")
EXT = ("U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+0304,U+0308,U+0329,U+1D00-1DBF,U+1E00-1E9F,"
       "U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF")


def _woff2(familia: str, arq: str, peso: int, estilo: str, rng: str) -> str:
    b64 = base64.b64encode((AQUI / "fonts" / arq).read_bytes()).decode()
    return (f"@font-face{{font-family:{familia};font-weight:{peso};font-style:{estilo};font-display:block;"
            f"src:url(data:font/woff2;base64,{b64}) format('woff2');unicode-range:{rng}}}")


def _fontes() -> str:
    """Inter (apoio, rodapé) · Barlow Condensed (placas, nomes, horários) · Anton (manchete, placar, numeral)."""
    global _fontes_css
    if _fontes_css is None:
        partes = []
        for peso, arq in ((900, "Black"), (800, "ExtraBold"), (700, "Bold"), (600, "SemiBold"), (500, "Medium"), (400, "Regular")):
            b64 = base64.b64encode((AQUI / "fonts" / f"Inter-{arq}.otf").read_bytes()).decode()
            partes.append(f"@font-face{{font-family:CardInter;font-weight:{peso};font-display:block;"
                          f"src:url(data:font/otf;base64,{b64})}}")
        for peso, estilo in ((900, "italic"), (800, "italic"), (800, "normal"), (700, "normal"), (600, "normal"), (500, "normal")):
            for sub, rng in (("latin", LATIN), ("latin-ext", EXT)):
                partes.append(_woff2("Barlow", f"barlow-condensed-{sub}-{peso}-{estilo}.woff2", peso, estilo, rng))
        for sub, rng in (("latin", LATIN), ("latin-ext", EXT)):
            partes.append(_woff2("Anton", f"anton-{sub}-400-normal.woff2", 400, "normal", rng))
        _fontes_css = "\n".join(partes)
    return _fontes_css


def marca_de(handle: str | None) -> dict[str, Any]:
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
def _adaptar(modelo: str, d: dict) -> str:
    """Nome antigo → formato AO VIVO, e campos antigos → campos novos. Devolve o template a usar."""
    m = ALIAS.get(modelo, modelo)
    if m == "noticia":
        # campos dos cards antigos (tag/tag2/linha_fina) viram kicker/competição/apoio
        d.setdefault("kicker", d.get("tag") or "Notícia")
        d.setdefault("competicao", d.get("tag2"))
        d.setdefault("apoio", d.get("linha_fina"))
        sinais = (str(d.get("editoria") or ""), str(d.get("tag") or ""), str(d.get("kicker") or ""))
        if d.get("urgente") or any(s.strip().lower() == "urgente" for s in sinais):
            m = "urgente"
    if m in ("noticia", "urgente"):
        # só a escrita (pedido do Nog, 05/11/2026): nenhuma foto, nenhum escudo
        for k in ("foto", "credito_foto", "escudo", "escudo_casa", "escudo_fora", "logo_liga"):
            d[k] = None
        d["texto"] = d.get("texto") or d.get("apoio")
    if m in ("capa_rodada", "resultado"):
        d["foto"], d["credito_foto"] = None, None  # jogos e placar: só escudos, sem foto de jogador
    if d.get("foto") and d.get("foto_tipo") == "retrato":
        d["foto"], d["credito_foto"] = None, None  # rosto pequeno da API nunca vai para a arte
    if m == "resultado":
        d.setdefault("competicao", " · ".join(x for x in (d.get("tag2"), d.get("fase")) if x) or None)
    rosto = d.get("rosto")
    if isinstance(rosto, (list, tuple)) and len(rosto) == 2:
        try:
            fx, fy = (float(v) for v in rosto)
            d["rosto"] = [fx / 100 if fx > 1 else fx, fy / 100 if fy > 1 else fy]
        except (TypeError, ValueError):
            d["rosto"] = None
    else:
        d["rosto"] = None
    return m


async def _resolver_imagens(modelo: str, d: dict) -> list[str]:
    """Troca URLs por data URIs. Foto principal é obrigatória quando informada;
    escudos, retrato e logos que falharem só somem do card. Devolve avisos."""
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
    for j in (d.get("jogos") or []) + ([d["jogo_destaque"]] if isinstance(d.get("jogo_destaque"), dict) else []):
        tarefas.append(opcional(j, "escudo_casa", 240))
        tarefas.append(opcional(j, "escudo_fora", 240))
    for r in d.get("linhas") or []:
        tarefas.append(opcional(r, "escudo", 120))
    await asyncio.gather(*tarefas)
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
        for k in ("jogos", "linhas", "ligas"):
            if d.get(k):
                d[k] = [dict(x) for x in d[k]]
        if isinstance(d.get("jogo_destaque"), dict):
            d["jogo_destaque"] = dict(d["jogo_destaque"])
        tpl_nome = _adaptar(modelo, d)
        avisos = await _resolver_imagens(tpl_nome, d)
        tpl = _env.get_template(f"{tpl_nome}.html")
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
