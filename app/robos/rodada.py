"""Robô da Rodada do dia.

Todo dia de manhã busca os jogos das ligas prioritárias na API-Football e
monta um carrossel: capa + 1 slide por liga (até 8 jogos por slide, até 10
slides). Foto do tema: o estádio do jogo principal de cada liga (imagem de
estádio da própria API-Football). Legenda montada por regra, sem IA.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

TZ = ZoneInfo("America/Sao_Paulo")
BASE = os.getenv("API_FOOTBALL_BASE", "https://v3.football.api-sports.io")
MEDIA = os.getenv("API_FOOTBALL_MEDIA", "https://media.api-sports.io")


def chave() -> str:
    return (os.getenv("API_FOOTBALL_KEY") or os.getenv("APIFOOTBALL_KEY") or "").strip()


# Ordem = prioridade (a primeira liga com jogo vira o destaque da capa)
LIGAS: list[dict[str, Any]] = [
    {"id": 13,  "nome": "Libertadores",       "curto": "Libertadores"},
    {"id": 71,  "nome": "Brasileirão",        "curto": "Brasileirão"},
    {"id": 73,  "nome": "Copa do Brasil",     "curto": "Copa do Brasil"},
    {"id": 11,  "nome": "Sul-Americana",      "curto": "Sul-Americana"},
    {"id": 2,   "nome": "Champions League",   "curto": "Champions"},
    {"id": 39,  "nome": "Premier League",     "curto": "Premier"},
    {"id": 140, "nome": "LaLiga",             "curto": "LaLiga"},
    {"id": 135, "nome": "Serie A",            "curto": "Serie A"},
    {"id": 78,  "nome": "Bundesliga",         "curto": "Bundesliga"},
    {"id": 61,  "nome": "Ligue 1",            "curto": "Ligue 1"},
]
IDS = {l["id"]: l for l in LIGAS}
ORDEM = {l["id"]: i for i, l in enumerate(LIGAS)}

# Nomes como o torcedor brasileiro escreve (a API vem sem acento / em inglês)
NOMES = {
    "Sao Paulo": "São Paulo", "Gremio": "Grêmio", "Atletico-MG": "Atlético-MG", "Atletico Mineiro": "Atlético-MG",
    "Atletico Paranaense": "Athletico-PR", "Athletico Paranaense": "Athletico-PR", "Vasco DA Gama": "Vasco",
    "Vasco da Gama": "Vasco", "Vitoria": "Vitória", "Ceara": "Ceará", "Goias": "Goiás", "Cuiaba": "Cuiabá",
    "America Mineiro": "América-MG", "Avai": "Avaí", "Criciuma": "Criciúma", "Sport Recife": "Sport",
    "RB Bragantino": "Bragantino", "Red Bull Bragantino": "Bragantino", "Atletico Goianiense": "Atlético-GO",
    "Juventude": "Juventude", "Mirassol": "Mirassol", "Remo": "Remo", "Paysandu": "Paysandu",
    "Bayern Munich": "Bayern", "Bayern München": "Bayern", "Paris Saint Germain": "PSG", "Manchester United": "Man United",
    "Manchester City": "Man City", "Atletico Madrid": "Atlético de Madrid", "Inter": "Inter de Milão",
    "AC Milan": "Milan", "Borussia Dortmund": "Dortmund", "Bayer Leverkusen": "Leverkusen", "Sporting CP": "Sporting",
    "Tottenham": "Tottenham", "Nottingham Forest": "Nottingham", "Wolves": "Wolves", "Real Betis": "Betis",
}

DIAS = ["SEG", "TER", "QUA", "QUI", "SEX", "SÁB", "DOM"]
DIAS_LONGO = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]


def nome(n: str) -> str:
    return NOMES.get(n, n)


def fase(round_: str) -> str:
    r = (round_ or "").strip()
    m = re.search(r"Regular Season\s*-\s*(\d+)", r)
    if m:
        return f"{m.group(1)}ª rodada"
    m = re.search(r"League Stage\s*-\s*(\d+)", r)
    if m:
        return f"{m.group(1)}ª rodada"
    mapa = [
        ("Semi-finals", "semifinal"), ("Quarter-finals", "quartas de final"), ("Round of 16", "oitavas de final"),
        ("Round of 32", "16 avos de final"), ("Final", "final"), ("Group Stage", "fase de grupos"),
        ("Knockout Round Play-offs", "playoffs"), ("Play-offs", "playoffs"),
    ]
    for en, pt in mapa:
        if en.lower() in r.lower():
            return pt
    return ""


def hora(iso: str) -> str:
    d = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ)
    return f"{d.hour:02d}:{d.minute:02d}"


def _foto_estadio(jogo: dict) -> str | None:
    vid = ((jogo.get("fixture") or {}).get("venue") or {}).get("id")
    return f"{MEDIA}/football/venues/{vid}.png" if vid else None


async def jogos_do_dia(dia: date) -> list[dict]:
    k = chave()
    if not k:
        raise RuntimeError("API_FOOTBALL_KEY não configurada no servidor")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{BASE}/fixtures", params={"date": dia.isoformat(), "timezone": "America/Sao_Paulo"},
                        headers={"x-apisports-key": k})
    r.raise_for_status()
    data = r.json()
    if data.get("errors"):
        raise RuntimeError(f"API-Football: {data['errors']}")
    out = []
    for j in data.get("response", []):
        lid = (j.get("league") or {}).get("id")
        st = ((j.get("fixture") or {}).get("status") or {}).get("short", "")
        if lid in IDS and st not in ("PST", "CANC", "ABD", "AWD", "WO"):
            out.append(j)
    out.sort(key=lambda j: (ORDEM[j["league"]["id"]], j["fixture"]["date"]))
    return out


def montar(jogos: list[dict], dia: date, handle: str = "seutipster.app") -> dict | None:
    """Monta o pedido de post (slides + legenda) no formato de /api/robo/cards."""
    if not jogos:
        return None
    por_liga: dict[int, list[dict]] = {}
    for j in jogos:
        por_liga.setdefault(j["league"]["id"], []).append(j)
    ligas = sorted(por_liga, key=lambda i: ORDEM[i])

    destaque = por_liga[ligas[0]][0]
    casa_d, fora_d = nome(destaque["teams"]["home"]["name"]), nome(destaque["teams"]["away"]["name"])
    total = len(jogos)
    primeiro = min(hora(j["fixture"]["date"]) for j in jogos)
    data_curta = f"{DIAS[dia.weekday()]} {dia.day:02d}/{dia.month:02d}"
    data_longa = f"{DIAS_LONGO[dia.weekday()]}, {dia.day} de {MESES[dia.month - 1]}"

    resto = total - 1
    titulo = (f"Hoje tem *{casa_d} x {fora_d}* e mais {resto} jogo{'s' if resto != 1 else ''}"
              if resto else f"Hoje tem *{casa_d} x {fora_d}*")
    fatos = [{"n": str(len(por_liga[i])), "l": IDS[i]["curto"]} for i in ligas[:3]]
    fatos.append({"n": primeiro.replace(":", "H"), "l": "primeiro jogo"})
    slides = [{"modelo": "noticia", "dados": {
        "foto": _foto_estadio(destaque), "foco": "center 45%",
        "tag": "Rodada do dia", "tag2": data_curta,
        "titulo": titulo, "fatos": fatos, "arrasta": len(ligas) > 0,
    }}]

    for lid in ligas:
        lista = por_liga[lid]
        if len(lista) <= 2:
            # poucos jogos: cada um ganha um card de destaque (escudos grandes + horário)
            for j in lista:
                if len(slides) >= 10:
                    break
                fs = fase(j["league"].get("round", ""))
                estadio = ((j.get("fixture") or {}).get("venue") or {}).get("name")
                slides.append({"modelo": "resultado", "dados": {
                    "foto": _foto_estadio(j), "tag": "Hoje", "tag2": IDS[lid]["curto"],
                    "casa": nome(j["teams"]["home"]["name"]), "fora": nome(j["teams"]["away"]["name"]),
                    "escudo_casa": j["teams"]["home"].get("logo"), "escudo_fora": j["teams"]["away"].get("logo"),
                    "gols_casa": None, "hora": hora(j["fixture"]["date"]),
                    "info": " · ".join(x for x in (IDS[lid]["nome"], fs, estadio) if x),
                }})
            continue
        n_partes = -(-len(lista) // 8)          # divide igual: 9 jogos viram 5 + 4, não 8 + 1
        tam = -(-len(lista) // n_partes)
        partes = [lista[i:i + tam] for i in range(0, len(lista), tam)]
        for n, parte in enumerate(partes):
            if len(slides) >= 10:
                break
            fs = fase(parte[0]["league"].get("round", ""))
            sufixo = f" ({n + 1}/{len(partes)})" if len(partes) > 1 else ""
            slides.append({"modelo": "rodada", "dados": {
                "foto": _foto_estadio(parte[0]), "tag": IDS[lid]["curto"],
                "titulo": f"*{IDS[lid]['nome']}*" + (f" · {fs}" if fs else "") + sufixo,
                "data": data_longa, "logo_liga": parte[0]["league"].get("logo"),
                "jogos": [{
                    "hora": hora(j["fixture"]["date"]),
                    "casa": nome(j["teams"]["home"]["name"]), "fora": nome(j["teams"]["away"]["name"]),
                    "escudo_casa": j["teams"]["home"].get("logo"), "escudo_fora": j["teams"]["away"].get("logo"),
                } for j in parte],
            }})

    # legenda (regras da skill: gancho nos 125 primeiros caracteres, 3 a 5 hashtags, número concreto)
    linhas = []
    for lid in ligas:
        lista = por_liga[lid]
        tops = ", ".join(f"{nome(j['teams']['home']['name'])} x {nome(j['teams']['away']['name'])} ({hora(j['fixture']['date']).replace(':00', 'h').replace(':', 'h')})"
                         for j in lista[:2])
        extra = f" e mais {len(lista) - 2}" if len(lista) > 2 else ""
        linhas.append(f"🏆 {IDS[lid]['nome']}: {tops}{extra}")
    gancho = f"{total} jogo{'s' if total != 1 else ''} hoje nas principais ligas, e o destaque é {casa_d} x {fora_d}."
    legenda = (
        f"{gancho}\n\n" + "\n".join(linhas) +
        "\n\nArrasta pro lado e salva pra não perder nenhum horário.\n\n"
        "Quer palpite pra esses jogos? Tem análise de todos no app SeuTipster, link na bio.\n\n"
        "#futebol #rodadadodia #palpites #SeuTipster"
    )
    sug = datetime(dia.year, dia.month, dia.day, 9, 0, tzinfo=TZ)
    return {
        "perfil_handle": handle, "linha": "rodada", "origem": "robo",
        "titulo": f"Rodada do dia · {data_curta} · {total} jogos",
        "legenda": legenda, "fonte": "API-Football",
        "sugerido_para": sug.isoformat(),
        "foto_opcional": True,  # foto de estádio que não vier (ou vier genérica) não derruba o carrossel
        "slides": slides,
    }
