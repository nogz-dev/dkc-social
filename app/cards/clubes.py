"""Escudo oficial de um clube pela API-Football (para cards sem foto de jogador).

Escudo de clube não é arte de outro veículo: é a marca do próprio clube, servida pela API-Football,
a mesma fonte dos cards de rodada e resultado.
"""
from __future__ import annotations

import logging
import os
import time
import unicodedata

import httpx

log = logging.getLogger("dkc.clubes")
API_FOOTBALL = os.getenv("API_FOOTBALL_BASE", "https://v3.football.api-sports.io")
_cache: dict[str, tuple[float, dict | None]] = {}

# Apelidos comuns → nome que a API-Football reconhece na busca
APELIDOS = {
    "atletico-mg": "Atletico Mineiro", "atlético-mg": "Atletico Mineiro", "galo": "Atletico Mineiro",
    "athletico-pr": "Athletico Paranaense", "athletico": "Athletico Paranaense",
    "sao paulo": "Sao Paulo", "são paulo": "Sao Paulo", "gremio": "Gremio", "grêmio": "Gremio",
    "vasco": "Vasco DA Gama", "barça": "Barcelona", "barca": "Barcelona", "real": "Real Madrid",
    "atlético de madrid": "Atletico Madrid", "atletico de madrid": "Atletico Madrid",
    "psg": "Paris Saint Germain", "inter de milão": "Inter", "man city": "Manchester City",
    "manchester united": "Manchester United", "man united": "Manchester United", "bayern": "Bayern Munich",
}


def _sem_acento(s: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFD", s or "") if unicodedata.category(ch) != "Mn").lower().strip()


async def escudo(nome: str | None) -> dict | None:
    """{"id", "nome", "logo"} do clube, ou None. Cache de 7 dias (a busca gasta cota da API)."""
    if not nome or len(nome.strip()) < 3:
        return None
    k = (os.getenv("API_FOOTBALL_KEY") or os.getenv("APIFOOTBALL_KEY") or "").strip()
    if not k:
        return None
    chave = _sem_acento(nome)
    c = _cache.get(chave)
    if c and time.time() - c[0] < 7 * 86400:
        return c[1]
    termo = APELIDOS.get(chave) or APELIDOS.get(nome.strip().lower()) or nome.strip()
    achado = None
    try:
        async with httpx.AsyncClient(timeout=15) as cl:
            r = await cl.get(f"{API_FOOTBALL}/teams", params={"search": _sem_acento(termo)[:40]}, headers={"x-apisports-key": k})
        lista = [x.get("team") or {} for x in (r.json().get("response") or [])]
        # prefere time principal (não feminino/sub-20/B) e nome que bate inteiro
        alvo = _sem_acento(termo)

        def nota(t: dict) -> tuple:
            n = _sem_acento(t.get("name") or "")
            ruim = any(x in n for x in (" w", "women", "fem", "u20", "u21", "u23", "u19", " ii", " b"))
            return (not ruim, n == alvo, alvo in n, not t.get("national"))

        lista = [t for t in lista if t.get("logo")]
        if lista:
            t = max(lista, key=nota)
            achado = {"id": t.get("id"), "nome": t.get("name"), "logo": t.get("logo")}
    except Exception as e:
        log.warning("[CLUBES] busca de %s falhou: %s", nome, e)
        return None
    _cache[chave] = (time.time(), achado)
    return achado
