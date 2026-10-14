"""Renderiza os slides de um arquivo de pauta localmente (para conferir antes de mandar).

uso:  CARD_ALLOW_FILE=1 python -m tools.preview pauta.json [pasta_saida]
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.cards.render import renderizador  # noqa: E402


async def main():
    pauta = json.loads(Path(sys.argv[1]).read_text())
    saida = Path(sys.argv[2] if len(sys.argv) > 2 else "preview")
    saida.mkdir(parents=True, exist_ok=True)
    for i, s in enumerate(pauta.get("slides", []), 1):
        img, avisos = await renderizador.png(s["modelo"], s.get("dados", {}), pauta.get("perfil"), pauta.get("fonte", ""))
        p = saida / f"{pauta.get('id', 'card')}-{i}.jpg"
        p.write_bytes(img)
        print(p, f"{len(img)//1024} KB", ("avisos: " + "; ".join(avisos)) if avisos else "")
    await renderizador.fechar()


asyncio.run(main())
