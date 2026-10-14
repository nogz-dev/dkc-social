"""Ponte de pauta: posts preparados no chat (Claude) chegam pelo GitHub.

O Claude grava `fila/<id>.json` no branch `pauta` do repositório. O servidor
confere o branch periodicamente; cada arquivo novo vira post na fila de
aprovação (cards renderizados aqui, com foto).

Formato do arquivo:
{
  "id": "2026-10-14-carvajal",
  "perfil": "seutipster.app",
  "linha": "noticia",
  "titulo": "Carvajal no Getafe",
  "legenda": "...",
  "fonte": "AS",
  "sugerido_para": "2026-10-14T18:00:00Z",      (opcional)
  "slides": [{"modelo": "noticia", "dados": {...}}],   (renderizados no servidor)
  "midias": ["fila/arquivos/x.jpg"]                    (ou arquivos prontos no branch)
}
"""
from __future__ import annotations

import os
from typing import Any

import httpx

REPO = os.getenv("PAUTA_REPO", "nogz-dev/dkc-social")
BRANCH = os.getenv("PAUTA_BRANCH", "pauta")
PASTA = os.getenv("PAUTA_DIR", "fila").strip("/")
TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
INTERVALO = int(os.getenv("PAUTA_INTERVALO", "30" if TOKEN else "120"))


def _headers() -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "User-Agent": "dkc-social"}
    if TOKEN:
        h["Authorization"] = f"Bearer {TOKEN}"
    return h


async def commit_atual(c: httpx.AsyncClient) -> str | None:
    r = await c.get(f"https://api.github.com/repos/{REPO}/branches/{BRANCH}", headers=_headers())
    if r.status_code == 404:
        return None  # branch ainda não existe
    r.raise_for_status()
    return r.json()["commit"]["sha"]


async def arquivos(c: httpx.AsyncClient, sha: str) -> list[dict[str, Any]]:
    r = await c.get(f"https://api.github.com/repos/{REPO}/git/trees/{sha}?recursive=1", headers=_headers())
    r.raise_for_status()
    return [
        {"path": t["path"], "sha": t["sha"]}
        for t in r.json().get("tree", [])
        if t["type"] == "blob" and t["path"].startswith(PASTA + "/") and t["path"].endswith(".json")
        and t["path"].count("/") == 1
    ]


async def baixar(c: httpx.AsyncClient, sha_commit: str, path: str) -> bytes:
    # raw por commit: imutável e fora do limite da API
    r = await c.get(f"https://raw.githubusercontent.com/{REPO}/{sha_commit}/{path}", headers={"User-Agent": "dkc-social"})
    r.raise_for_status()
    return r.content
