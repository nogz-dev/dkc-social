# DKC Social

Fila de aprovação de posts para Instagram. Robôs (ou upload manual) criam posts,
o app avisa por notificação no celular e no PC, e o post só vai para o Instagram
(via Publora) depois de aprovado.

- **App (celular):** PWA instalável, com push.
- **Dashboard (PC):** mesmo endereço, layout em grade com aprovação em lote.

## Stack
FastAPI + Postgres (asyncpg) + PWA em JS puro. Deploy no Railway (Dockerfile).
Web Push implementado em `app/webpush.py` (RFC 8291/8292) só com `cryptography`.

## Variáveis de ambiente (Railway)
| Variável | Obrigatória | O que é |
|---|---|---|
| `DATABASE_URL` | sim | Postgres do Railway (referência `${{Postgres.DATABASE_URL}}`) |
| `ADMIN_PASSWORD` | sim | Senha de acesso ao app |
| `PUBLORA_API_KEY` | sim | Chave da API do Publora (`sk_...`) |
| `ROBO_KEY` | para robôs | Chave que os robôs usam em `POST /api/robo/posts` (header `x-robo-key`) |
| `PUBLIC_BASE_URL` | não | Endereço público (ex.: `https://social.dkc.app`). Sem ela usa `RAILWAY_PUBLIC_DOMAIN` |
| `VAPID_PUBLIC` / `VAPID_PRIVATE_PEM` | não | Chaves de push. Sem elas, são geradas no 1º boot e guardadas no banco |

## Rotas principais
- `POST /api/login` · `GET /api/posts?status=pendente` · `POST /api/posts/{id}/aprovar` · `POST /api/posts/{id}/recusar`
- `POST /api/posts/aprovar-lote` · `PATCH /api/posts/{id}` · `POST /api/posts` (upload manual)
- `POST /api/robo/posts` (robôs, JSON com mídias em base64)
- `GET /m/{token}.png` — mídia pública que o Publora baixa na hora de publicar

## Robôs: formato do post
```json
POST /api/robo/posts   (header x-robo-key)
{
  "perfil_handle": "seutipster.app",
  "linha": "noticia",            // noticia | temporada | rodada | resultado | jogador
  "titulo": "Carvajal no Getafe",
  "legenda": "...",
  "fonte": "AS",
  "sugerido_para": "2026-10-14T18:00:00Z",
  "midias": [{"mime": "image/png", "base64": "..."}]
}
```

## Cards (fatia 2)
Modelos 1080×1350 em `app/cards/modelos` (Jinja2 + Chromium headless):
`noticia` · `jogador` · `rodada` · `resultado`. Fotos e escudos são baixados no
servidor (anti-SSRF) e embutidos. `*palavra*` no título vira destaque na cor da marca.
Marcas por perfil em `app/cards/render.py` (`MARCAS`).

- `POST /api/robo/cards` (header `x-robo-key`): `{perfil_handle, linha, titulo, legenda, fonte, slides:[{modelo, dados}]}`
- `POST /api/cards/preview` (logado): devolve o JPEG de um slide

## Pauta do Claude
Branch `pauta` deste repositório, pasta `fila/`. Cada `<id>.json` (mesmo formato
de `/api/robo/cards`, com `perfil` e `slides`) vira post na fila. O servidor
confere a cada 2 min (30 s com `GITHUB_TOKEN`). Erros passageiros são
re-tentados até 3 vezes. Variáveis opcionais: `GITHUB_TOKEN`, `PAUTA_REPO`,
`PAUTA_BRANCH`, `PAUTA_DIR`, `PAUTA_INTERVALO`.

Pré-visualizar localmente: `CARD_ALLOW_FILE=1 python -m tools.preview pauta.json saida/`
