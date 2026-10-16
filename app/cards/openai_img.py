"""Arte dos cards feita 100% pela OpenAI (gpt-image-2).

Decisão do Nog (16/10/2026): os templates próprios deixam de criar arte. A OpenAI
cria a imagem inteira (visual + textos + placar + escudos). Nós só:
1. entregamos o briefing: manual da marca (brands/<marca>/brand-guidelines.md),
   textos exatos, placar, e como referência a foto conferida do jogador e os
   escudos (nunca arte de outro veículo);
2. revisamos (Claude): texto/número errado, logo de emissora, rosto deformado.
   Erro = a OpenAI refaz uma vez com a lista. Se ainda tiver erro, a arte vai
   para a fila marcada "revisar a arte" (não publica sozinha);
3. aplicamos o logotipo oficial no rodapé (o manual proíbe redesenhar o logo).

Se a OpenAI não devolver imagem, o post não é criado (o robô tenta de novo).
IMAGENS_OPENAI=0 é só a chave de emergência que volta aos templates.
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
from functools import lru_cache
from pathlib import Path
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
RAIZ = Path(__file__).resolve().parents[2]
MANUAIS = {"seutipster.app": "seu-tipster"}   # @ do perfil → pasta do manual
AVISO_REVISAR = "revisar a arte"

_sem = asyncio.Semaphore(int(os.getenv("OPENAI_PARALELO", "3")))
_uso: dict[str, Any] = {"dia": None, "n": 0, "ultimo_erro": None}


class ArteErro(Exception):
    pass


def chave() -> str:
    return os.getenv("OPENAI_API_KEY", "").strip()


def desligado() -> bool:
    """Chave de emergência: IMAGENS_OPENAI=0 volta aos templates próprios."""
    return os.getenv("IMAGENS_OPENAI", "1") == "0"


def status() -> dict:
    return {"responsavel_pela_arte": "templates (emergência)" if desligado() else "OpenAI",
            "chave_configurada": bool(chave()), "modelo": MODELO,
            "hoje": _uso["n"] if _uso["dia"] == date.today() else 0, "limite_dia": LIMITE_DIA,
            "ultimo_erro": _uso["ultimo_erro"]}


async def testar_chave() -> dict:
    """Confere a chave sem gastar imagem (lista os modelos)."""
    if not chave():
        return {"ok": False, "erro": "OPENAI_API_KEY não configurada no Railway"}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{BASE}/models/{MODELO}", headers={"Authorization": f"Bearer {chave()}"})
        if r.status_code == 200:
            return {"ok": True}
        return {"ok": False, "erro": f"OpenAI {r.status_code}: {((r.json().get('error') or {}).get('message') or r.text)[:200]}"}
    except Exception as e:
        return {"ok": False, "erro": f"sem conexão com a OpenAI: {type(e).__name__}"}


def _limpo(t: Any) -> str:
    return re.sub(r"\s+", " ", str(t or "").replace("*", "")).strip()


def _destaques(t: str) -> list[str]:
    return [x.strip() for x in re.findall(r"\*([^*]+)\*", t or "") if x.strip()]


# ----------------------------------------------------------------- manual --
@lru_cache(maxsize=8)
def _secoes(pasta: str) -> dict[str, str]:
    arq = RAIZ / "brands" / pasta / "brand-guidelines.md"
    if not arq.exists():
        return {}
    txt = re.sub(r"<!--.*?-->", "", arq.read_text(encoding="utf-8"), flags=re.S)
    txt = re.sub(r"^# .*$", "", txt, flags=re.M)
    partes = re.split(r"^## ", txt, flags=re.M)
    return {p.split("\n", 1)[0].strip().upper(): (p.split("\n", 1)[1].strip() if "\n" in p else "") for p in partes[1:]}


TIPO_POR_MODELO = {"rodada": "A. AGENDA", "resultado": "D. RESULTADOS", "heroi": "D. RESULTADOS",
                   "confronto": "B. PRÉ-JOGO"}


def manual(handle: str | None, modelo: str, pre_jogo: bool = False) -> str:
    pasta = MANUAIS.get((handle or "").lstrip("@").lower())
    s = _secoes(pasta) if pasta else {}
    if not s:
        return ""
    pega = lambda prefixo: next((v for k, v in s.items() if k.startswith(prefixo)), "")
    alvo = "B. PRÉ-JOGO" if pre_jogo else TIPO_POR_MODELO.get(modelo, "C. NOTÍCIAS")
    m = re.search(r"^### " + re.escape(alvo) + r".*?(?=^### |\Z)", pega("5. TIPOS"), flags=re.M | re.S | re.I)
    partes = [pega("CORES OFICIAIS"), pega("1. IDENTIDADE"), pega("2. DIREÇÃO DE ARTE"), m.group(0) if m else ""]
    return "\n\n".join(p for p in partes if p)


# --------------------------------------------------------------- briefing --
REGRAS = """Regras fixas desta arte:
- Card vertical de Instagram 4:5, campanha esportiva premium, cinematográfica, com identidade própria (não imite nenhum canal ou emissora). Acabamento de designer humano, nada com cara de IA genérica (sem brilho plástico, pele de cera, objetos derretidos ou deformados).
- Tipografia moderna e impactante, sans serif condensada pesada, hierarquia clara, textos grandes e legíveis.
- Cores: base {bg}, destaque da marca {cor} e as cores do clube protagonista quando houver.
- Reserve os 150 pixels de baixo como faixa escura LIMPA: o logotipo oficial é aplicado ali depois. Não desenhe o logotipo da marca em lugar nenhum.
- PROIBIDO: logo, marca d'água ou nome de emissora, portal ou outro perfil; logo de casa de aposta; selo promocional; QR code; @ de perfis; texto que não esteja na lista; número inventado.
- Escreva SOMENTE os textos da lista, exatamente como estão (acentos, nomes e números idênticos)."""

CENAS = {
    "heroi": "Composição herói: jogador grande à direita sobre cenário na cor do clube, luz dramática; manchete à esquerda; placar num painel embaixo.",
    "destaque": "Capa editorial de notícia com o jogador em destaque e a manchete forte embaixo.",
    "resultado": "Composição de fim de jogo limpa: placar em evidência, escudos grandes, destaque para o vencedor.",
    "confronto": "Pré-jogo: os dois clubes frente a frente, atmosfera de grande confronto, escudos grandes, data e horário claros.",
    "jogador": "Perfil de jogador: jogador em destaque, dados em blocos organizados.",
    "clube": "Notícia de clube: escudo grande do clube como protagonista, cenário na cor do clube.",
    "editorial": "Capa editorial de notícia: manchete enorme como protagonista, grafismo forte e coerente com o tema.",
    "rodada": "Agenda: lista de jogos organizada e de leitura rápida, horários de Brasília, escudos ao lado dos nomes.",
    "noticia": "Capa editorial de notícia: manchete forte, imagem coerente com o tema.",
}


def briefing(modelo: str, d: dict, marca: dict, refs: list[str], correcoes: list[str] | None = None) -> str:
    L: list[str] = []
    tag = " · ".join(x for x in (_limpo(d.get("tag")), _limpo(d.get("tag2"))) if x)
    if tag:
        L.append(f'Etiqueta no topo: "{tag}"')
    if d.get("titulo"):
        L.append(f'Manchete principal: "{_limpo(d["titulo"])}"')
        dest = _destaques(d["titulo"])
        if dest:
            L.append("Palavras em destaque na cor da marca: " + ", ".join(f'"{x}"' for x in dest))
    for k, rot in (("linha_fina", "Linha fina sob a manchete"), ("legenda_jogador", "Linha pequena sob a manchete"),
                   ("texto", "Texto de apoio"), ("quando", "Data e horário"), ("data", "Data")):
        if d.get(k):
            L.append(f'{rot}: "{_limpo(d[k])}"')
    for k in ("palavra", "marca_dagua"):
        if d.get(k):
            L.append(f'Palavra gigante de fundo, só contorno: "{_limpo(d[k]).upper()}"')
    if d.get("casa") and d.get("fora"):
        if d.get("gols_casa") is not None and d.get("gols_fora") is not None:
            L.append(f'Placar: "{_limpo(d["casa"])}" {d["gols_casa"]} x {d["gols_fora"]} "{_limpo(d["fora"])}" '
                     "(mandante à esquerda, visitante à direita, cada um com seu escudo)")
            for lado in ("casa", "fora"):
                if d.get(f"autores_{lado}"):
                    L.append(f'Gols de {_limpo(d[lado])} (letra pequena, do lado dele): ' + " · ".join(_limpo(x) for x in d[f"autores_{lado}"]))
            if d.get("penaltis"):
                L.append(f'Selo: "{_limpo(d["penaltis"])}"')
        else:
            L.append(f'Confronto: "{_limpo(d["casa"])}" x "{_limpo(d["fora"])}"' + (f', horário "{_limpo(d["hora"])}"' if d.get("hora") else ""))
            if d.get("info"):
                L.append(f'Linha de informação: "{_limpo(d["info"])}"')
    for f in (d.get("fatos") or [])[:4]:
        L.append(f'Bloco de dado: número "{_limpo(f.get("n"))}" com legenda "{_limpo(f.get("l"))}"')
    if d.get("jogos"):
        L.append("Lista de jogos (uma linha por jogo):")
        L += [f'  - "{_limpo(j.get("hora"))}"  "{_limpo(j.get("casa"))}" x "{_limpo(j.get("fora"))}"' for j in d["jogos"]]

    tem_foto = any(r.startswith("foto do jogador") for r in refs)
    pessoa = ("Use a pessoa da foto de referência como o jogador: é uma fotografia real, conferida e licenciada. Preserve rosto, "
              "identidade, corpo e uniforme; não invente patrocínio, escudo ou número no uniforme; integre com luz coerente. "
              "É uma composição editorial, não pode parecer registro fotográfico de um lance real."
              if tem_foto else
              "Não desenhe nenhuma pessoa reconhecível (nenhum jogador, técnico ou celebridade). Use estádio, gramado, luz, bola, "
              "texturas, escudos e grafismos.")
    escudos = ("Reproduza os escudos e logos das referências fielmente, sem deformar, redesenhar ou trocar cores."
               if any("escudo" in r or "logo da competição" in r for r in refs) else "Não desenhe escudos de clubes.")
    pre_jogo = modelo in ("resultado", "confronto") and d.get("gols_casa") is None
    partes = [f"Crie a arte completa de um post para o perfil @{marca.get('handle', '')} ({marca.get('nome', '')})."]
    m = manual(marca.get("handle"), modelo, pre_jogo)
    if m:
        partes.append("MANUAL DA MARCA (siga; nesta operação você também escreve os textos da lista, e o logotipo oficial "
                      "é aplicado depois no rodapé):\n" + m)
    partes += [REGRAS.format(bg=marca.get("bg", "#0A0C09"), cor=marca.get("cor", "#95FF50")),
               CENAS.get(modelo, CENAS["noticia"]), pessoa, escudos]
    if refs:
        partes.append("Imagens de referência, na ordem: " + "; ".join(f"imagem {i}: {r}" for i, r in enumerate(refs, 1)) + ".")
    partes.append("TEXTOS DA ARTE (os únicos permitidos):\n" + ("\n".join(L) or "(nenhum texto: arte só visual)"))
    if correcoes:
        partes.append("A versão anterior foi reprovada na revisão. Corrija todos os pontos:\n- " + "\n- ".join(correcoes))
    return "\n\n".join(partes)


def esperados(d: dict) -> list[str]:
    out = [_limpo(d[k]) for k in ("titulo", "linha_fina", "legenda_jogador", "quando", "casa", "fora", "hora", "palavra", "marca_dagua") if d.get(k)]
    if d.get("gols_casa") is not None and d.get("gols_fora") is not None:
        out.append(f"placar {d['gols_casa']} x {d['gols_fora']}")
    for k in ("autores_casa", "autores_fora"):
        out += [_limpo(x) for x in d.get(k) or []]
    for f in d.get("fatos") or []:
        out += [_limpo(f.get("n")), _limpo(f.get("l"))]
    out += [f'{_limpo(j.get("hora"))} {_limpo(j.get("casa"))} x {_limpo(j.get("fora"))}' for j in d.get("jogos") or []]
    return [x for x in out if x]


# ----------------------------------------------------------- referências --
async def referencias(d: dict) -> tuple[list[tuple[bytes, str]], list[str]]:
    pedidos: list[tuple[str, str]] = []
    if d.get("foto"):
        nome = _limpo((d.get("jogador") or {}).get("nome") or d.get("legenda_jogador") or "")
        pedidos.append((d["foto"], f"foto do jogador {nome}".strip()))
    for lado in ("casa", "fora"):
        if d.get(lado) and d.get(f"escudo_{lado}"):
            pedidos.append((d[f"escudo_{lado}"], f"escudo do {_limpo(d[lado])}"))
    if d.get("escudo"):
        pedidos.append((d["escudo"], "escudo do clube da notícia"))
    if d.get("logo_liga"):
        pedidos.append((d["logo_liga"], "logo da competição"))
    for j in (d.get("jogos") or [])[:7]:
        for lado in ("casa", "fora"):
            if j.get(f"escudo_{lado}"):
                pedidos.append((j[f"escudo_{lado}"], f"escudo do {_limpo(j.get(lado))}"))
    imgs, nomes = [], []
    for url, nome in pedidos[:16]:
        try:
            dados, _ = await fotos.baixar(url, 1536 if nome.startswith("foto") else 512)
            im = Image.open(io.BytesIO(dados))
            buf = io.BytesIO()
            im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB").save(buf, "PNG")
        except Exception as e:
            log.info("[ARTE] referência ignorada (%s): %s", nome, e)
            continue
        imgs.append((buf.getvalue(), "image/png"))
        nomes.append(nome)
    return imgs, nomes


# ---------------------------------------------------------------- OpenAI --
async def _chamar(prompt: str, refs: list[tuple[bytes, str]]) -> bytes:
    hoje = date.today()
    if _uso["dia"] != hoje:
        _uso.update(dia=hoje, n=0)
    if LIMITE_DIA and _uso["n"] >= LIMITE_DIA:
        raise ArteErro(f"limite diário de {LIMITE_DIA} imagens da OpenAI atingido (OPENAI_IMAGENS_DIA)")
    if not chave():
        raise ArteErro("OPENAI_API_KEY não configurada no Railway")
    _uso["n"] += 1
    cab = {"Authorization": f"Bearer {chave()}"}
    params = {"model": MODELO, "prompt": prompt, "size": TAMANHO, "quality": QUALIDADE,
              "output_format": "jpeg", "output_compression": "95", "n": "1"}
    try:
        async with _sem:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=20)) as c:
                if refs:
                    campo = "image[]" if len(refs) > 1 else "image"
                    r = await c.post(f"{BASE}/images/edits", headers=cab, data=params,
                                     files=[(campo, (f"ref{i}.png", b, m)) for i, (b, m) in enumerate(refs, 1)])
                else:
                    r = await c.post(f"{BASE}/images/generations", headers=cab, json=params)
    except httpx.HTTPError as e:
        raise ArteErro(f"sem resposta da OpenAI ({type(e).__name__})")
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


# --------------------------------------------------------------- revisão --
PROMPT_CONF = """Você é o revisor final de uma arte de Instagram de futebol antes de publicar.
Textos que DEVEM aparecer, corretos (acentos, grafia e números idênticos; caixa alta vale):
{lista}

Responda SOMENTE com JSON:
{{"ok": true|false, "erros": ["cada problema em até 12 palavras"]}}
ok=false se: texto obrigatório faltando ou errado; número, placar ou nome diferente do pedido; texto legível inventado;
logo ou nome de emissora, portal, casa de aposta ou outro perfil; escudo deformado; pessoa ou rosto deformado;
algo escrito na faixa de 150 px de baixo; arte com cara amadora ou de IA genérica."""


async def revisar(img: bytes, d: dict) -> dict:
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
        return {"ok": False, "erros": [f"revisão automática falhou ({type(e).__name__})"]}
    return {"ok": bool(v.get("ok")), "erros": [str(x) for x in (v.get("erros") or [])][:8]}


# ---------------------------------------------------------------- principal --
async def criar_arte(modelo: str, d: dict, marca: dict) -> tuple[bytes, list[str]]:
    """Devolve (jpeg 1080x1350 sem o rodapé, avisos). Levanta ArteErro se a OpenAI não devolver imagem."""
    refs, nomes = await referencias(d)
    avisos: list[str] = []
    correcoes: list[str] | None = None
    img = None
    try:
        for tentativa in range(2):
            try:
                img = await _chamar(briefing(modelo, d, marca, nomes, correcoes), refs)
            except ArteErro as e:
                msg = str(e).lower()
                if refs and nomes and nomes[0].startswith("foto") and ("moderation" in msg or "safety" in msg):
                    refs, nomes = refs[1:], nomes[1:]
                    avisos.append("OpenAI recusou usar a foto do jogador: arte sem jogador")
                    continue
                if img is not None:   # a 1ª saiu; a refação falhou: fica a 1ª, para revisar
                    break
                raise
            v = await revisar(img, d)
            log.info("[ARTE] %s tentativa %d · %s", modelo, tentativa + 1, v)
            if v["ok"]:
                if tentativa:
                    avisos.append("arte refeita após revisão")
                _uso["ultimo_erro"] = None
                return _ajustar(img), avisos
            correcoes = v["erros"] or ["texto diferente do pedido"]
    except ArteErro as e:
        _uso["ultimo_erro"] = str(e)[:300]
        raise
    if img is None:
        raise ArteErro("OpenAI não devolveu imagem")
    avisos.append(f"{AVISO_REVISAR}: " + "; ".join(correcoes or ["revisão não concluída"]))
    return _ajustar(img), avisos


def _ajustar(img: bytes) -> bytes:
    im = Image.open(io.BytesIO(img)).convert("RGB")
    alvo = 1080 / 1350
    w, h = im.size
    if abs(w / h - alvo) > 0.005:
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
