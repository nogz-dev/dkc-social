"""Arte dos cards feita pela OpenAI (gpt-image-2).

Divisão de trabalho (Nog, 15/10/2026): a OpenAI cria TODAS as imagens. Nós
entregamos a informação (texto exato, placar, escudos, foto conferida do
jogador) e conferimos o resultado:

1. briefing(): monta o pedido de arte a partir dos dados do slide.
2. A OpenAI cria a arte em 4:5 usando as referências (foto conferida do
   jogador, escudos, logo da liga). Nunca mandamos arte de outro veículo.
3. conferir(): o Claude olha a arte e confere se todo texto/número está
   certo e se não apareceu logo de emissora ou texto inventado. Se errar,
   pede de novo uma vez com a lista de erros.
4. O rodapé com o logo real do perfil, @ e fonte é carimbado por nós (logo
   desenhado por IA sai torto).

Se a OpenAI falhar ou errar duas vezes, o card sai no template próprio
(plano B) com aviso.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
from datetime import date
from typing import Any

import httpx
from PIL import Image

from . import fotos

log = logging.getLogger("dkc.openai")

BASE = os.getenv("OPENAI_BASE", "https://api.openai.com/v1")
MODELO = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-2")
TAMANHO = os.getenv("OPENAI_IMAGE_SIZE", "1088x1360")      # 4:5, múltiplos de 16
QUALIDADE = os.getenv("OPENAI_IMAGE_QUALITY", "high")
LIMITE_DIA = int(os.getenv("OPENAI_IMAGENS_DIA", "400"))     # trava de segurança de custo
MODELO_CONF = os.getenv("ARTE_MODELO_CONFERENCIA", "claude-sonnet-5-5")

_sem = asyncio.Semaphore(int(os.getenv("OPENAI_PARALELO", "3")))
_uso = {"dia": None, "n": 0}


class ArteErro(Exception):
    pass


def chave() -> str:
    return os.getenv("OPENAI_API_KEY", "").strip()


def ativo() -> bool:
    return bool(chave()) and os.getenv("IMAGENS_OPENAI", "1") != "0"


def status() -> dict:
    return {"ativo": ativo(), "modelo": MODELO, "hoje": _uso["n"] if _uso["dia"] == date.today() else 0,
            "limite_dia": LIMITE_DIA}


def _limpo(t: Any) -> str:
    return re.sub(r"\s+", " ", str(t or "").replace("*", "")).strip()


def _destaques(t: str) -> list[str]:
    return [x.strip() for x in re.findall(r"\*([^*]+)\*", t or "") if x.strip()]


# ------------------------------------------------------------- briefing --
ESTILO = """Direção de arte (siga à risca):
- Card vertical de Instagram 4:5 de um perfil brasileiro de futebol, no nível de TNT Sports Brasil e CazéTV: design editorial esportivo de transmissão, moderno, impactante, acabamento profissional de designer humano, NADA com cara de "feito por IA" (sem brilho plástico, sem pele de cera, sem elementos derretidos ou deformados).
- Tipografia: sans serif condensada pesada, itálica, caixa alta nas manchetes; hierarquia forte; uma ou duas palavras de destaque com marca-texto na cor da marca.
- Cores: base escura quase preta {bg}, destaque da marca {cor} (use com moderação, em fitas/marca-texto/detalhes), e as cores do clube protagonista quando houver.
- Grafismos de transmissão: faixas diagonais, fita adesiva com a etiqueta no topo à esquerda, granulado sutil de impressão, luz de estádio, profundidade.
- Reserve os 150 pixels de baixo como uma faixa escura LIMPA, sem nenhum texto, logo ou elemento: o rodapé com a marca é colocado depois.
- PROIBIDO: logo, marca d'água ou nome de qualquer emissora, portal ou outro perfil (TNT, ESPN, Globo, CazéTV, ge, etc.); logo de casa de aposta; texto que não esteja na lista abaixo; números inventados; @ de perfis; QR code.
- Escreva SOMENTE os textos listados, exatamente como estão (acentos, nomes e números idênticos). Nenhum outro texto legível."""


def briefing(modelo: str, d: dict, marca: dict, refs: list[str], correcoes: list[str] | None = None) -> str:
    linhas: list[str] = []
    tag = " · ".join(x for x in (_limpo(d.get("tag")), _limpo(d.get("tag2"))) if x)
    if tag:
        linhas.append(f'Etiqueta (fita no topo): "{tag}"')
    titulo = d.get("titulo") or ""
    if titulo:
        linhas.append(f'Manchete principal: "{_limpo(titulo)}"')
        dest = _destaques(titulo)
        if dest:
            linhas.append("Palavras com marca-texto na cor da marca: " + ", ".join(f'"{x}"' for x in dest))
    if d.get("palavra"):
        linhas.append(f'Palavra gigante de fundo, vazada/contorno, atrás do jogador: "{_limpo(d["palavra"]).upper()}"')
    if d.get("marca_dagua"):
        linhas.append(f'Palavra gigante de fundo, só contorno: "{_limpo(d["marca_dagua"]).upper()}"')
    if d.get("legenda_jogador"):
        linhas.append(f'Linha pequena sob a manchete: "{_limpo(d["legenda_jogador"])}"')
    if d.get("casa") and d.get("fora"):
        if d.get("gols_casa") is not None and d.get("gols_fora") is not None:
            linhas.append(f'Placar em destaque: "{_limpo(d["casa"])}" {d["gols_casa"]} x {d["gols_fora"]} "{_limpo(d["fora"])}" '
                          f'(mandante à esquerda, visitante à direita, cada um com seu escudo)')
            if d.get("autores_casa"):
                linhas.append(f'Gols de {_limpo(d["casa"])} (lado esquerdo, letra pequena): ' + " · ".join(_limpo(x) for x in d["autores_casa"]))
            if d.get("autores_fora"):
                linhas.append(f'Gols de {_limpo(d["fora"])} (lado direito, letra pequena): ' + " · ".join(_limpo(x) for x in d["autores_fora"]))
            if d.get("penaltis"):
                linhas.append(f'Selo: "{_limpo(d["penaltis"])}"')
        else:
            linhas.append(f'Confronto: "{_limpo(d["casa"])}" x "{_limpo(d["fora"])}", horário "{_limpo(d.get("hora"))}" em destaque')
            if d.get("info"):
                linhas.append(f'Linha de informação: "{_limpo(d["info"])}"')
    for f in (d.get("fatos") or [])[:4]:
        linhas.append(f'Bloco de dado: número grande "{_limpo(f.get("n"))}" com legenda "{_limpo(f.get("l"))}"')
    if d.get("data"):
        linhas.append(f'Data: "{_limpo(d["data"])}"')
    jogos = d.get("jogos") or []
    if jogos:
        linhas.append("Lista de jogos em tabela limpa, uma linha por jogo, horário à esquerda, escudos ao lado dos nomes:")
        for j in jogos:
            linhas.append(f'  - "{_limpo(j.get("hora"))}"  "{_limpo(j.get("casa"))}" x "{_limpo(j.get("fora"))}"')
    if d.get("texto"):
        linhas.append(f'Texto de apoio: "{_limpo(d["texto"])}"')

    cena = {
        "heroi": "Composição herói: o jogador recortado e grande, à direita, sobre um cenário com a cor do clube, luz dramática de estádio; manchete à esquerda; placar num painel na parte de baixo.",
        "resultado": "Composição de fim de jogo: placar gigante como protagonista, escudos grandes, faixa diagonal com a cor do clube vencedor.",
        "jogador": "Composição de perfil de jogador: o jogador em destaque, faixa com a cor do clube, dados em blocos.",
        "rodada": "Composição de agenda: lista de jogos legível, limpa e alinhada, com cara de grade de transmissão.",
        "noticia": "Composição de notícia urgente: manchete enorme como protagonista, imagem de apoio coerente com o tema.",
    }.get(modelo, "Composição de notícia esportiva.")

    pessoa = ("Use a pessoa da imagem de referência do jogador como o jogador do card: mantenha o rosto, o corpo e a identidade "
              "fiéis à foto (é uma fotografia real conferida). Não invente outra pessoa e não altere o rosto."
              if any(r.startswith("foto do jogador") for r in refs) else
              "Não desenhe nenhuma pessoa real reconhecível (nenhum jogador, técnico ou celebridade). Use tipografia, escudos, "
              "estádio, bola, torcida desfocada sem rostos, texturas e grafismos.")
    escudos = ("Reproduza os escudos das referências fielmente, sem redesenhar, sem trocar cores nem inventar detalhes."
               if any("escudo" in r or "logo da competição" in r for r in refs) else
               "Não desenhe escudos de clubes.")

    partes = [
        f"Crie a arte de um card para o perfil @{marca.get('handle', 'seutipster.app')} ({marca.get('nome', '')}).",
        ESTILO.format(bg=marca.get("bg", "#0A0C09"), cor=marca.get("cor", "#95FF50")),
        cena, pessoa, escudos,
    ]
    if refs:
        partes.append("Imagens de referência, na ordem: " + "; ".join(f"imagem {i}: {r}" for i, r in enumerate(refs, 1)) + ".")
    partes.append("TEXTOS DO CARD (os únicos permitidos):\n" + "\n".join(linhas))
    if correcoes:
        partes.append("A versão anterior saiu com estes erros. Corrija todos:\n- " + "\n- ".join(correcoes))
    return "\n\n".join(partes)


def esperados(d: dict) -> list[str]:
    out = []
    for k in ("titulo", "legenda_jogador", "casa", "fora", "hora", "palavra", "marca_dagua"):
        if d.get(k):
            out.append(_limpo(d[k]))
    if d.get("gols_casa") is not None and d.get("gols_fora") is not None:
        out.append(f"placar {d['gols_casa']} x {d['gols_fora']}")
    for k in ("autores_casa", "autores_fora"):
        out += [_limpo(x) for x in d.get(k) or []]
    for f in d.get("fatos") or []:
        out += [_limpo(f.get("n")), _limpo(f.get("l"))]
    for j in d.get("jogos") or []:
        out.append(f'{_limpo(j.get("hora"))} {_limpo(j.get("casa"))} x {_limpo(j.get("fora"))}')
    return [x for x in out if x]


# ----------------------------------------------------------- referências --
async def referencias(d: dict) -> tuple[list[tuple[bytes, str]], list[str]]:
    """Baixa a foto conferida e os escudos. Só material limpo (nunca arte de terceiros)."""
    pedidos: list[tuple[str, str]] = []
    if d.get("foto") and d.get("foto_conferida", True):
        pedidos.append((d["foto"], f"foto do jogador {_limpo((d.get('jogador') or {}).get('nome') or d.get('legenda_jogador') or '')}".strip()))
    if d.get("casa") and d.get("escudo_casa"):
        pedidos.append((d["escudo_casa"], f"escudo do {_limpo(d['casa'])}"))
    if d.get("fora") and d.get("escudo_fora"):
        pedidos.append((d["escudo_fora"], f"escudo do {_limpo(d['fora'])}"))
    if d.get("escudo"):
        pedidos.append((d["escudo"], "escudo do clube da notícia"))
    if d.get("logo_liga"):
        pedidos.append((d["logo_liga"], "logo da competição"))
    for j in (d.get("jogos") or [])[:6]:
        for lado in ("casa", "fora"):
            if j.get(f"escudo_{lado}"):
                pedidos.append((j[f"escudo_{lado}"], f"escudo do {_limpo(j.get(lado))}"))
    imgs, nomes = [], []
    for url, nome in pedidos[:16]:
        try:
            dados, mime = await fotos.baixar(url, 1536 if nome.startswith("foto") else 512)
        except Exception as e:
            log.info("[ARTE] referência ignorada (%s): %s", nome, e)
            continue
        im = Image.open(io.BytesIO(dados))
        buf = io.BytesIO()
        im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB").save(buf, "PNG")
        imgs.append((buf.getvalue(), "image/png"))
        nomes.append(nome)
    return imgs, nomes


# ---------------------------------------------------------------- OpenAI --
async def _chamar(prompt: str, refs: list[tuple[bytes, str]]) -> bytes:
    hoje = date.today()
    if _uso["dia"] != hoje:
        _uso.update(dia=hoje, n=0)
    if LIMITE_DIA and _uso["n"] >= LIMITE_DIA:
        raise ArteErro(f"limite diário de {LIMITE_DIA} imagens da OpenAI atingido")
    _uso["n"] += 1
    cab = {"Authorization": f"Bearer {chave()}"}
    params = {"model": MODELO, "prompt": prompt, "size": TAMANHO, "quality": QUALIDADE,
              "output_format": "jpeg", "output_compression": "95", "n": "1"}
    async with _sem:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=20)) as c:
            if refs:
                campo = "image[]" if len(refs) > 1 else "image"
                arquivos = [(campo, (f"ref{i}.png", b, m)) for i, (b, m) in enumerate(refs, 1)]
                r = await c.post(f"{BASE}/images/edits", headers=cab, data=params, files=arquivos)
            else:
                r = await c.post(f"{BASE}/images/generations", headers=cab, json=params)
    if r.status_code >= 400:
        try:
            err = r.json().get("error") or {}
        except Exception:
            err = {}
        raise ArteErro(f"OpenAI {r.status_code}: {err.get('code') or ''} {(err.get('message') or r.text)[:200]}".strip())
    b64 = ((r.json().get("data") or [{}])[0]).get("b64_json")
    if not b64:
        raise ArteErro("OpenAI não devolveu imagem")
    return base64.b64decode(b64)


# ------------------------------------------------------------ conferência --
PROMPT_CONF = """Você é o revisor final de um card de Instagram de futebol antes de publicar.
Textos que DEVEM aparecer, corretos (acentos, grafia e números idênticos; pode estar em caixa alta):
{lista}

Responda SOMENTE com JSON:
{{"ok": true|false,
 "erros": ["cada problema em até 12 palavras"]}}
Marque ok=false se: algum texto obrigatório faltar ou estiver errado; aparecer número, placar ou nome diferente do pedido; aparecer texto legível inventado (exceto palavras soltas ilegíveis de fundo); aparecer logo ou nome de emissora, portal, casa de aposta ou outro perfil; houver pessoa ou rosto deformado; o card parecer amador."""


async def conferir(img: bytes, d: dict) -> dict:
    k = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not k:
        return {"ok": True, "erros": [], "sem_revisor": True}
    im = Image.open(io.BytesIO(img)).convert("RGB")
    im.thumbnail((1100, 1100))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    lista = "\n".join(f"- {x}" for x in esperados(d)) or "- (nenhum texto obrigatório)"
    try:
        async with httpx.AsyncClient(timeout=90) as c:
            r = await c.post(os.getenv("ANTHROPIC_BASE", "https://api.anthropic.com") + "/v1/messages",
                             headers={"x-api-key": k, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                             json={"model": MODELO_CONF, "max_tokens": 500, "messages": [{"role": "user", "content": [
                                 {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                              "data": base64.b64encode(buf.getvalue()).decode()}},
                                 {"type": "text", "text": PROMPT_CONF.format(lista=lista)}]}]})
        txt = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        m = re.search(r"\{.*\}", txt, re.S)
        v = json.loads(m.group(0)) if m else {}
    except Exception as e:
        return {"ok": False, "erros": [f"revisão falhou: {type(e).__name__}"]}
    return {"ok": bool(v.get("ok")), "erros": [str(x) for x in (v.get("erros") or [])][:8]}


# ---------------------------------------------------------------- principal --
async def criar_arte(modelo: str, d: dict, marca: dict) -> tuple[bytes, list[str]]:
    """Devolve (jpeg 1080x1350 sem rodapé, avisos). Levanta ArteErro se não sair arte confiável."""
    refs, nomes = await referencias(d)
    avisos: list[str] = []
    correcoes: list[str] | None = None
    ultimo = ""
    for tentativa in range(2):
        try:
            img = await _chamar(briefing(modelo, d, marca, nomes, correcoes), refs)
        except ArteErro as e:
            msg = str(e)
            # recusa por foto de pessoa real: tenta sem a foto (card sem jogador)
            if refs and nomes and nomes[0].startswith("foto") and ("moderation" in msg or "safety" in msg):
                refs, nomes = refs[1:], nomes[1:]
                avisos.append("OpenAI recusou usar a foto do jogador: arte sem jogador")
                continue
            raise
        v = await conferir(img, d)
        log.info("[ARTE] %s tentativa %d · %s", modelo, tentativa + 1, v)
        if v["ok"]:
            if tentativa:
                avisos.append("arte refeita após conferência de texto")
            return _ajustar(img), avisos
        correcoes = v["erros"] or ["texto diferente do pedido"]
        ultimo = "; ".join(correcoes)
    raise ArteErro(f"arte da OpenAI reprovada na conferência: {ultimo}")


def _ajustar(img: bytes) -> bytes:
    im = Image.open(io.BytesIO(img)).convert("RGB")
    alvo = 1080 / 1350
    w, h = im.size
    if abs(w / h - alvo) > 0.005:  # corta o excesso mantendo o centro
        if w / h > alvo:
            nw = int(h * alvo)
            im = im.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
        else:
            nh = int(w / alvo)
            im = im.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))
    im = im.resize((1080, 1350), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=94)
    return out.getvalue()
