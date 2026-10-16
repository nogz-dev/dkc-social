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

## Robô da Rodada do dia
Todo dia às 7h (`RODADA_HORA`) busca na API-Football os jogos das ligas
prioritárias (Libertadores, Brasileirão, Copa do Brasil, Sul-Americana,
Champions, Premier, LaLiga, Serie A, Bundesliga, Ligue 1) e manda um carrossel
para a fila com sugestão de publicar às 9h: capa + card de destaque para ligas
com até 2 jogos + lista por liga. Precisa de `API_FOOTBALL_KEY`.
Gerar manualmente: Ajustes → "Gerar a rodada de hoje agora" (`POST /api/robos/rodada`).

## Robô de notícias
A cada 15 min (`NOTICIAS_INTERVALO_MIN`) lê os feeds dos portais (lista em
`app/robos/noticias.py`, ou `NOTICIAS_FEEDS="Nome|url,..."`), descarta o que já
viu, uma IA rápida (`NOTICIAS_MODELO_TRIAGEM`) dá nota 0–10 e agrupa a mesma
notícia de vários portais (cada portal a mais soma +0,5, até +1,5). Assuntos com
nota ≥ 6 viram post (prioridades da casa: Neymar, Cristiano Ronaldo, clubes das
Séries A e B e regulamentação de apostas ganham nota mínima garantida) (IA de texto `NOTICIAS_MODELO_TEXTO`, regras da skill de
Instagram + checagem final da legenda), com foto (Wikimedia → imagem da matéria →
imagem do feed). Teto de 8 por dia (`NOTICIAS_TETO_DIA`), silêncio das 23h às 7h
salvo nota ≥ 9 (urgente), limite diário de tokens (`NOTICIAS_TOKENS_DIA`).
Assunto recusado nunca volta. Precisa de `ANTHROPIC_API_KEY`. Liga/desliga e
"procurar agora" em Ajustes.

## Robô de resultados
A cada 5 min (`RESULTADOS_INTERVALO_MIN`) confere os jogos do dia na API-Football.
Jogo encerrado (FT/AET/PEN) dos principais vira card de resultado e é publicado
sem aprovação (regra do servidor; chave "Resultados automáticos" em Ajustes).
Principais = todo jogo de Libertadores, Sul-Americana, Copa do Brasil,
Brasileirão e Champions; nas ligas europeias só jogo com clube grande
(`GRANDES` em `app/robos/resultados.py`). Teto de 12 por dia
(`RESULTADOS_TETO_DIA`). Na estreia e para jogos de mais de 6h atrás, só marca
como visto (não posta atrasado). Detecta virada, goleada, empate e pênaltis.

## Regra de fotos (inegociável)
Nunca usar imagem de matéria, arte ou post de outro veículo. A foto do card vem
de `app/cards/foto_jogador.py`: a API-Football confirma o jogador e dá a foto de
referência; buscamos candidatas pelo nome (Wikimedia Commons e, com
`BRAVE_API_KEY`, busca de imagens na web); uma IA com visão confere cada uma
(mesma pessoa da referência, fotografia limpa sem texto/logo/arte de outro
canal, boa para capa). Sem foto conferida: resultado vai para a fila (não sai
sozinho) e notícia sai sem foto de jogador.

## Arte 100% pela OpenAI (desde 16/10/2026)

Decisão do Nog: os templates próprios não criam mais arte. A OpenAI (`gpt-image-2`, `OPENAI_API_KEY`) cria a imagem
inteira, com textos, placar e escudos. O sistema entrega o briefing: o manual `brands/seu-tipster/brand-guidelines.md`
(cores, identidade, direção de arte, tipo de post), os textos exatos e, como referência, a foto conferida do jogador e os
escudos (nunca arte de outro veículo).

- Revisão pelo Claude: texto/número errado, logo de emissora, escudo ou rosto deformado → a OpenAI refaz 1x.
  Continuou com erro → a arte vai para a fila com o aviso "revisar a arte" (não publica sozinha).
- Por cima da arte entra só o logotipo oficial, o @ e a fonte, no rodapé (o manual proíbe redesenhar o logo).
- A OpenAI não devolveu imagem → o post não é criado, o robô tenta de novo e chega um push "Arte da OpenAI falhou"
  (no máximo 1 a cada 30 min).
- `/api/saude` → `imagens_openai`: quem faz a arte, se a chave funciona, imagens do dia e o último erro.
- Emergência: `IMAGENS_OPENAI=0` no Railway volta aos templates.

Variáveis: `OPENAI_IMAGE_MODEL` (`gpt-image-2`), `OPENAI_IMAGE_QUALITY` (`high`), `OPENAI_IMAGE_SIZE` (`1088x1360`),
`OPENAI_IMAGENS_DIA` (trava, 400).
