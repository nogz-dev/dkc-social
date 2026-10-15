"""Visual dos cards feito pela OpenAI (gpt-image-2), seguindo o manual da marca.

Divisão de trabalho (manual do Seu Tipster, 15/10/2026):
- Claude: inteligência editorial, briefing e conferência.
- OpenAI: geração e tratamento VISUAL (cenário, luz, textura, tratamento da
  foto real do jogador). Nenhum texto, número, escudo ou logo sai da OpenAI.
- Renderizador próprio (templates em app/cards/modelos): manchete, placar,
  estatísticas, data, horário, escudos e logotipo oficial, sempre exatos.

Fluxo de cada slide:
1. manual(): carrega brands/<marca>/brand-guidelines.md (a "memória" da
   marca; a API de imagens não guarda nada entre pedidos).
2. briefing(): pedido de arte com o manual, o tipo de post, a composição e as
   áreas que precisam ficar livres para o texto.
3. A OpenAI cria o visual 4:5. Se houver foto conferida e licenciada do
   jogador, ela vai como referência para ser tratada (rosto preservado).
4. conferir(): o Claude reprova se aparecer texto/letra/logo/escudo, rosto
   deformado, pessoa diferente da foto, área de texto ocupada ou cara de IA
   genérica. Reprovou: refaz uma vez com a lista de problemas.
5. O visual aprovado vira a "foto" do template, que escreve todo o resto.

Se falhar, o card sai no template com a foto original (plano B) e aviso.
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

# templates que têm espaço para o visual (os outros são composições só de escudo/tipografia)
COM_VISUAL = ("noticia", "resultado", "jogador", "rodada", "heroi", "destaque")
# pasta do manual por @ do perfil
MANUAIS = {"seutipster.app": "seu-tipster"}

_sem = asyncio.Semaphore(int(os.getenv("OPENAI_PARALELO", "3")))
_uso = {"dia": None, "n": 0}


class ArteErro(Exception):
    pass


def chave() -> str:
    return os.getenv("OPENAI_API_KEY", "").strip()


def ativo() -> bool:
    return bool(chave()) and os.getenv("IMAGENS_OPENAI", "1") != "0"


def usa_visual(modelo: str, handle: str | None) -> bool:
    return ativo() and modelo in COM_VISUAL and (handle or "").lstrip("@").lower() in MANUAIS


def status() -> dict:
    return {"ativo": ativo(), "modelo": MODELO, "hoje": _uso["n"] if _uso["dia"] == date.today() else 0,
            "limite_dia": LIMITE_DIA, "perfis": list(MANUAIS)}


def _limpo(t) -> str:
    return re.sub(r"\s+", " ", str(t or "").replace("*", "")).strip()


# ----------------------------------------------------------------- manual --
@lru_cache(maxsize=8)
def _secoes(pasta: str) -> dict[str, str]:
    arq = RAIZ / "brands" / pasta / "brand-guidelines.md"
    if not arq.exists():
        return {}
    txt = re.sub(r"<!--.*?-->", "", arq.read_text(encoding="utf-8"), flags=re.S)
    txt = re.sub(r"^# .*$", "", txt, flags=re.M)  # título do documento
    partes = re.split(r"^## ", txt, flags=re.M)
    return {p.split("\n", 1)[0].strip().upper(): p.split("\n", 1)[1].strip() if "\n" in p else "" for p in partes[1:]}


TIPO_POR_MODELO = {"rodada": "A. AGENDA", "noticia": "C. NOTÍCIAS", "destaque": "C. NOTÍCIAS",
                   "jogador": "C. NOTÍCIAS", "heroi": "D. RESULTADOS", "resultado": "D. RESULTADOS"}


def manual(handle: str | None, modelo: str, pre_jogo: bool = False) -> str:
    """Trechos do manual que valem para a arte: cores, identidade, direção de arte e o tipo de post."""
    pasta = MANUAIS.get((handle or "").lstrip("@").lower())
    s = _secoes(pasta) if pasta else {}
    if not s:
        return ""
    pega = lambda prefixo: next((v for k, v in s.items() if k.startswith(prefixo)), "")
    tipo_txt = ""
    tipos = pega("5. TIPOS")
    alvo = "B. PRÉ-JOGO" if pre_jogo else TIPO_POR_MODELO.get(modelo, "C. NOTÍCIAS")
    m = re.search(r"^### " + re.escape(alvo) + r".*?(?=^### |\Z)", tipos, flags=re.M | re.S | re.I)
    if m:
        tipo_txt = m.group(0)
    partes = [pega("CORES OFICIAIS"), pega("1. IDENTIDADE"), pega("2. DIREÇÃO DE ARTE"), tipo_txt]
    return "\n\n".join(p for p in partes if p)


# --------------------------------------------------------------- briefing --
ZONAS = {
    "noticia": "Sujeito principal na metade de cima. A metade de baixo (de 50% da altura até o fim) precisa ser escura, calma e sem detalhes: é onde entram manchete e dados.",
    "destaque": "Jogador à direita, do peito para cima, ocupando a metade de cima. Metade de baixo escura e limpa para manchete e dados.",
    "jogador": "Jogador em destaque na metade de cima, rosto nítido. Terço de baixo escuro e limpo para os dados.",
    "resultado": "Clima de fim de jogo na metade de cima. A partir de 45% da altura, área escura e limpa: ali entram manchete, escudos e placar.",
    "heroi": "Jogador à direita, grande, do quadril para cima. Metade esquerda e o terço de baixo escuros e limpos para manchete e placar.",
    "rodada": "Atmosfera de estádio discreta e escura. Área central larga (de 20% a 90% da altura) bem escura, uniforme e sem detalhes: ali entra a lista de jogos.",
}
CANTO = "Canto superior esquerdo (300 x 140 px) e a faixa de baixo (150 px) sempre livres e escuros: etiqueta e rodapé entram ali."


def briefing(modelo: str, d: dict, marca: dict, com_foto: bool, correcoes: list[str] | None = None) -> str:
    pre_jogo = modelo == "resultado" and d.get("gols_casa") is None
    clube = _limpo(d.get("fora") if d.get("lado_destaque") == "fora" else d.get("casa")) or _limpo((d.get("jogador") or {}).get("clube"))
    assunto = _limpo(d.get("titulo")) or _limpo(d.get("tag"))
    contexto = []
    if assunto:
        contexto.append(f"Assunto do post (só para inspirar o clima, NÃO escreva): {assunto}")
    if d.get("casa") and d.get("fora"):
        contexto.append(f"Partida: {_limpo(d['casa'])} x {_limpo(d['fora'])}" + (" (antes do jogo)" if pre_jogo else ""))
    if clube:
        contexto.append(f"Clube protagonista: {clube}. Use as cores desse clube na luz e no cenário, sem desenhar o escudo.")
    if d.get("cor_faixa"):
        contexto.append(f"Cor do clube protagonista: {d['cor_faixa']}")

    pessoa = (
        "Use a pessoa da imagem de referência como o jogador do card. É uma fotografia real, conferida e licenciada: "
        "preserve rosto, identidade, corpo e uniforme exatamente como estão. Faça um recorte profissional e integre o jogador "
        "ao cenário com luz e cor coerentes. Não invente patrocínio, escudo ou número no uniforme. Não altere o rosto. "
        "É uma composição editorial, não pode parecer o registro fotográfico de um lance real."
        if com_foto else
        "Não desenhe nenhuma pessoa (nenhum jogador, técnico ou torcedor com rosto). Use estádio, gramado, luz de refletores, "
        "bola, fumaça, texturas e grafismos discretos."
    )
    partes = [
        f"Crie o VISUAL de fundo de um post de Instagram (4:5) do perfil @{marca.get('handle', '')}. "
        "Tipografia, placar, escudos e logotipo serão colocados depois por um renderizador, então a imagem NÃO pode ter "
        "nenhuma letra, número, palavra, placa, logo, escudo, marca d'água ou interface.",
    ]
    m = manual(marca.get("handle"), modelo, pre_jogo)
    if m:
        partes.append("MANUAL DA MARCA (siga; as partes sobre texto e logotipo são feitas pelo renderizador, não por você):\n" + m)
    partes += [
        pessoa,
        "COMPOSIÇÃO: " + ZONAS.get(modelo, ZONAS["noticia"]) + " " + CANTO,
        "Acabamento: campanha esportiva premium, cinematográfica, iluminação profissional, contraste alto, profundidade, "
        "textura sofisticada. Nada de aparência genérica de IA: sem brilho plástico, pele de cera, objetos derretidos ou "
        "excesso de efeitos. Não imite nenhum canal ou emissora.",
    ]
    if contexto:
        partes.append("\n".join(contexto))
    if correcoes:
        partes.append("A versão anterior foi reprovada pela revisão. Corrija:\n- " + "\n- ".join(correcoes))
    return "\n\n".join(partes)


# ---------------------------------------------------------------- OpenAI --
async def _chamar(prompt: str, ref: bytes | None) -> bytes:
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
            if ref:
                r = await c.post(f"{BASE}/images/edits", headers=cab, data=params,
                                 files=[("image", ("jogador.png", ref, "image/png"))])
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
PROMPT_CONF = """Você é o diretor de arte que aprova o VISUAL de fundo de um post de Instagram de futebol. Texto, placar, escudos e logo serão colocados depois; esta imagem não pode ter nenhum deles.
{ref}
Área que precisa estar livre (escura, calma, sem detalhes): {zona} {canto}

Responda SOMENTE com JSON:
{{"estado": "aprovada" | "necessita_correcao" | "rejeitada",
 "problemas": ["cada problema em até 12 palavras"]}}
Reprove (necessita_correcao) se houver: qualquer letra, número, palavra, logo, escudo, marca d'água, placa legível; área de texto ocupada; rosto ou corpo deformado, mãos estranhas; {pessoa}; aparência genérica de IA ou amadora; elementos de outro canal/emissora.
Use "rejeitada" só se a imagem for inaproveitável ou ofensiva."""


async def conferir(img: bytes, modelo: str, ref: bytes | None) -> dict:
    k = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not k:
        return {"estado": "aprovada", "problemas": [], "sem_revisor": True}

    def b64(dados: bytes, lado: int) -> str:
        im = Image.open(io.BytesIO(dados)).convert("RGB")
        im.thumbnail((lado, lado))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)
        return base64.b64encode(buf.getvalue()).decode()

    conteudo = []
    if ref:
        conteudo += [{"type": "text", "text": "IMAGEM 1: foto real do jogador (referência)."},
                     {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64(ref, 512)}},
                     {"type": "text", "text": "IMAGEM 2: visual a aprovar."}]
    conteudo += [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64(img, 1100)}},
                 {"type": "text", "text": PROMPT_CONF.format(
                     ref="Compare o jogador da IMAGEM 2 com a IMAGEM 1." if ref else "Não deve haver pessoa com rosto.",
                     zona=ZONAS.get(modelo, ZONAS["noticia"]), canto=CANTO,
                     pessoa="pessoa diferente da referência ou rosto alterado" if ref else "pessoa com rosto visível")}]
    try:
        async with httpx.AsyncClient(timeout=90) as c:
            r = await c.post(os.getenv("ANTHROPIC_BASE", "https://api.anthropic.com") + "/v1/messages",
                             headers={"x-api-key": k, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                             json={"model": MODELO_CONF, "max_tokens": 500, "messages": [{"role": "user", "content": conteudo}]})
        txt = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        m = re.search(r"\{.*\}", txt, re.S)
        v = json.loads(m.group(0)) if m else {}
    except Exception as e:
        return {"estado": "necessita_correcao", "problemas": [f"revisão falhou: {type(e).__name__}"]}
    estado = v.get("estado") if v.get("estado") in ("aprovada", "necessita_correcao", "rejeitada") else "necessita_correcao"
    return {"estado": estado, "problemas": [str(x) for x in (v.get("problemas") or [])][:8]}


# ---------------------------------------------------------------- principal --
async def criar_visual(modelo: str, d: dict, marca: dict) -> tuple[bytes, list[str]]:
    """Devolve (jpeg 1080x1350 sem texto, avisos). Levanta ArteErro se não sair visual aprovado."""
    ref = None
    if d.get("foto"):
        try:
            dados, _ = await fotos.baixar(d["foto"], 1536)
            buf = io.BytesIO()
            Image.open(io.BytesIO(dados)).convert("RGB").save(buf, "PNG")
            ref = buf.getvalue()
        except Exception as e:
            log.info("[ARTE] foto de referência indisponível: %s", e)
    avisos: list[str] = []
    correcoes = None
    ultimo = ""
    for tentativa in range(2):
        try:
            img = await _chamar(briefing(modelo, d, marca, bool(ref), correcoes), ref)
        except ArteErro as e:
            msg = str(e).lower()
            if ref and ("moderation" in msg or "safety" in msg):  # recusou a foto de pessoa real
                ref = None
                avisos.append("OpenAI recusou tratar a foto do jogador: visual sem jogador")
                continue
            raise
        v = await conferir(img, modelo, ref)
        log.info("[ARTE] %s tentativa %d · %s", modelo, tentativa + 1, v)
        if v["estado"] == "aprovada":
            if tentativa:
                avisos.append("visual refeito após revisão")
            if ref is None and d.get("foto"):
                avisos.append("visual sem a foto do jogador")
            return _ajustar(img), avisos
        if v["estado"] == "rejeitada":
            raise ArteErro("visual rejeitado na revisão: " + "; ".join(v["problemas"]))
        correcoes = v["problemas"] or ["não seguiu a composição pedida"]
        ultimo = "; ".join(correcoes)
    raise ArteErro(f"visual reprovado na revisão: {ultimo}")


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
