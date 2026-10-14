"""Cliente mínimo da API REST do Publora (servidor → servidor).

Base: https://api.publora.com/api/v1  ·  Auth: header x-publora-key
Para Instagram mandamos a mídia por URL pública (o Publora baixa do nosso
servidor), numa chamada só: create-post com mediaUrls + publishNow ou
scheduledTime.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import httpx

BASE_URL = os.getenv("PUBLORA_BASE_URL", "https://api.publora.com/api/v1")


class PubloraErro(RuntimeError):
    pass


def _key() -> str:
    k = os.getenv("PUBLORA_API_KEY", "").strip()
    if not k:
        raise PubloraErro("PUBLORA_API_KEY não configurada no servidor")
    return k


async def _req(method: str, path: str, json_body: Optional[dict] = None) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.request(
            method,
            BASE_URL + path,
            json=json_body,
            headers={"x-publora-key": _key(), "Content-Type": "application/json"},
        )
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text[:500]}
    if r.status_code >= 400:
        msg = data.get("error") or data.get("message") or data
        raise PubloraErro(f"Publora HTTP {r.status_code}: {msg}")
    return data


async def conexoes() -> list[dict[str, Any]]:
    data = await _req("GET", "/platform-connections")
    return data.get("connections", [])


async def publicar(
    *,
    legenda: str,
    platform_id: str,
    media_urls: list[str],
    agendar_para_iso: Optional[str] = None,
    reel: bool = False,
    chave_idempotencia: Optional[str] = None,
) -> dict[str, Any]:
    if not media_urls:
        raise PubloraErro("Instagram exige ao menos uma imagem ou vídeo")
    body: dict[str, Any] = {
        "content": legenda,
        "platforms": [platform_id],
        "mediaUrls": media_urls[:10],
    }
    if agendar_para_iso:
        body["scheduledTime"] = agendar_para_iso
    else:
        body["publishNow"] = True
    if reel:
        body["platformSettings"] = {"instagram": {"videoType": "REELS"}}
    if chave_idempotencia:
        body["idempotencyKey"] = chave_idempotencia
    return await _req("POST", "/create-post", body)


async def status_post(group_id: str) -> dict[str, Any]:
    return await _req("GET", f"/get-post/{group_id}")
