"""Tabela de classificação no fim de cada rodada + resumo dos resultados do dia.

Pedido do Nog (16/10/2026):
- Fim de rodada → tabela de classificação de Brasileirão Série A, Champions
  (fase de liga), LaLiga e Premier League. Sai sem aprovação.
- Resultados do dia → carrossel com todos os placares, dividido por
  campeonato. Sai sem aprovação.

Rodada "acabou" = todos os jogos daquela rodada terminaram (adiados/cancelados
não seguram a rodada) e o último jogo acabou nas últimas 36 horas.
Dados: API-Football (fixtures, standings).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from . import rodada
from . import resultados as res

TABELAS = {
    71: {"nome": "Brasileirão Série A", "curto": "Brasileirão", "tag": "#Brasileirao"},
    2: {"nome": "Champions League", "curto": "Champions", "tag": "#ChampionsLeague"},
    140: {"nome": "LaLiga", "curto": "LaLiga", "tag": "#LaLiga"},
    39: {"nome": "Premier League", "curto": "Premier", "tag": "#PremierLeague"},
}
FORA = {"PST", "CANC", "ABD", "AWD", "WO", "SUSP"}   # não seguram a rodada
POR_SLIDE = 10


def _rodada_valida(lid: int, round_: str) -> bool:
    r = round_ or ""
    if lid == 2:  # Champions: só a fase de liga tem tabela
        return bool(re.search(r"League Stage\s*-\s*\d+", r))
    return bool(re.search(r"Regular Season\s*-\s*\d+", r))


def rodadas_candidatas(jogos: list[dict]) -> set[tuple[int, int, str]]:
    """(liga, temporada, rodada) de jogos encerrados das ligas com tabela."""
    out = set()
    for j in jogos:
        lg = j.get("league") or {}
        if lg.get("id") in TABELAS and j["fixture"]["status"]["short"] in res.FIM and _rodada_valida(lg["id"], lg.get("round")):
            out.add((lg["id"], lg.get("season"), lg.get("round")))
    return out


async def rodada_encerrada(lid: int, temporada: int, round_: str) -> bool:
    d = await res._get("fixtures", {"league": lid, "season": temporada, "round": round_, "timezone": "America/Sao_Paulo"})
    js = d.get("response") or []
    if not js:
        return False
    sts = [j["fixture"]["status"]["short"] for j in js]
    if any(s not in res.FIM and s not in FORA for s in sts):
        return False
    fim = [datetime.fromisoformat(j["fixture"]["date"].replace("Z", "+00:00")) for j in js if j["fixture"]["status"]["short"] in res.FIM]
    return bool(fim) and datetime.now(timezone.utc) - max(fim) < timedelta(hours=36)


async def classificacao(lid: int, temporada: int) -> list[dict]:
    d = await res._get("standings", {"league": lid, "season": temporada})
    try:
        grupos = d["response"][0]["league"]["standings"]
    except (KeyError, IndexError):
        return []
    linhas = max(grupos, key=len) if grupos else []
    return linhas


def _num_rodada(round_: str) -> str:
    m = re.search(r"(\d+)\s*$", round_ or "")
    return m.group(1) if m else ""


def montar_tabela(lid: int, round_: str, linhas: list[dict], handle: str = "seutipster.app") -> dict | None:
    if not linhas:
        return None
    info = TABELAS[lid]
    n = _num_rodada(round_)
    partes = [linhas[i:i + POR_SLIDE] for i in range(0, len(linhas), POR_SLIDE)]
    slides = []
    for k, parte in enumerate(partes, 1):
        slides.append({"modelo": "tabela", "dados": {
            "tag": "Classificação", "tag2": info["curto"],
            "titulo": f"*{info['nome']}*" + (f" · após a {n}ª rodada" if n else ""),
            "meta": {"liga": lid, "temporada": None, "times": [(parte[0].get("team") or {}).get("id")]} if k == 1 else None,
            "parte": f"{k}/{len(partes)}" if len(partes) > 1 else "",
            "linhas": [{
                "pos": str(r.get("rank")),
                "time": rodada.nome((r.get("team") or {}).get("name", "")),
                "escudo": (r.get("team") or {}).get("logo"),
                "pts": str(r.get("points")),
                "j": str((r.get("all") or {}).get("played", "")),
                "v": str((r.get("all") or {}).get("win", "")),
                "sg": (f"+{r.get('goalsDiff')}" if (r.get("goalsDiff") or 0) > 0 else str(r.get("goalsDiff"))),
            } for r in parte],
        }})
    lider, vice = linhas[0], (linhas[1] if len(linhas) > 1 else None)
    nl = rodada.nome(lider["team"]["name"])
    dif = (lider["points"] - vice["points"]) if vice else 0
    gancho = (f"Fim da {n}ª rodada {'da' if lid in (2, 140, 39) else 'do'} {info['nome']}: "
              if n else f"Classificação {'da' if lid in (2, 140, 39) else 'do'} {info['nome']}: ")
    gancho += f"{nl} lidera com {lider['points']} pontos" + (f", {dif} à frente do {rodada.nome(vice['team']['name'])}." if vice and dif else ".")
    top = "\n".join(f"{r['rank']}º {rodada.nome(r['team']['name'])} · {r['points']} pts" for r in linhas[:4])
    legenda = (f"{gancho}\n\n{top}\n\nArrasta pro lado e veja a tabela completa. Seu time tá onde? Comenta aí 👇\n\n"
               f"{info['tag']} #tabela #futebol #SeuTipster")
    return {"perfil_handle": handle, "linha": "tabela", "origem": "robo", "auto": True,
            "titulo": f"Tabela · {info['nome']}" + (f" · {n}ª rodada" if n else ""),
            "legenda": legenda, "fonte": "API-Football", "slides": slides[:10]}


# ------------------------------------------------------ resultados do dia --
def jogos_do_resumo(jogos: list[dict]) -> list[dict]:
    """Jogos das ligas prioritárias encerrados no dia, na ordem de prioridade e horário."""
    ok = [j for j in jogos if j["fixture"]["status"]["short"] in res.FIM]
    ok.sort(key=lambda j: (rodada.ORDEM[j["league"]["id"]], j["fixture"]["date"]))
    return ok


def dia_encerrado(jogos: list[dict]) -> bool:
    """Todos os jogos do dia terminaram (ou foram adiados/cancelados)."""
    return bool(jogos) and all(j["fixture"]["status"]["short"] in res.FIM or j["fixture"]["status"]["short"] in FORA for j in jogos)


def montar_resumo(jogos: list[dict], dia: date, handle: str = "seutipster.app") -> dict | None:
    fim = jogos_do_resumo(jogos)
    if not fim:
        return None
    por_liga: dict[int, list[dict]] = {}
    for j in fim:
        por_liga.setdefault(j["league"]["id"], []).append(j)
    data_curta = f"{rodada.DIAS[dia.weekday()]} {dia.day:02d}/{dia.month:02d}"
    slides = []
    for lid, lista in por_liga.items():
        n_partes = -(-len(lista) // 8)
        tam = -(-len(lista) // n_partes)
        for k in range(0, len(lista), tam):
            parte = lista[k:k + tam]
            if len(slides) >= 10:
                break
            slides.append({"modelo": "rodada", "dados": {
                "tag": "Resultados do dia", "tag2": data_curta,
                "titulo": f"*{rodada.IDS[lid]['nome']}*",
                "logo_liga": parte[0]["league"].get("logo"),
                "jogos": [{
                    "hora": rodada.hora(j["fixture"]["date"]),
                    "casa": rodada.nome(j["teams"]["home"]["name"]), "fora": rodada.nome(j["teams"]["away"]["name"]),
                    "escudo_casa": j["teams"]["home"].get("logo"), "escudo_fora": j["teams"]["away"].get("logo"),
                    "gols_casa": j["goals"]["home"], "gols_fora": j["goals"]["away"],
                    "penaltis": (f"{j['score']['penalty']['home']}-{j['score']['penalty']['away']} nos pênaltis"
                                 if (j.get("score") or {}).get("penalty", {}).get("home") is not None else ""),
                } for j in parte],
            }})
    linhas = []
    for lid, lista in por_liga.items():
        placares = "; ".join(f"{rodada.nome(j['teams']['home']['name'])} {j['goals']['home']}x{j['goals']['away']} "
                             f"{rodada.nome(j['teams']['away']['name'])}" for j in lista[:4])
        extra = f" e mais {len(lista) - 4}" if len(lista) > 4 else ""
        linhas.append(f"🏆 {rodada.IDS[lid]['nome']}: {placares}{extra}")
    legenda = (f"Todos os resultados de {data_curta} nas principais ligas, num lugar só. ⚽\n\n" + "\n".join(linhas) +
               "\n\nArrasta pro lado e salva. Qual resultado mais te surpreendeu? Comenta aí 👇\n\n"
               "#futebol #resultados #SeuTipster")
    return {"perfil_handle": handle, "linha": "resultado", "origem": "robo", "auto": True,
            "titulo": f"Resultados do dia · {data_curta} · {len(fim)} jogos",
            "legenda": legenda[:2200], "fonte": "API-Football", "slides": slides}
