"""Robô de notícias.

A cada ciclo (15 min): lê os feeds dos principais portais, separa o que é
novo, pede para uma IA rápida dar nota (0–10) pensando no público do perfil e
agrupar a mesma notícia de fontes diferentes, e para os assuntos fortes escreve
o post completo (card + legenda no padrão da skill de Instagram), com foto.

Nada daqui publica: tudo vai para a fila de aprovação.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser
import httpx

log = logging.getLogger("dkc")

UA = "Mozilla/5.0 (compatible; DKCSocial/1.0)"

FEEDS_PADRAO: list[tuple[str, str]] = [
    # Brasil
    ("ge", "https://ge.globo.com/rss/ge/futebol/"),
    ("UOL Esporte", "https://rss.uol.com.br/feed/esporte.xml"),
    ("Goal Brasil", "https://www.goal.com/feeds/br/news"),
    ("ESPN Brasil", "https://www.espn.com.br/rss/futebol"),
    ("Lance!", "https://www.lance.com.br/feed"),
    ("Trivela", "https://trivela.com.br/feed/"),
    ("Placar", "https://placar.com.br/feed/"),
    # Europa
    ("BBC Sport", "https://feeds.bbci.co.uk/sport/football/rss.xml"),
    ("Sky Sports", "https://www.skysports.com/rss/11095"),
    ("Sky Sports", "https://www.skysports.com/rss/12040"),
    ("The Guardian", "https://www.theguardian.com/football/rss"),
    ("ESPN FC", "https://www.espn.com/espn/rss/soccer/news"),
    ("AS", "https://feeds.as.com/mrss-s/pages/as/site/as.com/section/futbol/portada/"),
    ("Marca", "https://e00-marca.uecdn.es/rss/futbol/primera-division.xml"),
    ("Mundo Deportivo", "https://www.mundodeportivo.com/rss/futbol"),
    ("Gazzetta", "https://www.gazzetta.it/rss/calcio.xml"),
    # mercado europeu, bastidores e rumores de transferência
    ("Football365", "https://www.football365.com/feed"),
    ("TEAMtalk", "https://www.teamtalk.com/feed"),
    ("CaughtOffside", "https://www.caughtoffside.com/feed/"),
    ("Football Italia", "https://football-italia.net/feed/"),
    ("Fichajes", "https://www.fichajes.net/rss"),
    ("ESPN FC", "https://www.espn.com/espn/rss/soccer/transfers"),
    # mercado de apostas / regulamentação
    ("BNL Data", "https://bnldata.com.br/feed/"),
    ("iGaming Brazil", "https://igamingbrazil.com/feed/"),
    ("Games Magazine Brasil", "https://www.gamesbras.com/feed"),
]


def feeds() -> list[tuple[str, str]]:
    """NOTICIAS_FEEDS="Nome|url,Nome|url" substitui a lista padrão."""
    env = os.getenv("NOTICIAS_FEEDS", "").strip()
    if not env:
        return FEEDS_PADRAO
    out = []
    for parte in env.split(","):
        if "|" in parte:
            n, u = parte.split("|", 1)
            out.append((n.strip(), u.strip()))
    return out or FEEDS_PADRAO


INTERVALO_MIN = int(os.getenv("NOTICIAS_INTERVALO_MIN", "10"))
TETO_DIA = int(os.getenv("NOTICIAS_TETO_DIA", "30"))  # 0 = sem limite
NOTA_MIN = float(os.getenv("NOTICIAS_NOTA_MIN", "7"))
NOTA_URGENTE = float(os.getenv("NOTICIAS_NOTA_URGENTE", "9"))
TOKENS_DIA = int(os.getenv("NOTICIAS_TOKENS_DIA", "5000000"))  # só trava contra defeito em loop
MODELO_TRIAGEM = os.getenv("NOTICIAS_MODELO_TRIAGEM", "claude-haiku-5-5")
MODELO_TEXTO = os.getenv("NOTICIAS_MODELO_TEXTO", "claude-sonnet-5-5")
HORA_SILENCIO = (int(os.getenv("NOTICIAS_SILENCIO_INICIO", "23")), int(os.getenv("NOTICIAS_SILENCIO_FIM", "7")))


def chave_ia() -> str:
    return os.getenv("ANTHROPIC_API_KEY", "").strip()


# ------------------------------------------------------------------ feeds --
def normalizar_url(u: str) -> str:
    p = urlparse(u.strip())
    q = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(("utm_", "fbclid", "gclid", "cmpid"))]
    return urlunparse((p.scheme, p.netloc.lower(), p.path.rstrip("/"), "", urlencode(q), ""))


def hash_url(u: str) -> str:
    return hashlib.sha1(normalizar_url(u).encode()).hexdigest()


def _limpa(txt: str, n: int = 400) -> str:
    t = re.sub(r"<[^>]+>", " ", txt or "")
    t = html.unescape(re.sub(r"\s+", " ", t)).strip()
    return t[:n]


def _imagem_item(e: Any) -> str | None:
    for m in (e.get("media_content") or []) + (e.get("media_thumbnail") or []):
        if m.get("url"):
            return m["url"]
    for enc in e.get("enclosures") or []:
        if (enc.get("type") or "").startswith("image") and enc.get("href"):
            return enc["href"]
    return None


async def _ler_feed(c: httpx.AsyncClient, nome: str, url: str, desde: datetime) -> tuple[str, str, list[dict], str | None]:
    try:
        r = await c.get(url, headers={"User-Agent": UA}, timeout=15, follow_redirects=True)
        r.raise_for_status()
        f = feedparser.parse(r.content)
        if f.bozo and not f.entries:
            return nome, url, [], "feed inválido"
        itens = []
        for e in f.entries[:40]:
            link = e.get("link")
            titulo = _limpa(e.get("title", ""), 220)
            if not link or not titulo:
                continue
            quando = None
            for k in ("published_parsed", "updated_parsed"):
                if e.get(k):
                    quando = datetime(*e[k][:6], tzinfo=timezone.utc)
                    break
            if quando and quando < desde:
                continue
            itens.append({
                "fonte": nome, "url": link, "titulo": titulo,
                "resumo": _limpa(e.get("summary", ""), 400),
                "publicado": quando.isoformat() if quando else None,
                "imagem": _imagem_item(e),
            })
        return nome, url, itens, None
    except Exception as ex:
        return nome, url, [], str(ex)[:160]


async def ler_feeds(horas: int = 6) -> tuple[list[dict], dict[str, str | None]]:
    desde = datetime.now(timezone.utc) - timedelta(hours=horas)
    async with httpx.AsyncClient() as c:
        res = await asyncio.gather(*(_ler_feed(c, n, u, desde) for n, u in feeds()))
    itens, saude = [], {}
    for nome, url, its, erro in res:
        saude[url] = erro
        itens += its
    return itens, saude


# ------------------------------------------------------------- matéria --
async def ler_materia(url: str) -> dict[str, str]:
    """Texto principal + og:image da matéria (melhor esforço)."""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent": UA}) as c:
            r = await c.get(url)
        if r.status_code >= 400:
            return {}
        doc = r.text[:2_000_000]
    except Exception:
        return {}
    og = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', doc, re.I) or \
        re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image', doc, re.I)
    paras = re.findall(r"<p[^>]*>(.*?)</p>", doc, re.S | re.I)
    texto = " ".join(_limpa(p, 2000) for p in paras if len(_limpa(p, 2000)) > 60)
    return {"texto": texto[:5000], "og_image": html.unescape(og.group(1)) if og else ""}


# -------------------------------------------------------------- foto ----
def _sem_acento(s: str) -> str:
    import unicodedata
    return "".join(ch for ch in unicodedata.normalize("NFD", s) if unicodedata.category(ch) != "Mn").lower()


async def foto_wikimedia(pessoa: str) -> tuple[str, str] | None:
    """Procura foto livre (Wikimedia Commons) da pessoa. Devolve (url, crédito)."""
    if not pessoa:
        return None
    sobrenome = _sem_acento(pessoa.split()[-1])
    params = {
        "action": "query", "format": "json", "generator": "search", "gsrnamespace": "6",
        "gsrsearch": f"filetype:bitmap {pessoa}", "gsrlimit": "8",
        "prop": "imageinfo", "iiprop": "url|extmetadata|size|mime", "iiurlwidth": "1600",
    }
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "DKCSocial/1.0 (contato@dkc.social)"}) as c:
            r = await c.get("https://commons.wikimedia.org/w/api.php", params=params)
        pages = (r.json().get("query") or {}).get("pages") or {}
    except Exception:
        return None
    candidatos = sorted(pages.values(), key=lambda p: p.get("index", 99))
    for p in candidatos:
        titulo = _sem_acento(p.get("title", ""))
        info = (p.get("imageinfo") or [{}])[0]
        if sobrenome not in titulo or info.get("mime") not in ("image/jpeg", "image/png"):
            continue
        if (info.get("width") or 0) < 700:
            continue
        meta = info.get("extmetadata") or {}
        autor = _limpa((meta.get("Artist") or {}).get("value", ""), 60)
        lic = _limpa((meta.get("LicenseShortName") or {}).get("value", ""), 30)
        credito = "Foto: " + " · ".join(x for x in (autor, f"Wikimedia Commons ({lic})" if lic else "Wikimedia Commons") if x)
        return info.get("thumburl") or info.get("url"), credito
    return None


# ----------------------------------------------------------------- IA ----
class LimiteIA(RuntimeError):
    pass


async def claude(modelo: str, sistema: str, usuario: str, max_tokens: int = 2000) -> tuple[Any, dict]:
    k = chave_ia()
    if not k:
        raise RuntimeError("ANTHROPIC_API_KEY não configurada")
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(
            os.getenv("ANTHROPIC_BASE", "https://api.anthropic.com") + "/v1/messages",
            headers={"x-api-key": k, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            # instruções fixas em cache por 1h: a triagem roda a cada 10 min com o mesmo texto (leitura sai ~90% mais barata)
            json={"model": modelo, "max_tokens": max_tokens,
                  "system": [{"type": "text", "text": sistema, "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
                  "messages": [{"role": "user", "content": usuario}]},
        )
    if r.status_code >= 400:
        raise RuntimeError(f"Anthropic {r.status_code}: {r.text[:200]}")
    d = r.json()
    txt = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
    uso = d.get("usage") or {}
    if d.get("stop_reason") == "max_tokens":
        log.warning("[IA] resposta cortada no limite de %s tokens (%s) — aproveitando o que veio completo", max_tokens, modelo)
    try:
        dados = _json(txt)
    except ValueError:
        log.warning("[IA] JSON inválido (%s, parada=%s). Fim da resposta: %r", modelo, d.get("stop_reason"), txt[-300:])
        raise
    return dados, {"in": uso.get("input_tokens", 0), "out": uso.get("output_tokens", 0)}


def _json(txt: str) -> Any:
    txt = txt.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", txt, re.S)
    if m:
        txt = m.group(1).strip()
    pares = sorted((("[", "]"), ("{", "}")), key=lambda par: (txt.find(par[0]) == -1, txt.find(par[0])))
    for abre, fecha in pares:  # o que abrir primeiro manda (objeto com lista dentro continua objeto)
        i, j = txt.find(abre), txt.rfind(fecha)
        if i != -1 and j > i:
            try:
                return json.loads(txt[i:j + 1])
            except json.JSONDecodeError:
                continue
    # Lista cortada ou com um item quebrado: aproveita cada objeto que veio inteiro, em vez de perder o lote todo.
    if txt.lstrip().startswith("["):
        dec, achados, pos = json.JSONDecoder(), [], txt.find("{")
        while pos != -1:
            try:
                obj, fim = dec.raw_decode(txt, pos)
                if isinstance(obj, dict):
                    achados.append(obj)
                pos = txt.find("{", fim)
            except json.JSONDecodeError:
                pos = txt.find("{", pos + 1)
        if achados:
            return achados
    raise ValueError("a IA não devolveu JSON válido")


PUBLICO = (
    "Público: perfil @seutipster.app no Instagram, torcedores brasileiros de 18 a 40 anos que acompanham "
    "Brasileirão, Libertadores, Copa do Brasil, Sul-Americana e as grandes ligas europeias "
    "(Champions, Premier, LaLiga, Serie A, Bundesliga, Ligue 1) e que gostam de apostar. "
    "Referências de tom: TNT Sports Brasil e CazéTV (direto, informal, sem ser bobo)."
)

SISTEMA_TRIAGEM = PUBLICO + """

Você é o editor de pauta. Recebe manchetes novas de vários portais e uma lista de assuntos recentes.
Para CADA manchete devolva um objeto JSON com:
- "item": o id da manchete
- "assunto_existente": id do assunto recente que é a MESMA notícia (mesmo fato), ou null
- "assunto": título curto e factual em português (máx. 90 caracteres) do fato, ex.: "Getafe anuncia Carvajal até 2027"
- "nota": 0 a 10, interesse para ESSE público AGORA
- "tipo": mercado | tecnico | lesao | resultado | polemica | recorde | selecao | bastidor | outro
- "motivo": até 12 palavras

PRIORIDADES DA CASA (o dono do perfil pediu, valem mais que o resto):
- Qualquer notícia relevante sobre Neymar ou Cristiano Ronaldo: nota mínima 7.
- Clubes brasileiros da Série A e da Série B (contratação, saída, técnico, lesão, bastidor, diretoria, finanças, polêmica): nota mínima 6, mesmo que só um portal tenha dado.
- Regulamentação e mercado de apostas no Brasil (bets, Secretaria de Prêmios e Apostas, Ministério da Fazenda, CPI, patrocínio de bets, proibições, impostos, licenças): nota mínima 7.
- Clubes da Premier League, LaLiga e dos grandes da Champions (Real, Barça, Atlético, City, Liverpool, Arsenal, Chelsea, United, Tottenham, Newcastle, Bayern, Dortmund, PSG, Inter, Milan, Juventus, Napoli...): transferência, negociação avançada, rumor de veículo conhecido, técnico, lesão, crise, bastidor bombástico: nota mínima 6; bomba de clube grande: 8 a 10.
- "Here we go" ou informação atribuída a Fabrizio Romano sobre clube grande: nota mínima 8 (é transferência praticamente fechada).
- O objetivo do perfil é dar a notícia ANTES dos outros canais. Rumor forte de veículo conhecido entra (vai como "segundo X"); só descarte fofoca sem fonte ou de site obscuro.
- Uma notícia relevante dada por um só portal vale tanto quanto uma repetida em vários. Não rebaixe por exclusividade.
- SÓ NOTÍCIA RELEVANTE (regra do dono, 17/10/2026): as prioridades acima dizem QUAIS assuntos interessam, mas não salvam notícia fraca.
  Notícia fraca de clube grande ou de estrela (treino, declaração protocolar, aniversário, curiosidade, "pode", "estuda",
  "monitora", "interesse" sem negociação real, especulação sem fonte forte, repercussão requentada) fica entre 3 e 5.

Critérios de nota:
- 9–10: bomba que todo torcedor vai comentar hoje (contratação/saída de craque ou de clube grande, demissão de técnico de grande, lesão grave de estrela antes de jogo decisivo, escândalo).
- 7–8: notícia forte de clube grande brasileiro ou europeu, escalação/desfalque relevante para jogo grande do dia, recorde, polêmica com repercussão.
- 4–6: interesse médio ou nicho.
- 0–3: fofoca fraca, clube pequeno sem contexto, opinião/coluna, notícia velha, conteúdo patrocinado, outros esportes.
- Placar/resultado puro de jogo: no máximo 6 (já existe um robô só para resultados), exceto feito histórico.
- Mesmo fato já em "assuntos recentes": aponte em assunto_existente.
- Se duas manchetes DESTE lote forem o mesmo fato (e ele não estiver nos recentes), escreva exatamente o mesmo texto em "assunto" nas duas.

Responda SOMENTE com um array JSON."""

SISTEMA_REDACAO = PUBLICO + """

Você escreve um post de notícia para o Instagram (card + legenda) usando SOMENTE os fatos das fontes enviadas.
Nunca invente número, data, valor, declaração ou nome. Se a fonte trata como rumor ou "segundo X", diga isso.

Devolva SOMENTE um objeto JSON:
{
 "tag": rótulo curto em caixa normal (ex.: "Mercado da bola", "Urgente", "Lesão", "Demitido", "Polêmica", "Recorde", "Bastidores"; se for rumor/negociação não confirmada use "Rumor"),
 "tag2": competição ou liga (ex.: "Brasileirão", "LaLiga", "Premier") ou null,
 "titulo_card": manchete do card, até 70 caracteres, forte e factual; marque 1 a 3 palavras-chave entre *asteriscos* para destaque,
 "linha_fina": complemento opcional até 80 caracteres ou null (use só se não houver fatos numéricos),
 "fatos": de 0 a 4 itens {"n": número/valor curto (até 7 caracteres), "l": rótulo até 22 caracteres} TIRADOS DAS FONTES; se não houver números concretos, devolva [],
 "legenda": a legenda completa (regras abaixo),
 "pessoa_foto": nome completo do jogador/técnico protagonista para buscar foto, ou null,
 "clube_foto": clube atual dessa pessoa, ou null,
 "clube": clube protagonista da notícia (nome curto, ex.: "Flamengo", "Real Madrid"), ou null,
 "jogo": se a notícia é sobre UM jogo específico: {"casa": time mandante, "fora": time visitante, "quando": ex. "Hoje · 21h30" (só se a fonte disser)}; senão null,
 "editoria": "apostas" (regulação/mercado de apostas) | "mercado" (transferências) | "jogo" | "lesao" | "tecnico" | "selecao" | "polemica" | "outro",
 "palavra": UMA palavra curta (até 10 letras, sem acento se possível) para o grafismo gigante do card: sobrenome do protagonista, nome do clube ou o tema (ex.: "BETS", "MERCADO"); ou null,
 "fonte_nome": nome do veículo que deu a informação principal,
 "urgente": true só se for bomba de nota 9–10
}

Regras da legenda (padrão da casa):
- Português do Brasil, tom de perfil esportivo jovem. Frases curtas, quebras de linha entre blocos.
- A PRIMEIRA linha é o gancho e precisa caber em 125 caracteres: o fato principal com o detalhe mais forte (número, nome, valor).
- Depois 1 a 3 blocos curtos de contexto com os fatos das fontes. Cite a fonte quando for informação de um veículo ("segundo o AS").
- Termine com uma pergunta concreta para o torcedor responder (nada de "o que você acha?" genérico).
- 0 a 3 emojis no total, nunca um em cada linha.
- No máximo 1 travessão (—) na legenda inteira; prefira vírgula, dois-pontos ou quebra de linha.
- Proibido: "não é só X, é Y", "game-changer", "virada de chave", "nível", "jornada", "cenário", "fundamental", "crucial", "vale ressaltar", "sem dúvidas", "confira", "fique por dentro", "link na bio".
- Nada de aposta/odds/casa de aposta na legenda.
- Última linha: 3 a 5 hashtags, misturando 2–3 específicas (clube/jogador) e 1–2 do tema (#MercadoDaBola, #Brasileirão...). Sem #futebol genérico se já houver 4.
- Tamanho: 300 a 900 caracteres."""


# Reforço por palavra-chave (garante as prioridades mesmo se a IA errar a nota)
_CLUBES_BR = (
    "flamengo", "palmeiras", "corinthians", "são paulo", "sao paulo", "santos", "vasco", "fluminense", "botafogo",
    "grêmio", "gremio", "internacional", "inter de porto alegre", "atlético-mg", "atletico-mg", "atlético mineiro",
    "galo", "cruzeiro", "bahia", "vitória", "vitoria", "fortaleza", "ceará", "ceara", "sport recife", "athletico",
    "coritiba", "bragantino", "juventude", "mirassol", "remo", "paysandu", "goiás", "goias", "atlético-go",
    "américa-mg", "america-mg", "chapecoense", "avaí", "avai", "criciúma", "criciuma", "cuiabá", "cuiaba",
    "novorizontino", "operário", "operario", "ponte preta", "guarani", "náutico", "nautico", "vila nova", "crb",
    "amazonas fc", "botafogo-sp", "ferroviária", "ferroviaria", "athletic club", "volta redonda", "brasileirão",
    "série a", "série b", "serie b",
)
_ESTRELAS = ("neymar", "cristiano ronaldo", "cr7")
_CLUBES_EU = (
    "real madrid", "barcelona", "barça", "atlético de madrid", "atletico madrid", "manchester city", "man city",
    "liverpool", "arsenal", "chelsea", "manchester united", "man united", "man utd", "tottenham", "newcastle",
    "aston villa", "west ham", "everton", "brighton", "bayern", "dortmund", "leverkusen", "psg", "paris saint",
    "inter de milão", "inter milan", "milan", "juventus", "napoli", "roma", "sevilla", "betis", "villarreal",
    "athletic bilbao", "real sociedad", "valencia", "girona", "premier league", "laliga", "la liga", "champions",
)
_MERCADO = ("transfer", "contrat", "assina", "acerto", "negocia", "proposta", "oferta", "rumor", "sign", "deal",
            "bid", "here we go", "fichaje", "demit", "sacked", "técnico", "treinador", "manager", "lesão", "injur",
            "crise", "polêmica", "bastidor")
_APOSTAS = ("bets", " bet ", "apostas", "casas de aposta", "secretaria de prêmios", "prêmios e apostas", "spa/mf",
            "regulamentação", "regulamentacao", "jogo responsável", "cpi das bets", "loterias", "igaming")


def reforco(texto: str, nota: float, tipo: str | None) -> float:
    t = f" {texto.lower()} "
    if any(k in t for k in _ESTRELAS) and nota >= 5:  # estrela não salva notícia fraca
        nota = max(nota, 7.0)
    # "Here we go" do Fabrizio Romano = transferência fechada, é o furo que todo mundo vai repetir
    if "here we go" in t or "fabrizio romano" in t or "romano" in t and any(k in t for k in ("transfer", "deal", "contrat")):
        nota = max(nota, 8.0)
    if any(k in t for k in _APOSTAS) and any(k in t for k in ("bet", "aposta", "loteria", "igaming")) and nota >= 5:
        nota = max(nota, 7.0)
    if tipo != "resultado" and nota >= 3 and any(k in t for k in _CLUBES_BR):
        nota = max(nota, 6.0)
    if tipo != "resultado" and nota >= 3 and any(k in t for k in _CLUBES_EU) and any(k in t for k in _MERCADO):
        nota = max(nota, 6.0)
    return nota


def prompt_triagem(itens: list[dict], recentes: list[dict]) -> str:
    return json.dumps({
        "assuntos_recentes": [{"id": a["id"], "assunto": a["titulo"]} for a in recentes],
        "manchetes": [{"item": i["id"], "fonte": i["fonte"], "titulo": i["titulo"], "resumo": i["resumo"][:300]} for i in itens],
    }, ensure_ascii=False)


def prompt_redacao(assunto: str, fontes: list[dict]) -> str:
    return json.dumps({"assunto": assunto, "fontes": [
        {"veiculo": f["fonte"], "titulo": f["titulo"], "resumo": f.get("resumo", ""), "texto": f.get("texto", "")[:3500]}
        for f in fontes
    ]}, ensure_ascii=False)


VETADAS = ("game-changer", "virada de chave", "vale ressaltar", "fique por dentro", "link na bio", "não é só")


def checar_legenda(leg: str) -> list[str]:
    """Checagem final (humanizador): devolve problemas encontrados."""
    probs = []
    primeira = leg.strip().split("\n", 1)[0]
    if len(primeira) > 125:
        probs.append("gancho com mais de 125 caracteres")
    if leg.count("—") > 1:
        probs.append("travessões demais")
    tags = re.findall(r"#\w+", leg)
    if not 3 <= len(tags) <= 5:
        probs.append(f"{len(tags)} hashtags (o padrão é 3 a 5)")
    low = leg.lower()
    for v in VETADAS:
        if v in low:
            probs.append(f"expressão vetada: {v}")
    if len(leg) > 2200:
        probs.append("legenda acima de 2.200 caracteres")
    return probs


def corrigir_legenda(leg: str) -> str:
    """Ajustes mecânicos seguros quando a IA escorrega."""
    partes = leg.split("—")
    if len(partes) > 2:  # mantém o primeiro travessão, troca os outros por vírgula
        leg = partes[0] + "—" + ",".join(partes[1:])
        leg = re.sub(r"\s+,", ",", leg)
    tags = re.findall(r"#\w+", leg)
    if len(tags) > 5:
        for t in tags[5:]:
            leg = leg.replace(" " + t, "").replace(t, "")
    return leg.strip()[:2200]
