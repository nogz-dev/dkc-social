"""Contador de uso de IA por dia (Anthropic + OpenAI), para o Gestor de Tokens do QG.

Só registra: nunca bloqueia nem derruba a chamada que está sendo medida.
Cada linha = (dia em Salvador, modelo, origem) com os tokens somados no próprio banco (UPSERT atômico).
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("dkc.uso_ia")
TZ = ZoneInfo("America/Bahia")
pool = None  # o main.py preenche no startup

TABELA = """
CREATE TABLE IF NOT EXISTS ia_uso_dia (
  dia date NOT NULL,
  modelo text NOT NULL,
  origem text NOT NULL,
  chamadas int NOT NULL DEFAULT 0,
  tin bigint NOT NULL DEFAULT 0,
  tout bigint NOT NULL DEFAULT 0,
  cache_r bigint NOT NULL DEFAULT 0,
  cache_w bigint NOT NULL DEFAULT 0,
  img_in bigint NOT NULL DEFAULT 0,
  imagens int NOT NULL DEFAULT 0,
  PRIMARY KEY (dia, modelo, origem)
);
"""


async def registrar(modelo: str, origem: str, usage: dict | None = None, imagens: int = 0) -> None:
    """usage: o bloco 'usage' cru da Anthropic ou da OpenAI (imagens)."""
    if pool is None:
        return
    u = usage or {}
    try:
        if "input_tokens_details" in u:  # OpenAI imagens: texto e imagem de entrada têm preços diferentes
            det = u.get("input_tokens_details") or {}
            img_in = int(det.get("image_tokens") or 0)
            tin = int(det.get("text_tokens") or max(int(u.get("input_tokens") or 0) - img_in, 0))
            cache_r = cache_w = 0
        else:
            img_in = 0
            tin = int(u.get("input_tokens") or 0)
            cache_r = int(u.get("cache_read_input_tokens") or 0)
            cache_w = int(u.get("cache_creation_input_tokens") or 0)
        tout = int(u.get("output_tokens") or 0)
        await pool.execute(
            "INSERT INTO ia_uso_dia (dia,modelo,origem,chamadas,tin,tout,cache_r,cache_w,img_in,imagens) "
            "VALUES ($1,$2,$3,1,$4,$5,$6,$7,$8,$9) ON CONFLICT (dia,modelo,origem) DO UPDATE SET "
            "chamadas=ia_uso_dia.chamadas+1, tin=ia_uso_dia.tin+EXCLUDED.tin, tout=ia_uso_dia.tout+EXCLUDED.tout, "
            "cache_r=ia_uso_dia.cache_r+EXCLUDED.cache_r, cache_w=ia_uso_dia.cache_w+EXCLUDED.cache_w, "
            "img_in=ia_uso_dia.img_in+EXCLUDED.img_in, imagens=ia_uso_dia.imagens+EXCLUDED.imagens",
            datetime.now(TZ).date(), modelo or "?", origem, tin, tout, cache_r, cache_w, img_in, int(imagens))
    except Exception as e:  # medir nunca pode quebrar o trabalho
        log.warning("[USO] registro falhou (%s/%s): %s", modelo, origem, e)


async def resumo(dias: int = 7) -> list[dict]:
    """Linhas dos últimos N dias (hoje incluso), para o /api/qg/status."""
    if pool is None:
        return []
    rows = await pool.fetch(
        "SELECT dia, modelo, origem, chamadas, tin, tout, cache_r, cache_w, img_in, imagens FROM ia_uso_dia "
        "WHERE dia > $1::date - $2::int ORDER BY dia DESC, origem", datetime.now(TZ).date(), dias)
    return [{**dict(r), "dia": r["dia"].isoformat()} for r in rows]
