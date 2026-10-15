"""DKC Social — fila de aprovação de posts para Instagram.

Fluxo: robô (ou upload manual) cria um post PENDENTE → push no celular/PC →
Nog aprova, edita ou recusa → aprovado vai para o Publora (agora ou agendado).

Stack: FastAPI + Postgres (asyncpg) + PWA em JS puro (static/).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import asyncpg
import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import pauta, publora, webpush
from .robos import noticias as robo_noticias
from .robos import rodada as robo_rodada
from .robos import resultados as robo_resultados
from .cards import foto_jogador, fotos
from .cards.render import MODELOS, renderizador

log = logging.getLogger("dkc")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres@127.0.0.1:5433/dkc")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
ROBO_KEY = os.getenv("ROBO_KEY", "")  # chave dos robôs que criam posts
def _env_qg_key() -> str:
    """Lê a chave do QG tolerando nome com espaço/maiúscula diferente (comum ao digitar no celular)."""
    for nome, valor in os.environ.items():
        if nome.strip().upper().replace(" ", "") == "QG_KEY":
            return valor.strip().strip("\"'").strip()
    return ""


QG_KEY = _env_qg_key()  # chave do QG do Nog (só leitura do status)
# Reserva para quando a variável QG_KEY não chega ao servidor no Railway: só a impressão SHA-256 da chave
# (a chave em si nunca fica no código; com a variável configurada, ela tem prioridade).
QG_KEY_SHA256 = os.getenv("QG_KEY_SHA256", "ea5d51ff6f31f4ab19fcce2bd7ad4abb1d2fa36c3b69b4cd834aadf4d1c08145")
SESSAO_DIAS = 60

STATUS_VALIDOS = ("pendente", "publicando", "agendado", "publicado", "recusado", "erro")
LINHAS = ("noticia", "temporada", "rodada", "resultado", "jogador", "manual")
MIME_OK = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "video/mp4": "mp4"}

pool: asyncpg.Pool | None = None


# ---------------------------------------------------------------- banco ----

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
  chave TEXT PRIMARY KEY, valor TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessoes (
  token TEXT PRIMARY KEY,
  criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
  expira_em TIMESTAMPTZ NOT NULL,
  user_agent TEXT
);
CREATE TABLE IF NOT EXISTS login_falhas (
  ip TEXT NOT NULL, em TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS perfis (
  id SERIAL PRIMARY KEY,
  nome TEXT NOT NULL,
  handle TEXT NOT NULL,
  publora_platform_id TEXT UNIQUE,
  ativo BOOLEAN NOT NULL DEFAULT TRUE,
  marca JSONB NOT NULL DEFAULT '{}'::jsonb,
  criado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS posts (
  id UUID PRIMARY KEY,
  perfil_id INT REFERENCES perfis(id),
  linha TEXT NOT NULL DEFAULT 'manual',
  titulo TEXT NOT NULL DEFAULT '',
  legenda TEXT NOT NULL DEFAULT '',
  fonte TEXT NOT NULL DEFAULT '',
  origem TEXT NOT NULL DEFAULT 'manual',
  status TEXT NOT NULL DEFAULT 'pendente',
  sugerido_para TIMESTAMPTZ,
  agendar_para TIMESTAMPTZ,
  publora_group_id TEXT,
  erro TEXT,
  motivo_recusa TEXT,
  criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
  decidido_em TIMESTAMPTZ,
  publicado_em TIMESTAMPTZ
);
ALTER TABLE posts ADD COLUMN IF NOT EXISTS fonte_urls JSONB NOT NULL DEFAULT '[]'::jsonb;
CREATE INDEX IF NOT EXISTS posts_status_idx ON posts(status, criado_em DESC);
CREATE TABLE IF NOT EXISTS midias (
  id UUID PRIMARY KEY,
  post_id UUID NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  ordem INT NOT NULL DEFAULT 0,
  mime TEXT NOT NULL,
  dados BYTEA NOT NULL,
  token TEXT NOT NULL UNIQUE,
  criado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS midias_post_idx ON midias(post_id, ordem);
CREATE TABLE IF NOT EXISTS push_subs (
  endpoint TEXT PRIMARY KEY,
  p256dh TEXT NOT NULL,
  auth TEXT NOT NULL,
  user_agent TEXT,
  criado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS pauta_importada (
  path TEXT PRIMARY KEY,
  blob_sha TEXT NOT NULL,
  post_id UUID,
  status TEXT NOT NULL,
  erro TEXT,
  tentativas INT NOT NULL DEFAULT 0,
  em TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE pauta_importada ADD COLUMN IF NOT EXISTS tentativas INT NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS assuntos (
  id SERIAL PRIMARY KEY,
  titulo TEXT NOT NULL,
  tipo TEXT,
  nota REAL NOT NULL DEFAULT 0,
  motivo TEXT,
  status TEXT NOT NULL DEFAULT 'novo',
  post_id UUID,
  erro TEXT,
  criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
  postado_em TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS assuntos_idx ON assuntos(status, criado_em DESC);
CREATE TABLE IF NOT EXISTS noticias_itens (
  url_hash TEXT PRIMARY KEY,
  url TEXT NOT NULL,
  fonte TEXT,
  titulo TEXT,
  resumo TEXT,
  imagem TEXT,
  assunto_id INT REFERENCES assuntos(id),
  visto_em TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS noticias_itens_assunto ON noticias_itens(assunto_id);
CREATE TABLE IF NOT EXISTS resultados (
  fixture_id BIGINT PRIMARY KEY,
  status TEXT NOT NULL,
  post_id UUID,
  erro TEXT,
  tentativas INT NOT NULL DEFAULT 0,
  em TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS eventos (
  id BIGSERIAL PRIMARY KEY,
  post_id UUID,
  tipo TEXT NOT NULL,
  detalhe TEXT,
  em TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


async def cfg_get(chave: str) -> Optional[str]:
    return await pool.fetchval("SELECT valor FROM config WHERE chave=$1", chave)


async def cfg_set(chave: str, valor: str) -> None:
    await pool.execute(
        "INSERT INTO config(chave,valor) VALUES($1,$2) ON CONFLICT (chave) DO UPDATE SET valor=EXCLUDED.valor",
        chave, valor,
    )


async def vapid() -> tuple[str, str]:
    """Chaves VAPID: env tem prioridade; senão gera uma vez e guarda no banco."""
    priv, pub = os.getenv("VAPID_PRIVATE_PEM"), os.getenv("VAPID_PUBLIC")
    if priv and pub:
        return priv.replace("\\n", "\n"), pub
    priv, pub = await cfg_get("vapid_priv"), await cfg_get("vapid_pub")
    if not (priv and pub):
        priv, pub = webpush.gerar_vapid()
        await cfg_set("vapid_priv", priv)
        await cfg_set("vapid_pub", pub)
    return priv, pub


async def evento(post_id: Optional[uuid.UUID], tipo: str, detalhe: str = "") -> None:
    try:
        await pool.execute(
            "INSERT INTO eventos(post_id,tipo,detalhe) VALUES($1,$2,$3)", post_id, tipo, detalhe[:2000]
        )
    except Exception:
        log.exception("[EVENTO] falha ao gravar")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=8)
    async with pool.acquire() as c:
        await c.execute(SCHEMA)
    await vapid()
    if not ADMIN_PASSWORD:
        log.warning("[BOOT] ADMIN_PASSWORD não definida — login bloqueado até configurar")
    tasks = [asyncio.create_task(job_status_publora()), asyncio.create_task(job_pauta()), asyncio.create_task(job_rodada()),
             asyncio.create_task(job_noticias()), asyncio.create_task(job_resultados())]
    yield
    for t in tasks:
        t.cancel()
    try:
        await renderizador.fechar()
    except Exception:
        pass
    await pool.close()


app = FastAPI(title="DKC Social", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def seguranca(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    if not request.url.path.startswith("/m/"):
        resp.headers["X-Frame-Options"] = "DENY"
    return resp


# ------------------------------------------------------------- auth ----

def _ip(req: Request) -> str:
    fwd = req.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (req.client.host if req.client else "?")


async def exige_login(authorization: str = Header(default="")) -> str:
    tok = authorization.removeprefix("Bearer ").strip()
    if not tok:
        raise HTTPException(401, "faça login")
    ok = await pool.fetchval(
        "SELECT 1 FROM sessoes WHERE token=$1 AND expira_em>now()", hashlib.sha256(tok.encode()).hexdigest()
    )
    if not ok:
        raise HTTPException(401, "sessão expirada")
    return tok


async def exige_robo(x_robo_key: str = Header(default="")) -> None:
    if not ROBO_KEY or not hmac.compare_digest(x_robo_key, ROBO_KEY):
        raise HTTPException(401, "chave de robô inválida")


class LoginIn(BaseModel):
    senha: str


@app.post("/api/login")
async def login(body: LoginIn, request: Request):
    ip = _ip(request)
    falhas = await pool.fetchval(
        "SELECT count(*) FROM login_falhas WHERE ip=$1 AND em>now()-interval '15 minutes'", ip
    )
    if falhas >= 8:
        raise HTTPException(429, "muitas tentativas, espere 15 minutos")
    if not ADMIN_PASSWORD or not hmac.compare_digest(body.senha, ADMIN_PASSWORD):
        await pool.execute("INSERT INTO login_falhas(ip) VALUES($1)", ip)
        raise HTTPException(401, "senha incorreta")
    tok = secrets.token_urlsafe(32)
    await pool.execute(
        "INSERT INTO sessoes(token,expira_em,user_agent) VALUES($1,$2,$3)",
        hashlib.sha256(tok.encode()).hexdigest(),
        datetime.now(timezone.utc) + timedelta(days=SESSAO_DIAS),
        request.headers.get("user-agent", "")[:300],
    )
    await pool.execute("DELETE FROM login_falhas WHERE ip=$1", ip)
    return {"token": tok}


@app.post("/api/logout")
async def logout(tok: str = Depends(exige_login)):
    await pool.execute("DELETE FROM sessoes WHERE token=$1", hashlib.sha256(tok.encode()).hexdigest())
    return {"ok": True}


# ----------------------------------------------------------- helpers ----

def base_publica(request: Optional[Request] = None) -> str:
    b = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    if b:
        return b
    dom = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
    if dom:
        return f"https://{dom}"
    if request is not None:
        return str(request.base_url).rstrip("/")
    return ""


def _iso(d: Optional[datetime]) -> Optional[str]:
    return d.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if d else None


async def post_json(row: asyncpg.Record, base: str) -> dict[str, Any]:
    midias = await pool.fetch(
        "SELECT token,mime FROM midias WHERE post_id=$1 ORDER BY ordem", row["id"]
    )
    return {
        "id": str(row["id"]),
        "perfil_id": row["perfil_id"],
        "perfil": row.get("perfil_handle"),
        "linha": row["linha"],
        "titulo": row["titulo"],
        "legenda": row["legenda"],
        "fonte": row["fonte"],
        "fonte_urls": json.loads(row["fonte_urls"]) if isinstance(row["fonte_urls"], str) else (row["fonte_urls"] or []),
        "origem": row["origem"],
        "status": row["status"],
        "erro": row["erro"],
        "motivo_recusa": row["motivo_recusa"],
        "sugerido_para": _iso(row["sugerido_para"]),
        "agendar_para": _iso(row["agendar_para"]),
        "criado_em": _iso(row["criado_em"]),
        "decidido_em": _iso(row["decidido_em"]),
        "publicado_em": _iso(row["publicado_em"]),
        "midias": [
            {"url": f"/m/{m['token']}.{MIME_OK.get(m['mime'], 'bin')}", "mime": m["mime"]}
            for m in midias
        ],
    }


POST_SELECT = """
SELECT p.*, pf.handle AS perfil_handle
FROM posts p LEFT JOIN perfis pf ON pf.id=p.perfil_id
"""


async def perfil_padrao() -> Optional[int]:
    return await pool.fetchval("SELECT id FROM perfis WHERE ativo ORDER BY id LIMIT 1")


async def criar_post(
    *, perfil_id: Optional[int], linha: str, titulo: str, legenda: str, fonte: str,
    origem: str, midias: list[tuple[str, bytes]], sugerido_para: Optional[datetime] = None,
    fonte_urls: Optional[list] = None, aviso: Optional[str] = None, sem_auto: bool = False,
) -> uuid.UUID:
    if linha not in LINHAS:
        linha = "manual"
    if not midias:
        raise HTTPException(400, "o post precisa de pelo menos uma imagem ou vídeo")
    if len(midias) > 10:
        raise HTTPException(400, "máximo de 10 mídias por post (carrossel)")
    tipos = {m[0].split("/")[0] for m in midias}
    if len(tipos) > 1:
        raise HTTPException(400, "carrossel não aceita misturar foto e vídeo")
    for mime, dados in midias:
        if mime not in MIME_OK:
            raise HTTPException(400, f"formato não aceito: {mime}")
        lim = 150 if mime.startswith("video/") else 25
        if len(dados) > lim * 1024 * 1024:
            raise HTTPException(400, f"arquivo acima de {lim} MB")
    if len(legenda) > 2200:
        raise HTTPException(400, "legenda acima de 2.200 caracteres (limite do Instagram)")
    links = [u for u in (fonte_urls or []) if isinstance(u, str) and u.startswith(("https://", "http://"))][:5]
    pid = uuid.uuid4()
    perfil_id = perfil_id or await perfil_padrao()
    async with pool.acquire() as c, c.transaction():
        await c.execute(
            "INSERT INTO posts(id,perfil_id,linha,titulo,legenda,fonte,origem,sugerido_para,fonte_urls) "
            "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)",
            pid, perfil_id, linha, titulo[:200], legenda, fonte[:300], origem[:40], sugerido_para, json.dumps(links),
        )
        for i, (mime, dados) in enumerate(midias):
            await c.execute(
                "INSERT INTO midias(id,post_id,ordem,mime,dados,token) VALUES($1,$2,$3,$4,$5,$6)",
                uuid.uuid4(), pid, i, mime, dados, secrets.token_urlsafe(18),
            )
    await evento(pid, "criado", origem)
    # regra: só RESULTADO de jogo (vindo de robô/Claude) pode sair sem aprovação
    if linha == "resultado" and sem_auto:
        # resultado incompleto (sem foto boa): não sai sozinho, vai para a fila
        asyncio.create_task(avisar_novo_post(pid, titulo or legenda[:80], "⚠️ Resultado sem foto: confira antes de publicar"))
        return pid
    if linha == "resultado" and origem != "manual" and await auto_resultado_ligado():
        res = await _aprovar_um(pid, None, base_publica())
        await evento(pid, "auto_aprovado", json.dumps(res))
        asyncio.create_task(enviar_push({
            "title": "✅ Resultado publicado", "body": titulo or legenda[:80],
            "url": f"/?post={pid}", "tag": f"post-{pid}",
        }))
    else:
        asyncio.create_task(avisar_novo_post(pid, titulo or legenda[:80], aviso))
    return pid


async def auto_resultado_ligado() -> bool:
    return (await cfg_get("auto_resultado")) != "0"


class AutoIn(BaseModel):
    ligado: bool


@app.get("/api/config/auto-resultado")
async def get_auto(_=Depends(exige_login)):
    return {"ligado": await auto_resultado_ligado()}


@app.post("/api/config/auto-resultado")
async def set_auto(body: AutoIn, _=Depends(exige_login)):
    await cfg_set("auto_resultado", "1" if body.ligado else "0")
    return {"ligado": body.ligado}


# -------------------------------------------------------------- push ----

async def enviar_push(data: dict) -> dict[str, int]:
    priv, pub = await vapid()
    subs = await pool.fetch("SELECT endpoint,p256dh,auth FROM push_subs")
    ok = falha = removidas = 0
    async with httpx.AsyncClient(timeout=20) as c:
        for s in subs:
            try:
                url, headers, body = webpush.montar_requisicao(dict(s), data, priv, pub)
                r = await c.post(url, headers=headers, content=body)
                if r.status_code in (404, 410):
                    await pool.execute("DELETE FROM push_subs WHERE endpoint=$1", s["endpoint"])
                    removidas += 1
                elif r.status_code >= 300:
                    falha += 1
                    log.warning("[PUSH] %s %s", r.status_code, r.text[:200])
                else:
                    ok += 1
            except Exception:
                falha += 1
                log.exception("[PUSH] erro")
    return {"enviadas": ok, "falhas": falha, "removidas": removidas}


async def avisar_novo_post(pid: uuid.UUID, titulo: str, aviso: Optional[str] = None) -> None:
    pend = await pool.fetchval("SELECT count(*) FROM posts WHERE status='pendente'")
    extra = f" · {pend} na fila" if pend > 1 else ""
    await enviar_push({
        "title": aviso or "📲 Novo post para aprovar",
        "body": (titulo or "Post novo") + extra,
        "url": f"/?post={pid}",
        "tag": f"post-{pid}",
    })


class SubIn(BaseModel):
    endpoint: str
    keys: dict


@app.get("/api/push/chave")
async def push_chave():
    _, pub = await vapid()
    return {"publicKey": pub}


@app.post("/api/push/inscrever")
async def push_inscrever(sub: SubIn, request: Request, _=Depends(exige_login)):
    p256dh, auth = sub.keys.get("p256dh"), sub.keys.get("auth")
    if not (sub.endpoint.startswith("https://") and p256dh and auth):
        raise HTTPException(400, "inscrição inválida")
    await pool.execute(
        "INSERT INTO push_subs(endpoint,p256dh,auth,user_agent) VALUES($1,$2,$3,$4) "
        "ON CONFLICT (endpoint) DO UPDATE SET p256dh=EXCLUDED.p256dh, auth=EXCLUDED.auth",
        sub.endpoint, p256dh, auth, request.headers.get("user-agent", "")[:300],
    )
    return {"ok": True}


@app.post("/api/push/teste")
async def push_teste(_=Depends(exige_login)):
    return await enviar_push({"title": "🔔 DKC Social", "body": "Notificações funcionando.", "url": "/"})


# ------------------------------------------------------------- posts ----

@app.get("/api/posts")
async def listar(request: Request, status: str = "pendente", limite: int = 60, _=Depends(exige_login)):
    base = base_publica(request)
    filtros = [s for s in status.split(",") if s in STATUS_VALIDOS]
    if not filtros:
        raise HTTPException(400, "status inválido")
    ordem = "p.criado_em DESC" if filtros != ["agendado"] else "p.agendar_para ASC"
    rows = await pool.fetch(
        POST_SELECT + f" WHERE p.status = ANY($1::text[]) ORDER BY {ordem} LIMIT $2",
        filtros, max(1, min(limite, 200)),
    )
    return {"posts": [await post_json(r, base) for r in rows]}


@app.get("/api/resumo")
async def resumo(_=Depends(exige_login)):
    rows = await pool.fetch("SELECT status, count(*) n FROM posts GROUP BY status")
    hoje = await pool.fetchrow(
        "SELECT count(*) FILTER (WHERE status='publicado' AND publicado_em::date = (now() AT TIME ZONE 'America/Bahia')::date) pub, "
        "count(*) FILTER (WHERE status='recusado' AND decidido_em::date = (now() AT TIME ZONE 'America/Bahia')::date) rec "
        "FROM posts"
    )
    return {"por_status": {r["status"]: r["n"] for r in rows}, "publicados_hoje": hoje["pub"], "recusados_hoje": hoje["rec"]}


@app.get("/api/posts/{pid}")
async def ver(pid: uuid.UUID, request: Request, _=Depends(exige_login)):
    r = await pool.fetchrow(POST_SELECT + " WHERE p.id=$1", pid)
    if not r:
        raise HTTPException(404, "post não encontrado")
    return await post_json(r, base_publica(request))


@app.post("/api/posts")
async def novo_manual(
    legenda: str = Form(""),
    titulo: str = Form(""),
    linha: str = Form("manual"),
    perfil_id: Optional[int] = Form(None),
    arquivos: list[UploadFile] = File(...),
    _=Depends(exige_login),
):
    midias = [(a.content_type or "", await a.read()) for a in arquivos]
    pid = await criar_post(perfil_id=perfil_id, linha=linha, titulo=titulo, legenda=legenda,
                           fonte="", origem="manual", midias=midias)
    return {"id": str(pid)}


class MidiaB64(BaseModel):
    mime: str
    base64: str


class RoboPostIn(BaseModel):
    perfil_id: Optional[int] = None
    perfil_handle: Optional[str] = None
    linha: str = "noticia"
    titulo: str = ""
    legenda: str
    fonte: str = ""
    origem: str = "robo"
    sugerido_para: Optional[datetime] = None
    fonte_urls: list[str] = []
    midias: list[MidiaB64]


class SlideIn(BaseModel):
    modelo: str
    dados: dict = {}


class RoboCardsIn(BaseModel):
    perfil_id: Optional[int] = None
    perfil_handle: Optional[str] = None
    linha: str = "noticia"
    titulo: str = ""
    legenda: str
    fonte: str = ""
    origem: str = "robo"
    sugerido_para: Optional[datetime] = None
    fonte_urls: list[str] = []
    slides: list[SlideIn]


async def _perfil(perfil_id: Optional[int], handle: Optional[str]) -> tuple[Optional[int], Optional[str]]:
    if perfil_id:
        h = await pool.fetchval("SELECT handle FROM perfis WHERE id=$1", perfil_id)
        return perfil_id, h
    if handle:
        r = await pool.fetchrow("SELECT id, handle FROM perfis WHERE lower(handle)=lower($1)", handle.lstrip("@"))
        if r:
            return r["id"], r["handle"]
        return None, handle.lstrip("@")
    r = await pool.fetchrow("SELECT id, handle FROM perfis WHERE ativo ORDER BY id LIMIT 1")
    return (r["id"], r["handle"]) if r else (None, None)


async def renderizar_slides(slides: list[dict], handle: Optional[str], fonte: str,
                            foto_opcional: bool = False) -> tuple[list[tuple[str, bytes]], list[str]]:
    if not slides:
        raise HTTPException(400, "informe ao menos um slide")
    if len(slides) > 10:
        raise HTTPException(400, "máximo de 10 slides")
    midias, avisos = [], []
    for sl in slides:  # "jogador" no slide = buscar foto conferida (API-Football + busca + checagem por IA)
        dd = sl.get("dados") or {}
        jg = dd.get("jogador")
        if jg and not dd.get("foto"):
            try:
                fv = await foto_jogador.foto_verificada(jg.get("nome"), jg.get("clube"), jg.get("id"))
            except Exception:
                log.exception("[CARDS] foto do jogador")
                fv = None
            if fv:
                dd["foto"], dd["credito_foto"] = fv["url"], fv["credito"]
                if fv.get("aviso"):
                    avisos.append(fv["aviso"])
            else:
                avisos.append(f"sem foto conferida de {jg.get('nome')}")
            sl["dados"] = dd
    for i, sl in enumerate(slides, 1):
        if sl.get("modelo") not in MODELOS:
            raise HTTPException(400, f"slide {i}: modelo inválido ({', '.join(MODELOS)})")
        try:
            img, av = await renderizador.png(sl["modelo"], sl.get("dados") or {}, handle, fonte)
        except fotos.FotoErro as e:
            if not foto_opcional:
                raise HTTPException(422, f"slide {i}: {e}")
            dados = dict(sl.get("dados") or {}, foto=None, credito_foto=None)
            img, av = await renderizador.png(sl["modelo"], dados, handle, fonte)
        midias.append(("image/jpeg", img))
        avisos += [f"slide {i}: {a}" for a in av]
    return midias, avisos


def _links(body: dict) -> list:
    v = body.get("fonte_urls") or body.get("fonte_url") or []
    return [v] if isinstance(v, str) else list(v)


async def criar_post_de_slides(body: dict, origem: str) -> tuple[uuid.UUID, list[str]]:
    perfil_id, handle = await _perfil(body.get("perfil_id"), body.get("perfil_handle") or body.get("perfil"))
    midias, avisos = await renderizar_slides(body.get("slides") or [], handle, body.get("fonte", ""),
                                             foto_opcional=bool(body.get("foto_opcional")))
    sug = body.get("sugerido_para")
    if isinstance(sug, str) and sug:
        try:
            sug = datetime.fromisoformat(sug.replace("Z", "+00:00"))
        except ValueError:
            sug = None
    pid = await criar_post(perfil_id=perfil_id, linha=body.get("linha", "noticia"), titulo=body.get("titulo", ""),
                           legenda=body.get("legenda", ""), fonte=body.get("fonte", ""), origem=origem,
                           midias=midias, sugerido_para=sug or None, fonte_urls=_links(body), aviso=body.get("aviso"),
                           sem_auto=bool(body.get("sem_auto")) or any("sem foto" in a for a in avisos))
    if avisos:
        await evento(pid, "avisos", "; ".join(avisos))
    return pid, avisos


@app.post("/api/robo/cards")
async def novo_robo_cards(body: RoboCardsIn, _=Depends(exige_robo)):
    pid, avisos = await criar_post_de_slides(body.model_dump(mode="json"), body.origem or "robo")
    return {"id": str(pid), "avisos": avisos}


class PreviewIn(BaseModel):
    perfil_handle: Optional[str] = None
    fonte: str = ""
    slide: SlideIn


@app.post("/api/cards/preview")
async def preview_card(body: PreviewIn, _=Depends(exige_login)):
    if body.slide.modelo not in MODELOS:
        raise HTTPException(400, "modelo inválido")
    try:
        img, _av = await renderizador.png(body.slide.modelo, body.slide.dados, body.perfil_handle, body.fonte)
    except fotos.FotoErro as e:
        raise HTTPException(422, str(e))
    return Response(content=img, media_type="image/jpeg")


# ------------------------------------------------------------- pauta ----

_pauta_estado: dict[str, Any] = {"ultima": None, "erro": None}
_pauta_lock = asyncio.Lock()


async def importar_pauta() -> dict[str, Any]:
    """Confere o branch de pauta e importa arquivos novos. Seguro para rodar em paralelo."""
    async with _pauta_lock:
        novos = erros = 0
        async with httpx.AsyncClient(timeout=30) as c:
            sha = await pauta.commit_atual(c)
            _pauta_estado["ultima"] = _iso(datetime.now(timezone.utc))
            if not sha:
                _pauta_estado["erro"] = None
                return {"novos": 0, "erros": 0, "branch": "inexistente"}
            # erro passageiro (foto fora do ar etc.): tenta de novo a cada 10 min, até 3 vezes
            retentar = await pool.fetchval(
                "SELECT count(*) FROM pauta_importada WHERE status='erro' AND tentativas<3 AND em<now()-interval '10 minutes'"
            )
            if sha == await cfg_get("pauta_sha") and not retentar:
                _pauta_estado["erro"] = None
                return {"novos": 0, "erros": 0, "sha": sha[:7]}
            for arq in await pauta.arquivos(c, sha):
                ja = await pool.fetchrow("SELECT blob_sha, status FROM pauta_importada WHERE path=$1", arq["path"])
                # importa uma vez; só tenta de novo se deu erro e o arquivo foi corrigido
                if ja and ja["status"] == "ok":
                    continue
                if ja and ja["blob_sha"] == arq["sha"]:
                    pode = await pool.fetchval(
                        "SELECT tentativas<3 AND em<now()-interval '10 minutes' FROM pauta_importada WHERE path=$1", arq["path"])
                    if not pode:
                        continue
                try:
                    body = json.loads(await pauta.baixar(c, sha, arq["path"]))
                    if body.get("midias") and not body.get("slides"):
                        midias = []
                        for m in body["midias"][:10]:
                            dados = await pauta.baixar(c, sha, m)
                            ext = m.rsplit(".", 1)[-1].lower()
                            mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "mp4": "video/mp4"}.get(ext, "")
                            midias.append((mime, dados))
                        perfil_id, _h = await _perfil(None, body.get("perfil"))
                        pid = await criar_post(perfil_id=perfil_id, linha=body.get("linha", "manual"), titulo=body.get("titulo", ""),
                                               legenda=body.get("legenda", ""), fonte=body.get("fonte", ""), origem="claude", midias=midias,
                                               fonte_urls=_links(body))
                    else:
                        pid, _av = await criar_post_de_slides(body, "claude")
                    await pool.execute(
                        "INSERT INTO pauta_importada(path,blob_sha,post_id,status,erro) VALUES($1,$2,$3,'ok',NULL) "
                        "ON CONFLICT (path) DO UPDATE SET blob_sha=EXCLUDED.blob_sha, post_id=EXCLUDED.post_id, status='ok', erro=NULL, em=now()",
                        arq["path"], arq["sha"], pid,
                    )
                    novos += 1
                except Exception as e:
                    msg = e.detail if isinstance(e, HTTPException) else str(e)
                    log.exception("[PAUTA] falha em %s", arq["path"])
                    await pool.execute(
                        "INSERT INTO pauta_importada(path,blob_sha,status,erro,tentativas) VALUES($1,$2,'erro',$3,1) "
                        "ON CONFLICT (path) DO UPDATE SET tentativas=CASE WHEN pauta_importada.blob_sha=EXCLUDED.blob_sha "
                        "THEN pauta_importada.tentativas+1 ELSE 1 END, blob_sha=EXCLUDED.blob_sha, status='erro', erro=EXCLUDED.erro, em=now()",
                        arq["path"], arq["sha"], str(msg)[:500],
                    )
                    await enviar_push({"title": "⚠️ Pauta com erro", "body": f"{arq['path']}: {str(msg)[:100]}", "url": "/"})
                    erros += 1
            await cfg_set("pauta_sha", sha)
            _pauta_estado["erro"] = None
            return {"novos": novos, "erros": erros, "sha": sha[:7]}


async def job_pauta() -> None:
    await asyncio.sleep(15)
    while True:
        try:
            r = await importar_pauta()
            if r.get("novos") or r.get("erros"):
                log.info("[PAUTA] %s", r)
        except Exception as e:
            _pauta_estado["erro"] = str(e)[:300]
            log.warning("[PAUTA] %s", e)
        await asyncio.sleep(pauta.INTERVALO)


@app.post("/api/pauta/buscar")
async def pauta_buscar(_=Depends(exige_login)):
    try:
        return await importar_pauta()
    except Exception as e:
        raise HTTPException(502, f"não consegui ler a pauta no GitHub: {e}")


@app.get("/api/pauta/status")
async def pauta_status(_=Depends(exige_login)):
    rows = await pool.fetch("SELECT path,status,erro,em FROM pauta_importada ORDER BY em DESC LIMIT 10")
    return {
        "repo": pauta.REPO, "branch": pauta.BRANCH, "intervalo_s": pauta.INTERVALO,
        "ultima_checagem": _pauta_estado["ultima"], "erro": _pauta_estado["erro"],
        "importados": [{"path": r["path"], "status": r["status"], "erro": r["erro"], "em": _iso(r["em"])} for r in rows],
    }


# ------------------------------------------------------- robô: rodada ----

RODADA_HORA = int(os.getenv("RODADA_HORA", "7"))  # monta às 7h (sugestão de publicação 9h)


async def gerar_rodada(dia=None, forcar: bool = False) -> dict[str, Any]:
    dia = dia or datetime.now(robo_rodada.TZ).date()
    chave_dia = f"rodada_{dia.isoformat()}"
    if not forcar and await cfg_get(chave_dia):
        return {"ok": False, "motivo": "a rodada de hoje já foi gerada"}
    jogos = await robo_rodada.jogos_do_dia(dia)
    handle = await pool.fetchval("SELECT handle FROM perfis WHERE ativo ORDER BY id LIMIT 1") or "seutipster.app"
    pedido = robo_rodada.montar(jogos, dia, handle)
    if not pedido:
        await cfg_set(chave_dia, "sem_jogos")
        return {"ok": True, "jogos": 0, "motivo": "nenhum jogo das ligas prioritárias hoje"}
    pid, avisos = await criar_post_de_slides(pedido, "robo")
    await cfg_set(chave_dia, str(pid))
    return {"ok": True, "jogos": len(jogos), "slides": len(pedido["slides"]), "id": str(pid), "avisos": avisos}


async def job_rodada() -> None:
    await asyncio.sleep(30)
    while True:
        try:
            agora = datetime.now(robo_rodada.TZ)
            if agora.hour >= RODADA_HORA and robo_rodada.chave():
                r = await gerar_rodada(agora.date())
                if r.get("ok"):
                    log.info("[RODADA] %s", r)
        except Exception as e:
            log.warning("[RODADA] %s", e)
            await enviar_push({"title": "⚠️ Rodada do dia falhou", "body": str(e)[:120], "url": "/"})
            await asyncio.sleep(1800)  # não insiste em loop; tenta de novo em 30 min
        await asyncio.sleep(120)


class RodadaIn(BaseModel):
    data: Optional[str] = None  # AAAA-MM-DD; vazio = hoje
    forcar: bool = False


@app.post("/api/robos/rodada")
async def rodada_manual(body: RodadaIn, _=Depends(exige_login)):
    from datetime import date as _date
    try:
        dia = _date.fromisoformat(body.data) if body.data else None
        return await gerar_rodada(dia, body.forcar)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e)[:300])


# --------------------------------------------------- robô: resultados ----

_resultados_estado: dict[str, Any] = {"ultima": None, "erro": None}


async def ciclo_resultados() -> dict[str, Any]:
    agora = datetime.now(robo_rodada.TZ)
    dias = [agora.date()]
    if agora.hour < 4:  # jogo que acabou depois da meia-noite ainda é "de ontem"
        dias.insert(0, agora.date() - timedelta(days=1))
    jogos = []
    for d in dias:
        jogos += await robo_resultados.jogos(d)
    _resultados_estado["ultima"] = _iso(datetime.now(timezone.utc))
    acabados = [j for j in jogos if j["fixture"]["status"]["short"] in robo_resultados.FIM and robo_resultados.principal(j)]
    primeira_vez = not await pool.fetchval("SELECT 1 FROM resultados LIMIT 1")
    handle = await pool.fetchval("SELECT handle FROM perfis WHERE ativo ORDER BY id LIMIT 1") or "seutipster.app"
    feitos_hoje = await pool.fetchval(
        "SELECT count(*) FROM resultados WHERE status='postado' AND (em AT TIME ZONE 'America/Sao_Paulo')::date=$1", agora.date())
    res = {"acabados": len(acabados), "postados": 0}
    for j in acabados:
        fid = j["fixture"]["id"]
        ja = await pool.fetchrow("SELECT status, tentativas FROM resultados WHERE fixture_id=$1", fid)
        if ja and (ja["status"] != "erro" or ja["tentativas"] >= 3):
            continue
        inicio = datetime.fromisoformat(j["fixture"]["date"].replace("Z", "+00:00"))
        # estreia do robô (ou jogo antigo): só marca como visto, não posta atrasado
        if primeira_vez or datetime.now(timezone.utc) - inicio > timedelta(hours=6):
            await pool.execute("INSERT INTO resultados(fixture_id,status) VALUES($1,'ignorado') ON CONFLICT DO NOTHING", fid)
            continue
        if robo_resultados.TETO_DIA and feitos_hoje >= robo_resultados.TETO_DIA:
            await pool.execute("INSERT INTO resultados(fixture_id,status) VALUES($1,'teto') ON CONFLICT DO NOTHING", fid)
            continue
        try:
            gs = await robo_resultados.gols(fid)
            pedido = robo_resultados.montar(j, gs, handle)
            d0 = pedido["slides"][0]["dados"]
            foto_estadio = d0.get("foto")
            if not await foto_do_jogo(j, gs, pedido):
                pedido["sem_auto"] = True  # sem foto de jogador conferida: vai para a fila, não sai sozinho
            try:
                pid, _av = await criar_post_de_slides(pedido, "robo")
            except HTTPException as e:
                if e.status_code != 422:
                    raise
                # foto do jogador não baixou: estádio; se nem isso, sai sem foto
                d0["foto"] = foto_estadio if d0.get("foto") != foto_estadio else None
                d0["credito_foto"] = None
                try:
                    pid, _av = await criar_post_de_slides(pedido, "robo")
                except HTTPException as e2:
                    if e2.status_code != 422:
                        raise
                    d0["foto"] = None
                    pid, _av = await criar_post_de_slides(pedido, "robo")
            await pool.execute(
                "INSERT INTO resultados(fixture_id,status,post_id) VALUES($1,'postado',$2) "
                "ON CONFLICT (fixture_id) DO UPDATE SET status='postado', post_id=EXCLUDED.post_id, erro=NULL, em=now()",
                fid, pid)
            feitos_hoje += 1
            res["postados"] += 1
        except Exception as e:
            msg = e.detail if isinstance(e, HTTPException) else str(e)
            log.exception("[RESULTADOS] fixture %s", fid)
            await pool.execute(
                "INSERT INTO resultados(fixture_id,status,erro,tentativas) VALUES($1,'erro',$2,1) "
                "ON CONFLICT (fixture_id) DO UPDATE SET status='erro', erro=EXCLUDED.erro, tentativas=resultados.tentativas+1, em=now()",
                fid, str(msg)[:300])
    _resultados_estado["erro"] = None
    return res


async def foto_do_jogo(j: dict, gs: list[dict], pedido: dict) -> bool:
    """Foto do protagonista do jogo, conferida (jogador certo + foto limpa).
    Nunca usa imagem de matéria ou arte de outro veículo. Devolve True se achou."""
    d = pedido["slides"][0]["dados"]
    th, ta = j["teams"]["home"], j["teams"]["away"]
    venc = th["id"] if th.get("winner") else (ta["id"] if ta.get("winner") else None)
    h = robo_resultados.heroi(gs, venc)
    if not h:
        return False
    clube = robo_rodada.nome(th["name"] if h["time_id"] == th["id"] else ta["name"])
    try:
        f = await foto_jogador.foto_verificada(h.get("jogador"), clube, h.get("jogador_id"))
    except Exception:
        log.exception("[RESULTADOS] foto do jogador")
        f = None
    if not f:
        return False
    d["foto"], d["credito_foto"], d["foco"] = f["url"], f["credito"], "center 18%"
    return True


async def job_resultados() -> None:
    await asyncio.sleep(45)
    while True:
        try:
            if robo_rodada.chave() and (await cfg_get("resultados_ligado")) != "0":
                r = await ciclo_resultados()
                if r.get("postados"):
                    log.info("[RESULTADOS] %s", r)
        except Exception as e:
            _resultados_estado["erro"] = str(e)[:300]
            log.warning("[RESULTADOS] %s", e)
        await asyncio.sleep(robo_resultados.INTERVALO_MIN * 60)


@app.get("/api/robos/resultados")
async def resultados_status(_=Depends(exige_login)):
    hoje = datetime.now(robo_rodada.TZ).date()
    n = await pool.fetchval(
        "SELECT count(*) FROM resultados WHERE status='postado' AND (em AT TIME ZONE 'America/Sao_Paulo')::date=$1", hoje)
    return {"ligado": (await cfg_get("resultados_ligado")) != "0", "tem_chave": bool(robo_rodada.chave()),
            "postados_hoje": n, "teto_dia": robo_resultados.TETO_DIA, "intervalo_min": robo_resultados.INTERVALO_MIN,
            "ultima": _resultados_estado["ultima"], "erro": _resultados_estado["erro"]}


@app.post("/api/robos/resultados/ligar")
async def resultados_ligar(body: AutoIn, _=Depends(exige_login)):
    await cfg_set("resultados_ligado", "1" if body.ligado else "0")
    return {"ligado": body.ligado}


@app.post("/api/robos/resultados/rodar")
async def resultados_rodar(_=Depends(exige_login)):
    try:
        return await ciclo_resultados()
    except Exception as e:
        raise HTTPException(502, str(e)[:300])


# ----------------------------------------------------- robô: notícias ----

_noticias_estado: dict[str, Any] = {"ultima": None, "erro": None, "feeds": {}, "ultimo_resultado": None}
_noticias_lock = asyncio.Lock()


async def _uso_hoje() -> dict:
    hoje = datetime.now(robo_rodada.TZ).date().isoformat()
    raw = await cfg_get(f"ia_uso_{hoje}")
    return json.loads(raw) if raw else {"chamadas": 0, "in": 0, "out": 0}


async def _somar_uso(u: dict) -> None:
    hoje = datetime.now(robo_rodada.TZ).date().isoformat()
    atual = await _uso_hoje()
    atual = {"chamadas": atual["chamadas"] + 1, "in": atual["in"] + u.get("in", 0), "out": atual["out"] + u.get("out", 0)}
    await cfg_set(f"ia_uso_{hoje}", json.dumps(atual))


async def _ia(modelo: str, sistema: str, usuario: str, max_tokens: int = 2000):
    uso = await _uso_hoje()
    if uso["in"] + uso["out"] >= robo_noticias.TOKENS_DIA:
        raise robo_noticias.LimiteIA("limite diário de uso da IA atingido")
    dados, u = await robo_noticias.claude(modelo, sistema, usuario, max_tokens)
    await _somar_uso(u)
    return dados


async def noticias_ligado() -> bool:
    return (await cfg_get("noticias_ligado")) != "0" and bool(robo_noticias.chave_ia())


def _nota_efetiva(nota: float, n_fontes: int) -> float:
    return nota + min(1.5, 0.5 * max(0, n_fontes - 1))


async def ciclo_noticias(forcar: bool = False) -> dict[str, Any]:
    async with _noticias_lock:
        if not forcar and not await noticias_ligado():
            return {"ok": False, "motivo": "robô desligado ou sem ANTHROPIC_API_KEY"}
        res: dict[str, Any] = {"novas": 0, "assuntos": 0, "posts": 0}
        itens, saude = await robo_noticias.ler_feeds()
        _noticias_estado["feeds"] = saude
        _noticias_estado["ultima"] = _iso(datetime.now(timezone.utc))

        # 1) só o que nunca foi visto
        hashes = {robo_noticias.hash_url(i["url"]): i for i in itens}
        vistos = {r["url_hash"] for r in await pool.fetch(
            "SELECT url_hash FROM noticias_itens WHERE url_hash = ANY($1::text[])", list(hashes))}
        novos = [dict(i, hash=h) for h, i in hashes.items() if h not in vistos]
        novos.sort(key=lambda i: i.get("publicado") or "", reverse=True)
        primeira_vez = not await pool.fetchval("SELECT 1 FROM noticias_itens LIMIT 1")
        triar, ignorar = novos[:60], novos[60:]
        if primeira_vez:  # na estreia não despeja 6h de atraso: avalia só as mais recentes
            triar, ignorar = novos[:25], novos[25:]
        for i in ignorar:
            await pool.execute("INSERT INTO noticias_itens(url_hash,url,fonte,titulo) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING",
                               i["hash"], i["url"], i["fonte"], i["titulo"])
        res["novas"] = len(triar)

        # 2) triagem (nota + agrupamento)
        if triar:
            recentes = [dict(r) for r in await pool.fetch(
                "SELECT id, titulo FROM assuntos WHERE criado_em > now() - interval '48 hours' ORDER BY id DESC LIMIT 80")]
            ids_recentes = {r["id"] for r in recentes}
            for ini in range(0, len(triar), 30):
                lote = triar[ini:ini + 30]
                for k, it in enumerate(lote):
                    it["id"] = k
                saida = await _ia(robo_noticias.MODELO_TRIAGEM, robo_noticias.SISTEMA_TRIAGEM,
                                  robo_noticias.prompt_triagem(lote, recentes), 8000)
                por_item = {o.get("item"): o for o in (saida if isinstance(saida, list) else []) if isinstance(o, dict)}
                novos_no_lote: dict[str, int] = {}
                for it in lote:
                    o = por_item.get(it["id"]) or {}
                    aid = o.get("assunto_existente")
                    if aid not in ids_recentes:
                        aid = None
                    titulo_a = (o.get("assunto") or it["titulo"])[:120]
                    nota = robo_noticias.reforco(f"{it['titulo']} {it['resumo']} {titulo_a}",
                                                 float(o.get("nota") or 0), o.get("tipo"))
                    if aid is None and titulo_a in novos_no_lote:
                        aid = novos_no_lote[titulo_a]
                    if aid is None:
                        aid = await pool.fetchval(
                            "INSERT INTO assuntos(titulo,tipo,nota,motivo) VALUES($1,$2,$3,$4) RETURNING id",
                            titulo_a, o.get("tipo"), nota, (o.get("motivo") or "")[:200])
                        novos_no_lote[titulo_a] = aid
                        recentes.append({"id": aid, "titulo": titulo_a})
                        ids_recentes.add(aid)
                        res["assuntos"] += 1
                    else:
                        await pool.execute("UPDATE assuntos SET nota=GREATEST(nota,$2) WHERE id=$1", aid, nota)
                    await pool.execute(
                        "INSERT INTO noticias_itens(url_hash,url,fonte,titulo,resumo,imagem,assunto_id) "
                        "VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT DO NOTHING",
                        it["hash"], it["url"], it["fonte"], it["titulo"], it["resumo"], it.get("imagem"), aid)

        # 3) escreve os melhores (respeitando teto do dia e horário de silêncio)
        agora = datetime.now(robo_rodada.TZ)
        ini_s, fim_s = robo_noticias.HORA_SILENCIO
        silencio = agora.hour >= ini_s or agora.hour < fim_s
        feitos_hoje = await pool.fetchval(
            "SELECT count(*) FROM assuntos WHERE postado_em IS NOT NULL AND (postado_em AT TIME ZONE 'America/Sao_Paulo')::date = $1",
            agora.date())
        cands = await pool.fetch("""
            SELECT a.id, a.titulo, a.nota, a.tipo, count(DISTINCT n.fonte) AS n_fontes
            FROM assuntos a JOIN noticias_itens n ON n.assunto_id=a.id
            WHERE a.status='novo' AND a.criado_em > now() - interval '12 hours'
            GROUP BY a.id ORDER BY a.nota DESC, a.id DESC LIMIT 20""")
        cands = sorted(cands, key=lambda c: -_nota_efetiva(c["nota"], c["n_fontes"]))
        por_ciclo = 0
        for cnd in cands:
            ne = _nota_efetiva(cnd["nota"], cnd["n_fontes"])
            if ne < robo_noticias.NOTA_MIN:
                break
            if robo_noticias.TETO_DIA and feitos_hoje >= robo_noticias.TETO_DIA and ne < robo_noticias.NOTA_URGENTE:
                break
            if silencio and ne < robo_noticias.NOTA_URGENTE:
                continue
            if por_ciclo >= 6:
                break
            try:
                await escrever_post_noticia(cnd["id"], cnd["titulo"], ne)
                feitos_hoje += 1
                por_ciclo += 1
                res["posts"] += 1
            except robo_noticias.LimiteIA:
                raise
            except Exception as e:
                log.exception("[NOTICIAS] falha ao escrever assunto %s", cnd["id"])
                await pool.execute("UPDATE assuntos SET status='erro', erro=$2 WHERE id=$1", cnd["id"], str(e)[:300])
        _noticias_estado["ultimo_resultado"] = res
        _noticias_estado["erro"] = None
        return {"ok": True, **res}


async def escrever_post_noticia(aid: int, titulo_assunto: str, nota_ef: float) -> uuid.UUID:
    itens = [dict(r) for r in await pool.fetch(
        "SELECT url,fonte,titulo,resumo,imagem FROM noticias_itens WHERE assunto_id=$1 ORDER BY visto_em", aid)]
    # até 3 fontes, veículos diferentes
    fontes, vistos = [], set()
    for it in itens:
        if it["fonte"] not in vistos:
            fontes.append(it)
            vistos.add(it["fonte"])
        if len(fontes) == 3:
            break
    materias = await asyncio.gather(*(robo_noticias.ler_materia(f["url"]) for f in fontes))
    for f, m in zip(fontes, materias):
        f["texto"] = m.get("texto", "")
    post = await _ia(robo_noticias.MODELO_TEXTO, robo_noticias.SISTEMA_REDACAO,
                     robo_noticias.prompt_redacao(titulo_assunto, fontes), 2500)
    if not isinstance(post, dict) or not post.get("legenda") or not post.get("titulo_card"):
        raise ValueError("a IA não devolveu o post completo")
    legenda = robo_noticias.corrigir_legenda(post["legenda"])
    problemas = robo_noticias.checar_legenda(legenda)

    # foto: só jogador conferido (API-Football + busca + checagem por IA). Nunca imagem de matéria/arte de outro canal.
    foto, credito = None, None
    try:
        fv = await foto_jogador.foto_verificada(post.get("pessoa_foto"), post.get("clube_foto"))
    except Exception:
        log.exception("[NOTICIAS] foto do jogador")
        fv = None
    if fv:
        foto, credito = fv["url"], fv["credito"]
    urgente = bool(post.get("urgente")) or nota_ef >= robo_noticias.NOTA_URGENTE
    dados = {
        "foto": foto, "credito_foto": credito, "tag": post.get("tag") or ("Urgente" if urgente else "Notícia"),
        "tag2": post.get("tag2"), "titulo": post["titulo_card"][:90],
        "linha_fina": post.get("linha_fina"), "fatos": (post.get("fatos") or [])[:4],
    }
    if not foto:
        dados["marca_dagua"] = (post.get("tag2") or "")[:8].upper() or None
    pedido = {
        "perfil": None, "linha": "noticia", "titulo": titulo_assunto[:200], "legenda": legenda,
        "fonte": post.get("fonte_nome") or fontes[0]["fonte"],
        "fonte_urls": [f["url"] for f in fontes],
        "slides": [{"modelo": "noticia", "dados": dados}],
        "aviso": ("🚨 URGENTE · " if urgente else "📰 ") + "notícia para aprovar",
    }
    try:
        pid, _av = await criar_post_de_slides(pedido, "robo")
    except HTTPException as e:
        if foto and e.status_code == 422:  # foto da matéria não baixou: tenta foto livre, senão sai sem foto
            dados["foto"], dados["credito_foto"] = None, None
            dados["marca_dagua"] = (post.get("tag2") or "")[:8].upper() or None
            try:
                pid, _av = await criar_post_de_slides(pedido, "robo")
            except HTTPException as e2:
                if e2.status_code != 422:
                    raise
                dados["foto"], dados["credito_foto"] = None, None
                dados["marca_dagua"] = (post.get("tag2") or "")[:8].upper() or None
                pid, _av = await criar_post_de_slides(pedido, "robo")
        else:
            raise
    await pool.execute("UPDATE assuntos SET status='postado', post_id=$2, postado_em=now() WHERE id=$1", aid, pid)
    if problemas:
        await evento(pid, "checagem_legenda", "; ".join(problemas))
    return pid


async def job_noticias() -> None:
    await asyncio.sleep(60)
    while True:
        try:
            if await noticias_ligado():
                r = await ciclo_noticias()
                if r.get("posts"):
                    log.info("[NOTICIAS] %s", r)
        except robo_noticias.LimiteIA as e:
            _noticias_estado["erro"] = str(e)
            hoje = datetime.now(robo_rodada.TZ).date().isoformat()
            if not await cfg_get(f"ia_limite_avisado_{hoje}"):
                await cfg_set(f"ia_limite_avisado_{hoje}", "1")
                await enviar_push({"title": "⚠️ Robô de notícias pausado", "body": "Limite diário de uso da IA atingido. Volta amanhã.", "url": "/"})
        except Exception as e:
            _noticias_estado["erro"] = str(e)[:300]
            log.warning("[NOTICIAS] %s", e)
        await asyncio.sleep(robo_noticias.INTERVALO_MIN * 60)


@app.get("/api/robos/noticias")
async def noticias_status(_=Depends(exige_login)):
    hoje = datetime.now(robo_rodada.TZ).date()
    postados = await pool.fetchval(
        "SELECT count(*) FROM assuntos WHERE postado_em IS NOT NULL AND (postado_em AT TIME ZONE 'America/Sao_Paulo')::date = $1", hoje)
    top = await pool.fetch("""SELECT a.titulo, a.nota, a.status, count(DISTINCT n.fonte) n_fontes FROM assuntos a
        LEFT JOIN noticias_itens n ON n.assunto_id=a.id WHERE a.criado_em > now() - interval '24 hours'
        GROUP BY a.id ORDER BY a.nota DESC LIMIT 8""")
    feeds = _noticias_estado["feeds"]
    return {
        "ligado": (await cfg_get("noticias_ligado")) != "0", "tem_chave": bool(robo_noticias.chave_ia()),
        "intervalo_min": robo_noticias.INTERVALO_MIN, "teto_dia": robo_noticias.TETO_DIA, "postados_hoje": postados,
        "ultima": _noticias_estado["ultima"], "erro": _noticias_estado["erro"], "uso_hoje": await _uso_hoje(),
        "limite_tokens": robo_noticias.TOKENS_DIA,
        "feeds_ok": sum(1 for v in feeds.values() if not v), "feeds_total": len(feeds),
        "feeds_com_erro": [u for u, v in feeds.items() if v][:20],
        "assuntos_24h": [dict(r) for r in top],
    }


@app.post("/api/robos/noticias/ligar")
async def noticias_ligar(body: AutoIn, _=Depends(exige_login)):
    await cfg_set("noticias_ligado", "1" if body.ligado else "0")
    return {"ligado": body.ligado}


@app.post("/api/robos/noticias/rodar")
async def noticias_rodar(_=Depends(exige_login)):
    try:
        return await ciclo_noticias(forcar=True)
    except Exception as e:
        raise HTTPException(502, str(e)[:300])


@app.post("/api/robo/posts")
async def novo_robo(body: RoboPostIn, _=Depends(exige_robo)):
    perfil_id = body.perfil_id
    if not perfil_id and body.perfil_handle:
        perfil_id = await pool.fetchval(
            "SELECT id FROM perfis WHERE lower(handle)=lower($1)", body.perfil_handle.lstrip("@")
        )
    midias = []
    for m in body.midias:
        try:
            midias.append((m.mime, base64.b64decode(m.base64)))
        except Exception:
            raise HTTPException(400, "mídia em base64 inválida")
    pid = await criar_post(perfil_id=perfil_id, linha=body.linha, titulo=body.titulo, legenda=body.legenda,
                           fonte=body.fonte, origem=body.origem, midias=midias, sugerido_para=body.sugerido_para,
                           fonte_urls=body.fonte_urls)
    return {"id": str(pid)}


class EditarIn(BaseModel):
    legenda: Optional[str] = None
    titulo: Optional[str] = None
    perfil_id: Optional[int] = None


@app.patch("/api/posts/{pid}")
async def editar(pid: uuid.UUID, body: EditarIn, _=Depends(exige_login)):
    r = await pool.fetchrow("SELECT status FROM posts WHERE id=$1", pid)
    if not r:
        raise HTTPException(404, "post não encontrado")
    if r["status"] not in ("pendente", "erro", "recusado"):
        raise HTTPException(409, "esse post já saiu da fila e não pode ser editado")
    if body.legenda is not None and len(body.legenda) > 2200:
        raise HTTPException(400, "legenda acima de 2.200 caracteres")
    await pool.execute(
        "UPDATE posts SET legenda=COALESCE($2,legenda), titulo=COALESCE($3,titulo), perfil_id=COALESCE($4,perfil_id) WHERE id=$1",
        pid, body.legenda, body.titulo, body.perfil_id,
    )
    await evento(pid, "editado")
    return {"ok": True}


class AprovarIn(BaseModel):
    agendar_para: Optional[datetime] = None  # vazio = publicar agora


async def _aprovar_um(pid: uuid.UUID, agendar_para: Optional[datetime], base: str) -> dict:
    # trava otimista: só sai de pendente/erro uma vez, mesmo com 2 aparelhos aprovando juntos
    row = await pool.fetchrow(
        "UPDATE posts SET status='publicando', decidido_em=now(), erro=NULL, agendar_para=$2 "
        "WHERE id=$1 AND status IN ('pendente','erro') RETURNING id",
        pid, agendar_para,
    )
    if not row:
        return {"id": str(pid), "ok": False, "erro": "já decidido em outro aparelho"}
    asyncio.create_task(publicar_no_publora(pid, base))
    return {"id": str(pid), "ok": True}


@app.post("/api/posts/{pid}/aprovar")
async def aprovar(pid: uuid.UUID, body: AprovarIn, request: Request, _=Depends(exige_login)):
    if body.agendar_para and body.agendar_para <= datetime.now(timezone.utc) + timedelta(minutes=2):
        body.agendar_para = None
    res = await _aprovar_um(pid, body.agendar_para, base_publica(request))
    if not res["ok"]:
        raise HTTPException(409, res["erro"])
    return res


class LoteIn(BaseModel):
    ids: list[uuid.UUID]


@app.post("/api/posts/aprovar-lote")
async def aprovar_lote(body: LoteIn, request: Request, _=Depends(exige_login)):
    base = base_publica(request)
    return {"resultados": [await _aprovar_um(i, None, base) for i in body.ids[:50]]}


class RecusarIn(BaseModel):
    motivo: str = ""


@app.post("/api/posts/{pid}/recusar")
async def recusar(pid: uuid.UUID, body: RecusarIn, _=Depends(exige_login)):
    row = await pool.fetchrow(
        "UPDATE posts SET status='recusado', decidido_em=now(), motivo_recusa=$2 "
        "WHERE id=$1 AND status IN ('pendente','erro') RETURNING id",
        pid, body.motivo[:300],
    )
    if not row:
        raise HTTPException(409, "esse post já foi decidido")
    await evento(pid, "recusado", body.motivo)
    await pool.execute("UPDATE assuntos SET status='recusado' WHERE post_id=$1", pid)
    return {"ok": True}


@app.post("/api/posts/{pid}/voltar-fila")
async def voltar_fila(pid: uuid.UUID, _=Depends(exige_login)):
    row = await pool.fetchrow(
        "UPDATE posts SET status='pendente', decidido_em=NULL, motivo_recusa=NULL, erro=NULL "
        "WHERE id=$1 AND status IN ('recusado','erro') RETURNING id", pid,
    )
    if not row:
        raise HTTPException(409, "só posts recusados ou com erro voltam para a fila")
    return {"ok": True}


# ---------------------------------------------------------- publicar ----

async def publicar_no_publora(pid: uuid.UUID, base: str) -> None:
    try:
        r = await pool.fetchrow(
            "SELECT p.*, pf.publora_platform_id FROM posts p LEFT JOIN perfis pf ON pf.id=p.perfil_id WHERE p.id=$1",
            pid,
        )
        if not r["publora_platform_id"]:
            raise publora.PubloraErro("o post não tem perfil do Instagram vinculado (sincronize os perfis)")
        if not base.startswith("https://"):
            raise publora.PubloraErro("endereço público do servidor não configurado (PUBLIC_BASE_URL)")
        midias = await pool.fetch("SELECT token,mime FROM midias WHERE post_id=$1 ORDER BY ordem", pid)
        urls = [f"{base}/m/{m['token']}.{MIME_OK[m['mime']]}" for m in midias]
        reel = len(midias) == 1 and midias[0]["mime"].startswith("video/")
        res = await publora.publicar(
            legenda=r["legenda"],
            platform_id=r["publora_platform_id"],
            media_urls=urls,
            agendar_para_iso=_iso(r["agendar_para"]),
            reel=reel,
            chave_idempotencia=f"dkc-{pid}",
        )
        gid = res.get("postGroupId") or (res.get("postGroup") or {}).get("id")
        novo = "agendado" if r["agendar_para"] else "publicado"
        await pool.execute(
            "UPDATE posts SET status=$2, publora_group_id=$3, publicado_em=CASE WHEN $2='publicado' THEN now() ELSE NULL END WHERE id=$1",
            pid, novo, gid,
        )
        await evento(pid, novo, json.dumps(res)[:1500])
        log.info("[PUBLORA] post %s → %s (%s)", pid, novo, gid)
    except Exception as e:
        msg = str(e)[:500]
        log.exception("[PUBLORA] falha no post %s", pid)
        await pool.execute("UPDATE posts SET status='erro', erro=$2 WHERE id=$1", pid, msg)
        await evento(pid, "erro", msg)
        await enviar_push({"title": "⚠️ Falha ao publicar", "body": msg[:120], "url": f"/?post={pid}"})


async def job_status_publora() -> None:
    """A cada 10 min: agendados cujo horário passou viram 'publicado'
    (e confere no Publora, se a rota de status responder)."""
    while True:
        await asyncio.sleep(600)
        try:
            rows = await pool.fetch(
                "SELECT id, publora_group_id FROM posts WHERE status='agendado' AND agendar_para < now() - interval '5 minutes'"
            )
            for r in rows:
                falhou = None
                if r["publora_group_id"]:
                    try:
                        d = await publora.status_post(r["publora_group_id"])
                        txt = json.dumps(d).lower()
                        if '"status": "failed"' in txt or '"status":"failed"' in txt:
                            falhou = "o Publora informou falha na publicação"
                    except Exception:
                        pass
                if falhou:
                    await pool.execute("UPDATE posts SET status='erro', erro=$2 WHERE id=$1", r["id"], falhou)
                else:
                    await pool.execute("UPDATE posts SET status='publicado', publicado_em=now() WHERE id=$1", r["id"])
        except Exception:
            log.exception("[JOB] status publora")


# ------------------------------------------------------------ perfis ----

@app.get("/api/perfis")
async def perfis(_=Depends(exige_login)):
    rows = await pool.fetch("SELECT id,nome,handle,publora_platform_id,ativo FROM perfis ORDER BY id")
    return {"perfis": [dict(r) for r in rows]}


@app.post("/api/perfis/sincronizar")
async def sincronizar(_=Depends(exige_login)):
    try:
        cons = await publora.conexoes()
    except publora.PubloraErro as e:
        raise HTTPException(502, str(e))
    n = 0
    for c in cons:
        pid = str(c.get("platformId", ""))
        if not pid.startswith("instagram-"):
            continue
        handle = c.get("username") or pid
        await pool.execute(
            "INSERT INTO perfis(nome,handle,publora_platform_id) VALUES($1,$2,$3) "
            "ON CONFLICT (publora_platform_id) DO UPDATE SET handle=EXCLUDED.handle",
            c.get("displayName") or handle, handle, pid,
        )
        n += 1
    return {"sincronizados": n, "conexoes": [
        {"platformId": c.get("platformId"), "username": c.get("username"), "tokenStatus": c.get("tokenStatus"),
         "expira": c.get("accessTokenExpiresAt")} for c in cons
    ]}


@app.get("/api/saude")
async def saude(request: Request, _=Depends(exige_login)):
    out: dict[str, Any] = {"base_publica": base_publica(request)}
    try:
        cons = await publora.conexoes()
        out["publora"] = {"ok": True, "conexoes": len(cons)}
    except Exception as e:
        out["publora"] = {"ok": False, "erro": str(e)[:300]}
    out["push_inscritos"] = await pool.fetchval("SELECT count(*) FROM push_subs")
    out["robo_configurado"] = bool(ROBO_KEY)
    out["api_football"] = bool(robo_rodada.chave())
    import importlib.util
    out["recorte"] = importlib.util.find_spec("rembg") is not None
    return out


# ------------------------------------------------- QG do Nog (status) ----

def _digital(v: str) -> str:
    """Impressão digital curta da chave: compara os dois lados sem revelar a chave."""
    return hashlib.sha256(v.encode()).hexdigest()[:8] if v else "vazia"


async def exige_qg(x_qg_key: str = Header(default="")) -> None:
    if not QG_KEY:
        enviada = x_qg_key.strip()
        if enviada and QG_KEY_SHA256 and hmac.compare_digest(hashlib.sha256(enviada.encode()).hexdigest(), QG_KEY_SHA256):
            return
        vistos = [repr(n) for n in os.environ if "QG" in n.upper()]
        servico = os.getenv("RAILWAY_SERVICE_NAME", "?")
        raise HTTPException(401, f"QG_KEY não está configurada no DKC Social (serviço {servico}; "
                                 f"variáveis com QG no nome: {', '.join(vistos) or 'nenhuma'})")
    if not hmac.compare_digest(x_qg_key.strip(), QG_KEY):
        raise HTTPException(401, f"chave diferente — DKC Social tem {_digital(QG_KEY)}, o QG mandou {_digital(x_qg_key.strip())}")


@app.get("/api/qg/status")
async def qg_status(_=Depends(exige_qg)):
    """Só leitura: o QG do Nog usa para mostrar a equipe do Social trabalhando de verdade."""
    hoje = datetime.now(robo_rodada.TZ).date()
    por_status = {r["status"]: r["n"] for r in await pool.fetch("SELECT status, count(*) n FROM posts GROUP BY status")}
    dia = await pool.fetchrow(
        "SELECT count(*) FILTER (WHERE status='publicado' AND publicado_em::date = (now() AT TIME ZONE 'America/Bahia')::date) pub, "
        "count(*) FILTER (WHERE status='recusado' AND decidido_em::date = (now() AT TIME ZONE 'America/Bahia')::date) rec, "
        "count(*) FILTER (WHERE criado_em::date = (now() AT TIME ZONE 'America/Bahia')::date) criados "
        "FROM posts"
    )
    ultimo = await pool.fetchrow("SELECT titulo, status, linha, criado_em FROM posts ORDER BY criado_em DESC LIMIT 1")
    res_hoje = await pool.fetchval(
        "SELECT count(*) FROM resultados WHERE status='postado' AND (em AT TIME ZONE 'America/Sao_Paulo')::date=$1", hoje)
    def _iso(v):
        return v.isoformat() if hasattr(v, "isoformat") else v
    return {
        "agora": datetime.now(robo_rodada.TZ).isoformat(),
        "fila": {k: por_status.get(k, 0) for k in STATUS_VALIDOS},
        "hoje": {"publicados": dia["pub"], "recusados": dia["rec"], "criados": dia["criados"], "resultados": res_hoje},
        "ultimo_post": ({"titulo": ultimo["titulo"], "status": ultimo["status"], "linha": ultimo["linha"],
                         "criado_em": _iso(ultimo["criado_em"])} if ultimo else None),
        "robos": {
            "noticias": {"ligado": (await cfg_get("noticias_ligado")) != "0", "ultima": _iso(_noticias_estado["ultima"]),
                         "erro": _noticias_estado["erro"]},
            "resultados": {"ligado": (await cfg_get("resultados_ligado")) != "0", "ultima": _iso(_resultados_estado["ultima"]),
                           "erro": _resultados_estado["erro"]},
            "pauta": {"ultima": _iso(_pauta_estado["ultima"]), "erro": _pauta_estado["erro"]},
        },
    }


# ----------------------------------------------------- mídia pública ----

@app.get("/m/{nome}")
async def midia_publica(nome: str):
    token = nome.rsplit(".", 1)[0]
    r = await pool.fetchrow("SELECT mime,dados FROM midias WHERE token=$1", token)
    if not r:
        raise HTTPException(404)
    return Response(content=bytes(r["dados"]), media_type=r["mime"],
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


# ------------------------------------------------------------ front ----

@app.get("/healthz")
async def healthz():
    await pool.fetchval("SELECT 1")
    return {"ok": True}


@app.get("/sw.js")
async def sw():
    return FileResponse(STATIC / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/favicon.ico")
async def favicon():
    return FileResponse(STATIC / "favicon.ico", media_type="image/x-icon")


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")
