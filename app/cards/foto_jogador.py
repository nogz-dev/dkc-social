"""Foto de jogador verificada.

Regra da casa (Nog, 14/10/2026): NUNCA usar imagem de matéria/arte de outro
veículo. A foto do card é sempre uma fotografia limpa do jogador certo.

Processo:
1. A API-Football confirma quem é o jogador e dá a foto de referência oficial
   (rosto pequeno, só para conferência — não vai para o card).
2. Busca fotos candidatas pelo nome (Wikimedia Commons e, se configurado,
   busca de imagens na web).
3. Uma IA com visão confere cada candidata contra a referência:
   - é a MESMA pessoa?
   - é uma fotografia limpa (sem texto, logo de emissora, montagem, moldura,
     marca d'água ou arte de outro canal)?
   - serve de capa (pessoa em destaque, nítida)?
4. Só a primeira que passa nas três vai para o card. Se nenhuma passar, o card
   fica sem foto de jogador (e resultado não sai sozinho).
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import unicodedata
from typing import Any

import httpx
from PIL import Image

from . import fotos

log = logging.getLogger("dkc.foto")

API_FOOTBALL = os.getenv("API_FOOTBALL_BASE", "https://v3.football.api-sports.io")
MEDIA = os.getenv("API_FOOTBALL_MEDIA", "https://media.api-sports.io")
MODELO_VISAO = os.getenv("FOTO_MODELO_VISAO", "claude-sonnet-5-5")
UA_WIKI = "DKCSocial/1.0 (contato@dkc.social)"


def _sem_acento(s: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFD", s or "") if unicodedata.category(ch) != "Mn").lower()


# ----------------------------------------------------------- referência --
async def referencia_api(nome: str | None = None, jogador_id: int | None = None) -> dict | None:
    """Jogador confirmado na API-Football: {id, nome, nome_completo, foto_ref}."""
    k = (os.getenv("API_FOOTBALL_KEY") or os.getenv("APIFOOTBALL_KEY") or "").strip()
    if not k:
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            if jogador_id:
                r = await c.get(f"{API_FOOTBALL}/players/profiles", params={"player": jogador_id}, headers={"x-apisports-key": k})
            else:
                termo = (nome or "").split()[-1] if nome else ""
                if len(termo) < 3:
                    return None
                r = await c.get(f"{API_FOOTBALL}/players/profiles", params={"search": _sem_acento(termo)}, headers={"x-apisports-key": k})
        lista = [x.get("player") or {} for x in (r.json().get("response") or [])]
    except Exception:
        return None
    if not lista:
        return None
    if not jogador_id:  # escolhe o que mais bate com o nome inteiro
        alvo = set(_sem_acento(nome).split())
        def nota(p):
            nomes = set(_sem_acento(" ".join(x for x in (p.get("name"), p.get("firstname"), p.get("lastname")) if x)).split())
            return len(alvo & nomes)
        lista.sort(key=nota, reverse=True)
        if nota(lista[0]) == 0:
            return None
    p = lista[0]
    completo = " ".join(x for x in (p.get("firstname"), p.get("lastname")) if x).strip()
    return {"id": p.get("id"), "nome": p.get("name") or completo, "nome_completo": completo or p.get("name"),
            "foto_ref": p.get("photo") or (f"{MEDIA}/football/players/{p.get('id')}.png" if p.get("id") else None)}


# ----------------------------------------------------------- candidatas --
async def candidatas_wikimedia(nome: str, n: int = 6) -> list[dict]:
    if not nome:
        return []
    sobrenome = _sem_acento(nome.split()[-1])
    params = {"action": "query", "format": "json", "generator": "search", "gsrnamespace": "6",
              "gsrsearch": f"filetype:bitmap {nome}", "gsrlimit": "12",
              "prop": "imageinfo", "iiprop": "url|extmetadata|size|mime", "iiurlwidth": "1600"}
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": UA_WIKI}) as c:
            r = await c.get("https://commons.wikimedia.org/w/api.php", params=params)
        pages = sorted(((r.json().get("query") or {}).get("pages") or {}).values(), key=lambda p: p.get("index", 99))
    except Exception:
        return []
    out = []
    for p in pages:
        info = (p.get("imageinfo") or [{}])[0]
        if sobrenome not in _sem_acento(p.get("title", "")) or info.get("mime") not in ("image/jpeg", "image/png"):
            continue
        if (info.get("width") or 0) < 700:
            continue
        meta = info.get("extmetadata") or {}
        autor = re.sub(r"<[^>]+>", "", (meta.get("Artist") or {}).get("value", ""))[:60].strip()
        lic = re.sub(r"<[^>]+>", "", (meta.get("LicenseShortName") or {}).get("value", ""))[:30].strip()
        credito = "Foto: " + " · ".join(x for x in (autor, f"Wikimedia Commons ({lic})" if lic else "Wikimedia Commons") if x)
        out.append({"url": info.get("thumburl") or info.get("url"), "credito": credito, "origem": "wikimedia"})
        if len(out) >= n:
            break
    return out


async def candidatas_web(nome: str, clube: str | None, n: int = 6) -> list[dict]:
    """Busca de imagens na web (Brave Search API). Só roda se BRAVE_API_KEY existir."""
    k = os.getenv("BRAVE_API_KEY", "").strip()
    if not k or not nome:
        return []
    q = f"{nome} {clube or ''} jogador futebol foto".strip()
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get("https://api.search.brave.com/res/v1/images/search",
                            params={"q": q, "count": 20, "safesearch": "strict"},
                            headers={"X-Subscription-Token": k, "Accept": "application/json"})
        res = r.json().get("results") or []
    except Exception:
        return []
    out = []
    for it in res:
        props = it.get("properties") or {}
        url = props.get("url") or (it.get("thumbnail") or {}).get("src")
        larg = int(props.get("width") or 0)
        if not url or (larg and larg < 700):
            continue
        site = (it.get("source") or it.get("meta_url", {}).get("hostname") or "").replace("www.", "")
        out.append({"url": url, "credito": f"Foto: reprodução {site}".strip(), "origem": "web"})
        if len(out) >= n:
            break
    return out


# -------------------------------------------------------------- visão ----
def _b64_jpeg(dados: bytes, lado: int = 768) -> str:
    im = Image.open(io.BytesIO(dados)).convert("RGB")
    im.thumbnail((lado, lado))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=85)
    return base64.b64encode(out.getvalue()).decode()


PROMPT_VISAO = """Você confere fotos para um perfil esportivo no Instagram. A foto escolhida vai ocupar o card inteiro.
{ref_txt}
Jogador procurado: {nome}{clube_txt}.

Responda SOMENTE com JSON:
{{"mesma_pessoa": true|false|null,   // a candidata mostra o jogador procurado? (null só se não houver referência e não der para saber)
 "foto_limpa": true|false,          // fotografia real, SEM texto sobreposto, logo de emissora/portal, arte gráfica, montagem, moldura, marca d'água ou layout de outro canal
 "boa_para_capa": true|false,       // jogador em destaque, nítido, rosto visível, cabe num recorte vertical 4:5
 "camisa_do_clube": true|false|null, // está com a camisa do clube informado? (null se não houver clube ou não der para ver)
 "corpo_inteiro_ou_busto": true|false, // aparece pelo menos da cintura para cima (bom para recortar)
 "motivo": "até 15 palavras"}}
Seja rigoroso: qualquer texto ou grafismo editado por cima da foto = foto_limpa false."""


async def checar(cand_bytes: bytes, nome: str, clube: str | None, ref_bytes: bytes | None) -> dict:
    k = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not k:
        return {"ok": False, "motivo": "sem chave da IA para conferir a foto"}
    conteudo: list[dict[str, Any]] = []
    if ref_bytes:
        conteudo += [{"type": "text", "text": "IMAGEM 1 — referência oficial do jogador (pode ser pequena):"},
                     {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _b64_jpeg(ref_bytes, 256)}}]
    conteudo += [{"type": "text", "text": ("IMAGEM 2" if ref_bytes else "IMAGEM") + " — candidata:"},
                 {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _b64_jpeg(cand_bytes)}},
                 {"type": "text", "text": PROMPT_VISAO.format(
                     ref_txt="Compare a IMAGEM 2 com a IMAGEM 1." if ref_bytes else "Não há foto de referência.",
                     nome=nome, clube_txt=f" ({clube})" if clube else "")}]
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(os.getenv("ANTHROPIC_BASE", "https://api.anthropic.com") + "/v1/messages",
                             headers={"x-api-key": k, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                             json={"model": MODELO_VISAO, "max_tokens": 300, "messages": [{"role": "user", "content": conteudo}]})
        txt = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        m = re.search(r"\{.*\}", txt, re.S)
        v = json.loads(m.group(0)) if m else {}
    except Exception as e:
        return {"ok": False, "motivo": f"falha na conferência: {type(e).__name__}"}
    pessoa_ok = v.get("mesma_pessoa") is True or (v.get("mesma_pessoa") is None and not ref_bytes)
    ok = bool(v.get("foto_limpa")) and bool(v.get("boa_para_capa")) and pessoa_ok
    return {"ok": ok, **v}


# ------------------------------------------------------------ principal --
async def foto_verificada(nome: str | None, clube: str | None = None, jogador_id: int | None = None) -> dict | None:
    """Devolve {"url", "credito", "nome"} de uma foto conferida, ou None."""
    ref = await referencia_api(nome, jogador_id)
    if not ref and not nome:
        return None
    # nome de camisa primeiro ("Hulk"), depois o completo ("Givanildo Vieira de Sousa")
    nomes = []
    for x in ((ref or {}).get("nome"), nome, (ref or {}).get("nome_completo")):
        if x and x not in nomes:
            nomes.append(x)
    nome_busca = " / ".join(nomes[:2])
    ref_bytes = None
    if ref and ref.get("foto_ref"):
        try:
            ref_bytes, _ = await fotos.baixar(ref["foto_ref"], 400)
        except Exception:
            ref_bytes = None
    cands, vistos = [], set()
    for n in nomes[:2]:
        for cnd in await candidatas_wikimedia(n) + await candidatas_web(n, clube):
            if cnd["url"] not in vistos:
                vistos.add(cnd["url"])
                cands.append(cnd)
    reserva = None  # foto aprovada, mas com camisa de outro clube: só se não houver melhor
    for cnd in cands[:10]:
        try:
            dados, _ = await fotos.baixar(cnd["url"], 1600)
        except Exception:
            continue
        v = await checar(dados, nome_busca, clube, ref_bytes)
        log.info("[FOTO] %s · %s · %s", nome_busca, cnd["origem"], v)
        if not v.get("ok"):
            continue
        achada = {"url": cnd["url"], "credito": cnd["credito"], "nome": nome_busca, "aviso": None}
        if not clube or v.get("camisa_do_clube") is not False:
            return achada
        if not reserva:
            reserva = {**achada, "aviso": f"foto de {nome_busca} com camisa de outro clube: confira"}
    return reserva
