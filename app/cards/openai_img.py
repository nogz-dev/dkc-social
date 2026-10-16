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
TENTATIVAS = int(os.getenv("OPENAI_TENTATIVAS", "3"))   # 1ª + refações quando a revisão acha erro

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
                   "confronto": "B. PRÉ-JOGO", "tabela": "E. ESTATÍSTICAS"}


def manual(handle: str | None, modelo: str, pre_jogo: bool = False, resultados_do_dia: bool = False) -> str:
    pasta = MANUAIS.get((handle or "").lstrip("@").lower())
    s = _secoes(pasta) if pasta else {}
    if not s:
        return ""
    pega = lambda prefixo: next((v for k, v in s.items() if k.startswith(prefixo)), "")
    alvo = "B. PRÉ-JOGO" if pre_jogo else TIPO_POR_MODELO.get(modelo, "C. NOTÍCIAS")
    if modelo == "rodada" and resultados_do_dia:
        alvo = "D. RESULTADOS"
    m = re.search(r"^### " + re.escape(alvo) + r".*?(?=^### |\Z)", pega("5. TIPOS"), flags=re.M | re.S | re.I)
    partes = [pega("CORES OFICIAIS"), pega("1. IDENTIDADE"), pega("2. DIREÇÃO DE ARTE"), m.group(0) if m else ""]
    return "\n\n".join(p for p in partes if p)


# --------------------------------------------------------------- briefing --
REGRAS = """Regras fixas desta arte:
- Card vertical de Instagram 4:5, campanha esportiva premium, cinematográfica, com identidade própria (não imite nenhum canal ou emissora). Acabamento de designer humano, nada com cara de IA genérica (sem brilho plástico, pele de cera, objetos derretidos ou deformados).
- Tipografia moderna e impactante, sans serif condensada pesada, hierarquia clara, textos grandes e legíveis.
- Cores: base {bg}, destaque da marca {cor} e as cores do clube protagonista quando houver.
- PROIBIDO: logo, marca d'água ou nome de emissora, portal ou outro perfil; logo de casa de aposta; selo promocional; QR code; @ de perfis; texto que não esteja na lista; número inventado.
- Escreva SOMENTE os textos da lista, exatamente como estão (acentos, nomes e números idênticos).

GRADE, ALINHAMENTO E RESPIRO (obrigatório; a arte será reprovada se não cumprir). Tela de 1088 x 1360 px:
- Margem de segurança: 88 px à esquerda e à direita, 96 px no topo. Nenhum texto, escudo, caixa ou etiqueta encosta nem passa dessas margens. Só o fundo/foto pode ir até a borda.
- Faixa de baixo de 200 px totalmente livre e escura (sem texto, escudo, caixa ou elemento): o logotipo oficial entra ali depois. Não desenhe o logotipo da marca em lugar nenhum.
- Tudo alinhado a UM eixo: ou centralizado no eixo vertical do meio, ou alinhado à margem esquerda. Não misture.
- Nada torto: nenhum texto, caixa, etiqueta, moldura ou escudo inclinado, girado, em perspectiva ou com cantos chanfrados/assimétricos. Linhas horizontais retas, caixas retangulares com cantos iguais.
- Espaçamento consistente: 40 a 56 px entre blocos; 24 px entre itens do mesmo bloco. Nenhum elemento encostado em outro. Texto nunca sobre escudo, rosto ou outro texto.
- Elementos repetidos (escudos, caixas de dados, linhas de tabela) com o mesmo tamanho, mesma altura, mesma linha de base e distribuídos por igual.
- Manchete em no máximo 3 linhas, com entrelinha confortável e largura máxima de 900 px. Menos elementos e mais respiro: na dúvida, tire grafismo.
- Mínimo de 30% da área com respiro (fundo limpo, sem texto nem grafismo)."""

CENAS = {  # função de cada formato (o conceito de cada peça vem da etapa de conceito)
    "heroi": "Resultado com protagonista: o jogador decisivo + placar.",
    "destaque": "Notícia com jogador protagonista.",
    "resultado": "Resultado de partida: placar confirmado em evidência, escudos, destaque para o vencedor.",
    "confronto": "Pré-jogo: os dois clubes frente a frente, data e horário claros.",
    "jogador": "Perfil de jogador com dados organizados.",
    "clube": "Notícia de clube: o clube é o protagonista.",
    "editorial": "Notícia em que a manchete é a protagonista.",
    "rodada": "Agenda do dia de um campeonato: jogos em ordem de horário (Brasília), leitura rápida, escudos ao lado dos nomes.",
    "resultados": "Resultados do dia de um campeonato: placares em evidência, um jogo por linha, escudos ao lado dos nomes.",
    "tabela": "Tabela de classificação: tabela editorial alinhada, uma linha por time, colunas bem separadas, escudos pequenos; destaque sutil para o líder.",
    "noticia": "Notícia com manchete forte e imagem coerente com o tema.",
}


@lru_cache(maxsize=1)
def diretor() -> str:
    """Briefing permanente de direção de arte (brands/diretor-de-arte.md), sem o formulário do fim."""
    arq = RAIZ / "brands" / "diretor-de-arte.md"
    if not arq.exists():
        return ""
    txt = re.sub(r"<!--.*?-->", "", arq.read_text(encoding="utf-8"), flags=re.S)
    return txt.split("## BRIEFING DE CADA PEÇA")[0].strip()


def textos(d: dict) -> list[str]:
    L: list[str] = []
    tag = " · ".join(x for x in (_limpo(d.get("tag")), _limpo(d.get("tag2"))) if x)
    if tag:
        L.append(f'Etiqueta: "{tag}"')
    if d.get("titulo"):
        L.append(f'Título: "{_limpo(d["titulo"])}"')
        dest = _destaques(d["titulo"])
        if dest:
            L.append("Palavras em destaque na cor da marca: " + ", ".join(f'"{x}"' for x in dest))
    for k, rot in (("linha_fina", "Linha fina"), ("legenda_jogador", "Linha complementar"),
                   ("texto", "Texto de apoio"), ("quando", "Data e horário"), ("data", "Data")):
        if d.get(k):
            L.append(f'{rot}: "{_limpo(d[k])}"')
    for k in ("palavra", "marca_dagua"):
        if d.get(k):
            L.append(f'Palavra de apoio (opcional, só se servir ao conceito): "{_limpo(d[k]).upper()}"')
    if d.get("casa") and d.get("fora"):
        if d.get("gols_casa") is not None and d.get("gols_fora") is not None:
            L.append(f'Placar: "{_limpo(d["casa"])}" {d["gols_casa"]} x {d["gols_fora"]} "{_limpo(d["fora"])}" '
                     "(mandante à esquerda, visitante à direita, cada um com seu escudo)")
            for lado in ("casa", "fora"):
                if d.get(f"autores_{lado}"):
                    L.append(f'Gols de {_limpo(d[lado])}: ' + " · ".join(_limpo(x) for x in d[f"autores_{lado}"]))
            if d.get("penaltis"):
                L.append(f'Pênaltis: "{_limpo(d["penaltis"])}"')
        else:
            L.append(f'Confronto: "{_limpo(d["casa"])}" x "{_limpo(d["fora"])}"' + (f', horário "{_limpo(d["hora"])}"' if d.get("hora") else ""))
            if d.get("info"):
                L.append(f'Informação de serviço: "{_limpo(d["info"])}"')
    for f in (d.get("fatos") or [])[:4]:
        L.append(f'Dado: "{_limpo(f.get("n"))}" — "{_limpo(f.get("l"))}"')
    if d.get("jogos"):
        L.append("Jogos (uma linha por jogo, nesta ordem):")
        for j in d["jogos"]:
            if j.get("gols_casa") is not None:
                pen = f' ({_limpo(j["penaltis"])})' if j.get("penaltis") else ""
                L.append(f'  - "{_limpo(j.get("casa"))}" {j["gols_casa"]} x {j["gols_fora"]} "{_limpo(j.get("fora"))}"{pen}')
            else:
                L.append(f'  - "{_limpo(j.get("hora"))}"  "{_limpo(j.get("casa"))}" x "{_limpo(j.get("fora"))}"')
    if d.get("linhas"):
        L.append('Tabela (cabeçalho: "#", "Time", "P", "J", "V", "SG"), uma linha por time, nesta ordem:')
        L += [f'  - "{r["pos"]}" "{_limpo(r["time"])}" P "{r["pts"]}" J "{r["j"]}" V "{r["v"]}" SG "{r["sg"]}"' for r in d["linhas"]]
    if d.get("parte"):
        L.append(f'Página: "{d["parte"]}"')
    return L


def _tipo(modelo: str, d: dict) -> str:
    if modelo == "rodada" and any(j.get("gols_casa") is not None for j in d.get("jogos") or []):
        return "resultados"
    return modelo


def briefing(modelo: str, d: dict, marca: dict, refs: list[str], correcoes: list[str] | None = None,
             conceito: dict | None = None, pagina: tuple[int, int] = (1, 1)) -> str:
    tipo = _tipo(modelo, d)
    tem_foto = any(r.startswith("foto do jogador") for r in refs)
    arquivos = ["logotipo oficial (aplicado depois por nós no rodapé: NÃO desenhe nenhuma versão do logotipo)"]
    arquivos += refs
    if tem_foto:
        imagem = ("A foto do jogador é real, conferida e licenciada: preserve rosto, identidade, corpo e uniforme; recorte limpo "
                  "(cabelo, mãos, uniforme); não invente patrocínio, escudo ou número; integre com luz coerente; é composição "
                  "editorial, não pode parecer registro fotográfico de um lance real.")
    else:
        imagem = ("Não há foto de atleta: não desenhe nenhuma pessoa reconhecível nem jogador fictício. Resolva com tipografia, "
                  "cor, escudos e grafismo; estádio, grama, fumaça ou luz só se servirem ao conceito.")
    if any(r.startswith("foto do estádio") for r in refs):
        imagem += " A foto do estádio é só referência de ambiente (pode estilizar ou nem usar)."
    escudos = ("Escudos e logos das referências: reproduza fielmente, sem deformar, redesenhar ou trocar cores."
               if any("escudo" in r or "logo da competição" in r for r in refs) else "Não desenhe escudos de clubes.")
    k, n = pagina
    formato = "Feed vertical 1080 x 1350 (4:5)" + (f", página {k} de {n} de um carrossel"
                                                     + (" (CAPA: promessa clara)" if k == 1 else " (página interna: continuidade visual com a capa)")
                                                     if n > 1 else "")
    pre_jogo = modelo in ("resultado", "confronto") and d.get("gols_casa") is None

    partes = []
    dr = diretor()
    if dr:
        partes.append(dr)
    m = manual(marca.get("handle"), modelo, pre_jogo, tipo == "resultados")
    if m:
        partes.append("MANUAL DA MARCA (nesta operação você escreve os textos; o logotipo oficial é aplicado depois):\n" + m)
    partes.append(REGRAS.format(bg=marca.get("bg", "#0A0C09"), cor=marca.get("cor", "#95FF50")))
    if conceito:
        partes.append("CONCEITO DESTA PEÇA (definido pela direção editorial; execute):\n" +
                      "\n".join(f"- {rot}: {conceito[c]}" for c, rot in (
                          ("objetivo", "Objetivo"), ("mensagem", "Mensagem que o público entende primeiro"), ("emocao", "Emoção"),
                          ("ideia_visual", "Ideia visual"), ("protagonista", "Protagonista"), ("composicao", "Composição"),
                          ("evitar", "Evitar nesta peça")) if conceito.get(c)))
    briefing_peca = [
        f"Marca: {marca.get('nome', '')} (@{marca.get('handle', '')})",
        f"Objetivo da publicação: {CENAS.get(tipo, CENAS['noticia'])}",
        "Público: torcedores brasileiros de futebol que acompanham o perfil no celular",
        f"Tema: {_limpo(d.get('titulo')) or _limpo(d.get('tag'))}",
        "Texto obrigatório (o ÚNICO texto permitido, exatamente assim):\n" + ("\n".join(textos(d)) or "(nenhum)"),
        f"Formato e dimensões: {formato}. Margens e faixa livre conforme a grade acima.",
        "Arquivos oficiais disponíveis (imagens de referência, na ordem): " + "; ".join(
            f"{i}) {r}" if i else r for i, r in enumerate(arquivos)),
        f"Imagens: {imagem} {escudos}",
        "Chamada para ação: nenhuma na arte (fica na legenda)",
        "Restrições: nada de emissora/portal/outro perfil, casa de aposta, selos, @, QR code, número ou texto inventado.",
    ]
    partes.append("BRIEFING DESTA PEÇA:\n" + "\n".join(briefing_peca))
    if correcoes:
        partes.append("A versão anterior foi reprovada na revisão. Corrija todos os pontos:\n- " + "\n- ".join(correcoes))
    partes.append("Entregue a arte final. Critério final: cada elemento tem uma função; se remover melhora a clareza, remova.")
    return "\n\n".join(partes)


# --------------------------------------------------------------- conceito --
PROMPT_CONCEITO = """Você é a direção editorial de arte do perfil @{handle} (futebol, Instagram). Antes da criação visual, defina o CONCEITO desta publicação.
Tipo: {tipo}. Páginas: {paginas}.
Conteúdo (dados confirmados):
{conteudo}

Conceitos usados nas últimas publicações (NÃO repita o mesmo enquadramento, distribuição ou recurso):
{recentes}

Regras: o tema define as decisões visuais (rivalidade pede tensão/oposição; conquista pede escala/celebração; análise pede clareza
editorial; curiosidade pede humor/surpresa). Um protagonista só. Nada de efeitos gratuitos (fumaça, fogo, raios, neon, partículas,
estádio de fundo) se não servirem à ideia. Sem foto de atleta disponível = {foto}.
Responda SOMENTE com JSON, frases curtas e concretas, em português:
{{"objetivo": "...", "mensagem": "...", "emocao": "...", "ideia_visual": "a ideia específica desta peça",
 "protagonista": "...", "composicao": "como os elementos se organizam (posições, escala, eixo)", "evitar": "...",
 "resumo": "até 8 palavras para lembrar este conceito"}}"""


async def conceito(slides: list[dict], marca: dict, recentes: list[str]) -> dict | None:
    """Etapa 'comece pelo conceito' (Claude): uma ideia visual por publicação, igual em todas as páginas."""
    k = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not k or not slides:
        return None
    d0 = slides[0].get("dados") or {}
    conteudo = []
    for i, sl in enumerate(slides, 1):
        conteudo.append(f"Página {i} ({sl.get('modelo')}):\n" + "\n".join(textos(sl.get("dados") or {})))
    tem_foto = bool(d0.get("foto")) and (d0.get("jogador") or d0.get("foto_tipo") == "jogador"
                                         or slides[0].get("modelo") in ("heroi", "destaque", "jogador"))
    prompt = PROMPT_CONCEITO.format(
        handle=marca.get("handle", ""), tipo=_tipo(slides[0].get("modelo", ""), d0), paginas=len(slides),
        conteudo="\n\n".join(conteudo)[:6000], recentes="\n".join(f"- {r}" for r in recentes[:8]) or "- (nenhum)",
        foto="há foto real do jogador para usar" if tem_foto else "resolver sem pessoa (tipografia, cor, escudos, grafismo)")
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(os.getenv("ANTHROPIC_BASE", "https://api.anthropic.com") + "/v1/messages",
                             headers={"x-api-key": k, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                             json={"model": MODELO_CONF, "max_tokens": 700, "messages": [{"role": "user", "content": prompt}]})
        txt = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        m = re.search(r"\{.*\}", txt, re.S)
        v = json.loads(m.group(0)) if m else None
    except Exception as e:
        log.info("[ARTE] conceito falhou: %s", e)
        return None
    return v if isinstance(v, dict) else None


def esperados(d: dict) -> list[str]:
    out = [_limpo(d[k]) for k in ("titulo", "linha_fina", "legenda_jogador", "quando", "casa", "fora", "hora") if d.get(k)]
    if d.get("gols_casa") is not None and d.get("gols_fora") is not None:
        out.append(f"placar {d['gols_casa']} x {d['gols_fora']}")
    for k in ("autores_casa", "autores_fora"):
        out += [_limpo(x) for x in d.get(k) or []]
    for f in d.get("fatos") or []:
        out += [_limpo(f.get("n")), _limpo(f.get("l"))]
    for j in d.get("jogos") or []:
        if j.get("gols_casa") is not None:
            out.append(f'{_limpo(j.get("casa"))} {j["gols_casa"]} x {j["gols_fora"]} {_limpo(j.get("fora"))}')
        else:
            out.append(f'{_limpo(j.get("hora"))} {_limpo(j.get("casa"))} x {_limpo(j.get("fora"))}')
    for r in d.get("linhas") or []:
        out.append(f'{r["pos"]} {_limpo(r["time"])} P {r["pts"]} J {r["j"]} V {r["v"]} SG {r["sg"]}')
    return [x for x in out if x]


# ----------------------------------------------------------- referências --
async def referencias(d: dict, modelo: str = "") -> tuple[list[tuple[bytes, str]], list[str]]:
    pedidos: list[tuple[str, str]] = []
    if d.get("foto"):
        if d.get("jogador") or d.get("foto_tipo") == "jogador" or modelo in ("heroi", "destaque", "jogador"):
            nome = _limpo((d.get("jogador") or {}).get("nome") or d.get("legenda_jogador") or "")
            pedidos.append((d["foto"], f"foto do jogador {nome}".strip()))
        else:
            pedidos.append((d["foto"], "foto do estádio (referência de ambiente)"))
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
    for r in (d.get("linhas") or [])[:15]:
        if r.get("escudo"):
            pedidos.append((r["escudo"], f"escudo do {_limpo(r.get('time'))}"))
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
PROMPT_CONF = """Você é o diretor de arte e revisor final de uma arte de Instagram de futebol (1080 x 1350) antes de publicar.
Textos que DEVEM aparecer, corretos (acentos, grafia e números idênticos; caixa alta vale):
{lista}

Responda SOMENTE com JSON:
{{"ok": true|false, "erros": ["cada problema em até 14 palavras, dizendo onde está e como corrigir"]}}
ok=false se houver QUALQUER um destes:
TEXTO: texto obrigatório faltando ou errado; número, placar ou nome diferente do pedido; texto legível inventado.
MARCA: logo ou nome de emissora, portal, casa de aposta ou outro perfil; escudo deformado; logotipo da marca desenhado.
LAYOUT: elemento cortado ou encostado na borda (margem lateral menor que ~80 px ou topo menor que ~90 px); algo nos 190 px de baixo;
elemento torto, inclinado, em perspectiva ou com cantos assimétricos; elementos desalinhados (fora de um eixo comum);
elementos encostados uns nos outros ou sem respiro; caixas/escudos repetidos com tamanhos ou alturas diferentes;
texto sobre escudo, rosto ou outro texto; manchete com mais de 3 linhas; arte poluída.
PESSOA/QUALIDADE: rosto ou corpo deformado; recorte sujo (cabelo, mãos, uniforme); cara de amador ou de IA genérica.
DIREÇÃO DE ARTE (olhe também como miniatura de celular): a mensagem principal não aparece primeiro; não há protagonista claro
ou tudo chama atenção igual; elemento competindo sem necessidade ou sem função; texto secundário ilegível no celular;
letras deformadas, esticadas ou com contorno/sombra para compensar; efeitos gratuitos (fumaça, fogo, raios, neon) sem relação com o tema."""


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
async def criar_arte(modelo: str, d: dict, marca: dict, conceito: dict | None = None,
                     pagina: tuple[int, int] = (1, 1)) -> tuple[bytes, list[str]]:
    """Devolve (jpeg 1080x1350 sem o rodapé, avisos). Levanta ArteErro se a OpenAI não devolver imagem."""
    refs, nomes = await referencias(d, modelo)
    avisos: list[str] = []
    correcoes: list[str] | None = None
    img = None
    try:
        for tentativa in range(TENTATIVAS):
            try:
                img = await _chamar(briefing(modelo, d, marca, nomes, correcoes, conceito, pagina), refs)
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
