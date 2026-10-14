"""Robô de resultados.

A cada 5 minutos confere os jogos do dia na API-Football. Jogo dos principais
que acabou vira card de resultado (placar, escudos, autores dos gols, foto do
estádio) com legenda montada por regra. É o único tipo de post que sai sem
aprovação (regra do servidor, com chave em Ajustes).
"""
from __future__ import annotations

import os
from typing import Any

import httpx

from . import rodada

INTERVALO_MIN = int(os.getenv("RESULTADOS_INTERVALO_MIN", "5"))
TETO_DIA = int(os.getenv("RESULTADOS_TETO_DIA", "0"))  # 0 = sem limite
FIM = {"FT", "AET", "PEN"}

# competições em que TODO jogo é principal
LIGAS_TODAS = {13, 11, 73, 71, 2}  # Libertadores, Sul-Americana, Copa do Brasil, Brasileirão, Champions
# ligas europeias: só jogos com clube grande
LIGAS_GRANDES = {39, 140, 135, 78, 61}
GRANDES = {
    541, 529, 530,                    # Real Madrid, Barcelona, Atlético de Madrid
    50, 40, 42, 49, 33, 47,           # Man City, Liverpool, Arsenal, Chelsea, Man United, Tottenham
    157, 165, 168,                    # Bayern, Dortmund, Leverkusen
    85,                               # PSG
    505, 489, 496, 492, 497,          # Inter, Milan, Juventus, Napoli, Roma
}

HASHTAG_LIGA = {13: "#Libertadores", 11: "#SulAmericana", 73: "#CopaDoBrasil", 71: "#Brasileirão",
                2: "#ChampionsLeague", 39: "#PremierLeague", 140: "#LaLiga", 135: "#SerieA",
                78: "#Bundesliga", 61: "#Ligue1"}


def principal(j: dict) -> bool:
    lid = j["league"]["id"]
    if lid in LIGAS_TODAS:
        return True
    if lid in LIGAS_GRANDES:
        return j["teams"]["home"].get("id") in GRANDES or j["teams"]["away"].get("id") in GRANDES
    return False


async def _get(path: str, params: dict) -> dict:
    k = rodada.chave()
    if not k:
        raise RuntimeError("API_FOOTBALL_KEY não configurada no servidor")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{rodada.BASE}/{path}", params=params, headers={"x-apisports-key": k})
    r.raise_for_status()
    d = r.json()
    if d.get("errors"):
        raise RuntimeError(f"API-Football: {d['errors']}")
    return d


async def jogos(dia) -> list[dict]:
    d = await _get("fixtures", {"date": dia.isoformat(), "timezone": "America/Sao_Paulo"})
    return [j for j in d.get("response", []) if j.get("league", {}).get("id") in rodada.IDS]


async def gols(fixture_id: int) -> list[dict]:
    d = await _get("fixtures/events", {"fixture": fixture_id})
    out = []
    for e in d.get("response", []):
        if e.get("type") != "Goal" or (e.get("detail") or "") == "Missed Penalty":
            continue
        t = e.get("time") or {}
        out.append({
            "time_id": (e.get("team") or {}).get("id"),
            "jogador": ((e.get("player") or {}).get("name") or "").strip(),
            "min": t.get("elapsed") or 0, "extra": t.get("extra"),
            "tipo": e.get("detail") or "",
        })
    out.sort(key=lambda g: (g["min"], g["extra"] or 0))
    return out


def _min(g: dict) -> str:
    return f"{g['min']}+{g['extra']}'" if g.get("extra") else f"{g['min']}'"


def _autor(g: dict) -> str:
    nome = g["jogador"] or "Gol"
    # "V. Junior" → "Junior": no card fica o sobrenome, como o torcedor fala
    partes = nome.split()
    if len(partes) > 1 and partes[0].endswith("."):
        nome = " ".join(partes[1:])
    suf = " (contra)" if "Own" in g["tipo"] else (" (pên.)" if "Penalty" in g["tipo"] else "")
    return f"{nome} {_min(g)}{suf}"


def _agrupar(gs: list[dict]) -> list[str]:
    """Mesmo jogador vira uma linha só: "Salah 10', 20', 30'"."""
    ordem, por = [], {}
    for g in gs:
        a = _autor(g)
        chave = (g["jogador"], "Own" in g["tipo"])
        if chave not in por:
            por[chave] = [a.rsplit(" " + _min(g), 1)[0], []]
            ordem.append(chave)
        suf = " (pên.)" if "Penalty" in g["tipo"] else ""
        por[chave][1].append(_min(g) + suf)
    out = []
    for k in ordem:
        nome, mins = por[k]
        contra = " (contra)" if k[1] else ""
        out.append(f"{nome.replace(' (contra)', '')} {', '.join(mins)}{contra}")
    return out


def virada(gs: list[dict], casa_id: int, vencedor_id: int | None) -> bool:
    """O vencedor esteve atrás no placar em algum momento?"""
    if not vencedor_id:
        return False
    a = b = 0
    for g in gs:
        marcou_casa = g["time_id"] == casa_id
        if "Own" in g["tipo"]:
            marcou_casa = not marcou_casa
        if marcou_casa:
            a += 1
        else:
            b += 1
        venc, perd = (a, b) if vencedor_id == casa_id else (b, a)
        if venc < perd:
            return True
    return False


def montar(j: dict, gs: list[dict], handle: str = "seutipster.app") -> dict:
    lid = j["league"]["id"]
    liga = rodada.IDS[lid]
    th, ta = j["teams"]["home"], j["teams"]["away"]
    casa, fora = rodada.nome(th["name"]), rodada.nome(ta["name"])
    gc, gf = j["goals"]["home"] or 0, j["goals"]["away"] or 0
    st = j["fixture"]["status"]["short"]
    pen = (j.get("score") or {}).get("penalty") or {}
    pen_txt = f" (pênaltis {pen.get('home')} x {pen.get('away')})" if st == "PEN" and pen.get("home") is not None else ""

    venc_id = th["id"] if th.get("winner") else (ta["id"] if ta.get("winner") else None)
    venc, perd = (casa, fora) if venc_id == th["id"] else (fora, casa)
    vg, pg = (gc, gf) if venc_id == th["id"] else (gf, gc)
    fase = rodada.fase(j["league"].get("round", ""))
    virou = virada(gs, th["id"], venc_id) if st != "PEN" else False

    if st == "PEN" and venc_id:
        titulo = f"*{venc}* passa nos pênaltis"
    elif not venc_id:
        titulo = f"{casa} e {fora} *empatam*" if gc else f"{casa} e {fora} *ficam no 0 a 0*"
    elif virou:
        titulo = f"*{venc}* vira contra {perd}"
    elif vg - pg >= 3:
        titulo = f"*{venc}* atropela {perd}"
    else:
        titulo = f"*{venc}* bate {perd}"
    if fase and len(titulo) < 40:
        titulo += f" na {fase}" if fase in ("semifinal", "final") else ""

    gols_casa = _agrupar([g for g in gs if (g["time_id"] == th["id"]) != ("Own" in g["tipo"])])
    gols_fora = _agrupar([g for g in gs if (g["time_id"] == ta["id"]) != ("Own" in g["tipo"])])

    vid = ((j.get("fixture") or {}).get("venue") or {}).get("id")
    dados = {
        "foto": f"{rodada.MEDIA}/football/venues/{vid}.png" if vid else None,
        "tag": "Fim de jogo", "tag2": liga["curto"],
        "casa": casa, "fora": fora, "gols_casa": gc, "gols_fora": gf,
        "escudo_casa": th.get("logo"), "escudo_fora": ta.get("logo"),
        "autores_casa": gols_casa[:6], "autores_fora": gols_fora[:6],
        "titulo": titulo,
        "penaltis": f"Pênaltis {pen.get('home')} x {pen.get('away')}" if pen_txt else None,
    }

    # legenda por regra: gancho com o placar, gols, pergunta
    placar = f"{casa} {gc} x {gf} {fora}{pen_txt}"
    if st == "PEN" and venc_id:
        gancho = f"{venc} passa nos pênaltis! {placar}."
    elif not venc_id:
        gancho = f"Empate: {placar}."
    elif virou:
        gancho = f"Virada! {placar}."
    else:
        gancho = f"Fim de jogo: {placar}."
    blocos = [gancho]
    contexto = " · ".join(x for x in (liga["nome"], fase) if x)
    if contexto:
        blocos.append(contexto)
    if gs:
        blocos.append("⚽ " + " · ".join(_agrupar(gs)))
    pergunta = (f"Quem foi o melhor em campo?" if venc_id else "Resultado justo?")
    blocos.append(pergunta + " 👇")
    tags = [f"#{casa.replace(' ', '').replace('-', '')}", f"#{fora.replace(' ', '').replace('-', '')}", HASHTAG_LIGA.get(lid, "")]
    blocos.append(" ".join(t for t in tags if t))
    return {
        "perfil_handle": handle, "linha": "resultado", "origem": "robo",
        "titulo": f"{placar} · {liga['curto']}",
        "legenda": "\n\n".join(blocos), "fonte": "API-Football",
        "slides": [{"modelo": "resultado", "dados": dados}],
    }
