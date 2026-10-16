"use strict";
/* DKC Social — fila de aprovação (celular + dashboard no PC) */

const VIEWS = {
  fila:       { t: "Fila de aprovação", nav: "Fila", status: "pendente", icon: "i-inbox", mob: "Fila" },
  agendados:  { t: "Agendados",         status: "publicando,agendado",   icon: "i-clock", mob: "Agenda" },
  publicados: { t: "Publicados",        status: "publicado",             icon: "i-send",  mob: "Publicados" },
  recusados:  { t: "Recusados e erros", status: "recusado,erro",         icon: "i-ban",   mob: null },
  ajustes:    { t: "Ajustes",           status: null,                    icon: "i-gear",  mob: "Ajustes" },
};
const STATUS_TXT = { pendente: "Aguardando", publicando: "Enviando", agendado: "Agendado", publicado: "Publicado", recusado: "Recusado", erro: "Erro" };
const ORIGEM_TXT = { claude: "Claude", robo: "Robô", manual: "Manual" };
const LINHA_TXT = { noticia: "Notícia", temporada: "Temporada", rodada: "Rodada", resultado: "Resultado", tabela: "Tabela", jogador: "Jogador", manual: "Manual" };

const S = { token: null, view: "fila", posts: [], sel: new Set(), atual: null, slide: 0, perfis: [], resumo: {} };
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ico = (id) => `<svg><use href="#${id}"/></svg>`;
const dominio = (u) => { try { return new URL(u).hostname.replace(/^www\./, ""); } catch { return u; } };
const isDesk = () => matchMedia("(min-width:861px)").matches;

function lsGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function lsSet(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch {} }

function toast(msg, err) {
  const el = document.createElement("div");
  el.className = "toast" + (err ? " err" : "");
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), err ? 5000 : 2600);
}

async function api(path, opt = {}) {
  const headers = opt.headers || {};
  if (S.token) headers.Authorization = "Bearer " + S.token;
  if (opt.json !== undefined) { headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(opt.json); }
  let r;
  try { r = await fetch(path, { ...opt, headers }); }
  catch { throw new Error("Sem conexão com o servidor"); }
  let data = null;
  try { data = await r.json(); } catch {}
  if (r.status === 401 && path !== "/api/login") { sair(true); throw new Error("Sessão expirada"); }
  if (!r.ok) throw new Error((data && (data.detail || data.erro)) || `Erro ${r.status}`);
  if (data === null) throw new Error("Resposta inválida do servidor (ele pode estar reiniciando)");
  return data;
}

function quando(iso) {
  if (!iso) return "";
  const d = new Date(iso), s = (Date.now() - d) / 1000;
  if (s > -60 && s < 60) return "agora";
  if (s >= 60 && s < 3600) return `há ${Math.floor(s / 60)} min`;
  if (s >= 3600 && s < 86400) return `há ${Math.floor(s / 3600)} h`;
  return d.toLocaleString("pt-BR", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
}

/* ------------------------------------------------------------ login */
function mostrarLogin() { $("#app").classList.add("hidden"); $("#login").classList.remove("hidden"); $("#senha").focus(); }
function sair(silencioso) {
  if (!silencioso && S.token) api("/api/logout", { method: "POST" }).catch(() => {});
  S.token = null; lsSet("dkc_token", null); mostrarLogin();
}
$("#loginForm").addEventListener("submit", async (e) => {
  e.preventDefault(); $("#loginErr").textContent = "";
  try {
    const d = await api("/api/login", { method: "POST", json: { senha: $("#senha").value } });
    S.token = d.token; lsSet("dkc_token", d.token); $("#senha").value = ""; iniciar();
  } catch (err) { $("#loginErr").textContent = err.message; }
});

/* ------------------------------------------------------------ navegação */
function montarNav() {
  const pend = S.resumo.pendente || 0, err = S.resumo.erro || 0;
  $("#navSide").innerHTML = Object.entries(VIEWS).map(([k, v]) => {
    let b = "";
    if (k === "fila" && pend) b = `<span class="badge">${pend}</span>`;
    if (k === "recusados" && err) b = `<span class="badge" style="background:var(--red);color:#0A0A0A">${err}</span>`;
    return `<button data-view="${k}" class="${S.view === k ? "on" : ""}">${ico(v.icon)}${v.nav || v.t}${b}</button>`;
  }).join("");
  $("#navMob").innerHTML = Object.entries(VIEWS).filter(([, v]) => v.mob).map(([k, v]) =>
    `<button data-view="${k}" class="${S.view === k ? "on" : ""}">${ico(v.icon)}${v.mob}${k === "fila" && pend ? `<span class="badge">${pend}</span>` : ""}</button>`
  ).join("");
  $("#perfisSide").innerHTML = S.perfis.length
    ? S.perfis.map((p) => `<div class="perfil"><span class="dot"></span>@${esc(p.handle)}</div>`).join("")
    : `<div class="perfil muted">Nenhum perfil. Vá em Ajustes.</div>`;
}

function irPara(view) {
  S.view = view; S.sel.clear(); atualizarLote();
  const v = VIEWS[view];
  $("#viewTitle").textContent = v.t;
  if (view === "ajustes") $("#viewCount").textContent = "";
  $("#ajustes").classList.toggle("hidden", view !== "ajustes");
  $("#lista").classList.toggle("hidden", view === "ajustes");
  $("#stats").classList.toggle("hidden", view !== "fila");
  montarNav();
  if (view === "ajustes") renderAjustes(); else carregar();
}

document.addEventListener("click", (e) => {
  const v = e.target.closest("[data-view]");
  if (v) { irPara(v.dataset.view); return; }
  const a = e.target.closest("[data-act]");
  if (!a) return;
  const act = a.dataset.act, id = a.dataset.id;
  if (act === "atualizar") carregar(true);
  else if (act === "novo") abrirNovo();
  else if (act === "novo-fechar") fecharNovo();
  else if (act === "fechar") fecharDetalhe();
  else if (act === "lote-limpar") { S.sel.clear(); renderLista(); }
  else if (act === "lote-aprovar") aprovarLote();
  else if (act === "lote-recusar") recusarLote();
  else if (act === "aprovar") { e.stopPropagation(); aprovar(id); }
  else if (act === "recusar") { e.stopPropagation(); recusar(id); }
  else if (act === "editar") { e.stopPropagation(); abrirDetalhe(id); }
  else if (act === "sel") { e.stopPropagation(); S.sel.has(id) ? S.sel.delete(id) : S.sel.add(id); renderLista(); }
  else if (act === "slide") { S.slide += +a.dataset.d; renderPreview(); }
  else if (act === "push-ativar") ativarPush();
  else if (act === "push-teste") testarPush();
  else if (act === "sincronizar") sincronizar();
  else if (act === "pauta") buscarPauta();
  else if (act === "auto") alternarAuto();
  else if (act === "rodada") gerarRodada();
  else if (act === "not-ligar") ligarNoticias();
  else if (act === "not-rodar") rodarNoticias();
  else if (act === "res-ligar") ligarResultados();
  else if (act === "sair") sair();
});

/* ------------------------------------------------------------ dados */
async function carregarResumo() {
  try { const r = await api("/api/resumo"); S.resumo = r.por_status; S.resumoDia = r; } catch {}
}

async function carregar(manual) {
  const v = VIEWS[S.view];
  if (!v.status) return;
  try {
    const [d] = await Promise.all([api(`/api/posts?status=${v.status}`), carregarResumo()]);
    S.posts = d.posts;
    for (const id of [...S.sel]) if (!S.posts.find((p) => p.id === id)) S.sel.delete(id);
    renderStats(); renderLista(); montarNav();
    if (manual) toast("Atualizado");
  } catch (err) { toast(err.message, true); }
}

function renderStats() {
  const r = S.resumo || {}, d = S.resumoDia || {};
  $("#stats").innerHTML = [
    ["Na fila", r.pendente || 0, true], ["Agendados", (r.agendado || 0) + (r.publicando || 0)],
    ["Publicados hoje", d.publicados_hoje || 0], ["Com erro", r.erro || 0],
  ].map(([t, n, hl]) => `<div class="stat ${hl ? "hl" : ""}"><b>${n}</b><span>${t}</span></div>`).join("");
}

function thumbHTML(p) {
  const m = p.midias[0];
  if (!m) return `<div class="thumb"></div>`;
  const multi = p.midias.length > 1 ? `<span class="multi">1/${p.midias.length}</span>` : "";
  if (m.mime.startsWith("video/")) return `<div class="thumb"><video src="${esc(m.url)}#t=0.5" muted playsinline preload="metadata"></video>${multi}</div>`;
  return `<div class="thumb" style="background-image:url('${esc(m.url)}')">${multi}</div>`;
}

function renderLista() {
  const el = $("#lista");
  $("#viewCount").textContent = S.posts.length ? `${S.posts.length}` : "";
  if (!S.posts.length) {
    const txt = { fila: ["Fila zerada", "Quando um robô ou você criar um post, ele aparece aqui e você recebe uma notificação."],
      agendados: ["Nada agendado", "Posts aprovados com horário aparecem aqui."],
      publicados: ["Nenhum post publicado ainda", ""], recusados: ["Nada por aqui", "Posts recusados ou com erro aparecem aqui."] }[S.view];
    el.innerHTML = `<div class="vazio">${ico(VIEWS[S.view].icon)}<b>${txt[0]}</b>${txt[1]}</div>`;
    atualizarLote(); return;
  }
  const fila = S.view === "fila";
  el.innerHTML = S.posts.map((p) => {
    const sel = S.sel.has(p.id);
    const quandoTxt = p.status === "agendado" ? "sai " + quando(p.agendar_para)
      : p.status === "publicado" ? quando(p.publicado_em) : quando(p.criado_em);
    const acts = fila ? `<div class="acts">
        <button class="btn danger" data-act="recusar" data-id="${p.id}">${ico("i-x")}Recusar</button>
        <button class="btn primary" data-act="aprovar" data-id="${p.id}">${ico("i-check")}Aprovar</button></div>` : "";
    return `<article class="card ${sel ? "sel" : ""}" data-open="${p.id}">
      ${thumbHTML(p)}
      ${fila ? `<span class="selbox" data-act="sel" data-id="${p.id}">${ico("i-check")}</span>` : ""}
      <div class="body">
        <div class="meta"><span class="chip ${p.status}">${STATUS_TXT[p.status]}</span><span class="chip">${LINHA_TXT[p.linha] || p.linha}</span>${p.origem && p.origem !== "manual" ? `<span class="chip">${esc(ORIGEM_TXT[p.origem] || p.origem)}</span>` : ""}<span class="time">${quandoTxt}</span></div>
        <h3>${esc(p.titulo || "Sem título")}</h3>
        ${(p.fonte_urls || []).length ? `<a class="link-fonte sm" href="${esc(p.fonte_urls[0])}" target="_blank" rel="noopener noreferrer" data-ext="1">${ico("i-ext")}Ver fonte · ${esc(dominio(p.fonte_urls[0]))}</a>` : ""}
        <p>${esc(p.legenda)}</p>
        ${p.erro ? `<p style="color:var(--red)">${esc(p.erro)}</p>` : ""}
      </div>${acts}</article>`;
  }).join("");
  el.querySelectorAll("video").forEach((v) => { v.addEventListener("click", (e) => e.preventDefault()); });
  atualizarLote();
}

$("#lista").addEventListener("click", (e) => {
  if (e.target.closest("[data-act]") || e.target.closest("[data-ext]")) return;
  const c = e.target.closest("[data-open]");
  if (c) abrirDetalhe(c.dataset.open);
});

function atualizarLote() {
  const n = S.sel.size;
  $("#loteBar").classList.toggle("hidden", n === 0);
  $("#loteN").textContent = n === 1 ? "1 selecionado" : `${n} selecionados`;
}

/* ------------------------------------------------------------ ações */
const horaCurta = (iso) => new Date(iso).toLocaleString("pt-BR", { weekday: "short", hour: "2-digit", minute: "2-digit" });
async function aprovar(id, agendarIso, agora = false) {
  const p = S.posts.find((x) => x.id === id) || S.atual;
  if (p && !p.perfil) { toast("Esse post não tem perfil. Sincronize os perfis em Ajustes.", true); return; }
  try {
    const r = await api(`/api/posts/${id}/aprovar`, { method: "POST", json: { agendar_para: agendarIso || null, agora } });
    toast(r.sai_em ? `Aprovado · sai ${horaCurta(r.sai_em)}` : "Aprovado, enviando para o Instagram agora");
    fecharDetalhe(); S.sel.delete(id); carregar();
  } catch (err) { toast(err.message, true); carregar(); }
}

async function aprovarLote() {
  const ids = [...S.sel];
  if (!ids.length) return;
  if (!confirm(`Aprovar ${ids.length} post(s)? Eles entram na fila, um a cada horário (:00 e :30).`)) return;
  try {
    const r = await api("/api/posts/aprovar-lote", { method: "POST", json: { ids } });
    const oks = r.resultados.filter((x) => x.ok);
    const ult = oks.map((x) => x.sai_em).filter(Boolean).sort().pop();
    toast(`${oks.length} de ${ids.length} aprovados${ult ? ` · o último sai ${horaCurta(ult)}` : ""}`);
  } catch (err) { toast(err.message, true); }
  S.sel.clear(); carregar();
}

async function recusarLote() {
  const ids = [...S.sel];
  if (!ids.length) return;
  if (!confirm(`Recusar ${ids.length} post(s)?`)) return;
  try {
    const r = await api("/api/posts/recusar-lote", { method: "POST", json: { ids } });
    toast(`${r.recusados} recusado(s)`);
  } catch (err) { toast(err.message, true); }
  S.sel.clear(); carregar();
}

async function recusar(id) {
  const motivo = prompt("Motivo da recusa (opcional):", "");
  if (motivo === null) return;
  try {
    await api(`/api/posts/${id}/recusar`, { method: "POST", json: { motivo } });
    toast("Recusado"); fecharDetalhe(); carregar();
  } catch (err) { toast(err.message, true); carregar(); }
}

async function salvarEdicao(id) {
  try {
    await api(`/api/posts/${id}`, { method: "PATCH", json: { legenda: $("#dLegenda").value, titulo: $("#dTitulo").value } });
    return true;
  } catch (err) { toast(err.message, true); return false; }
}

/* ------------------------------------------------------------ detalhe */
async function abrirDetalhe(id) {
  let p = S.posts.find((x) => x.id === id);
  if (!p) { try { p = await api(`/api/posts/${id}`); } catch (err) { toast(err.message, true); return; } }
  S.atual = p; S.slide = 0;
  const editavel = ["pendente", "erro"].includes(p.status);
  $("#dStatus").className = "chip " + p.status; $("#dStatus").textContent = STATUS_TXT[p.status];
  $("#dPerfil").textContent = p.perfil ? "@" + p.perfil : "sem perfil";
  $("#dTitulo").value = p.titulo; $("#dTitulo").readOnly = !editavel;
  $("#dLegenda").value = p.legenda; $("#dLegenda").readOnly = !editavel;
  const links = p.fonte_urls || [];
  $("#dFonteWrap").classList.toggle("hidden", !p.fonte && !links.length);
  $("#dFonte").innerHTML = (p.fonte ? `<div>${esc(p.fonte)}</div>` : "") +
    links.map((u) => `<a class="link-fonte" href="${esc(u)}" target="_blank" rel="noopener noreferrer">${ico("i-ext")}${esc(dominio(u))}</a>`).join("");
  $("#dErroWrap").classList.toggle("hidden", !p.erro); $("#dErro").textContent = p.erro || "";
  $("#dAgendaWrap").classList.toggle("hidden", !editavel); $("#dAgenda").value = "";
  if (p.sugerido_para && editavel) {
    const d = new Date(p.sugerido_para);
    if (d > new Date()) $("#dAgenda").value = new Date(d - d.getTimezoneOffset() * 6e4).toISOString().slice(0, 16);
  }
  contarLegenda(); renderPreview();
  const f = $("#dActions");
  if (p.status === "pendente" || p.status === "erro") {
    f.innerHTML = `<button class="btn danger" id="bRecusar">${ico("i-x")}Recusar</button>
      <button class="btn" id="bSalvar">Salvar texto</button>
      <button class="btn" id="bJa" title="Fura a fila: publica agora (notícia urgente)">Publicar já</button>
      <button class="btn primary wide" id="bAprovar">${ico("i-check")}<span id="bAprovarTxt">Aprovar (entra na fila)</span></button>`;
    api("/api/fila/proximo").then((r) => { const t = $("#bAprovarTxt"); if (t && !$("#dAgenda").value) t.textContent = r.sai_em ? `Aprovar · sai ${horaCurta(r.sai_em)}` : "Aprovar · sai agora"; }).catch(() => {});
    $("#bJa").onclick = async () => {
      if ((p.legenda !== $("#dLegenda").value || p.titulo !== $("#dTitulo").value) && !(await salvarEdicao(p.id))) return;
      aprovar(p.id, null, true);
    };
    $("#bRecusar").onclick = () => recusar(p.id);
    $("#bSalvar").onclick = async () => { if (await salvarEdicao(p.id)) { toast("Texto salvo"); carregar(); } };
    $("#bAprovar").onclick = async () => {
      const ag = $("#dAgenda").value ? new Date($("#dAgenda").value).toISOString() : null;
      if ((p.legenda !== $("#dLegenda").value || p.titulo !== $("#dTitulo").value) && !(await salvarEdicao(p.id))) return;
      aprovar(p.id, ag);
    };
  } else if (p.status === "recusado") {
    f.innerHTML = `<button class="btn wide" id="bVoltar">${ico("i-undo")}Voltar para a fila</button>`;
    $("#bVoltar").onclick = async () => { try { await api(`/api/posts/${p.id}/voltar-fila`, { method: "POST" }); toast("De volta à fila"); fecharDetalhe(); carregar(); } catch (err) { toast(err.message, true); } };
  } else f.innerHTML = "";
  $("#detail").classList.add("open"); $("#detail").setAttribute("aria-hidden", "false");
  $("#scrim").classList.add("on");
  history.replaceState(null, "", "/?post=" + p.id);
}

function fecharDetalhe() {
  $("#detail").classList.remove("open"); $("#detail").setAttribute("aria-hidden", "true");
  $("#scrim").classList.remove("on"); S.atual = null;
  if (location.search) history.replaceState(null, "", "/");
}

function renderPreview() {
  const p = S.atual; if (!p) return;
  const n = p.midias.length;
  S.slide = Math.max(0, Math.min(S.slide, n - 1));
  const m = p.midias[S.slide];
  const media = !m ? "" : m.mime.startsWith("video/")
    ? `<video src="${esc(m.url)}" controls playsinline></video>` : `<img src="${esc(m.url)}" alt="">`;
  const nav = n > 1 ? `${S.slide > 0 ? `<button class="nav-arrow l" data-act="slide" data-d="-1">${ico("i-left")}</button>` : ""}
    ${S.slide < n - 1 ? `<button class="nav-arrow r" data-act="slide" data-d="1">${ico("i-right")}</button>` : ""}
    <div class="dots">${p.midias.map((_, i) => `<i class="${i === S.slide ? "on" : ""}"></i>`).join("")}</div>` : "";
  $("#dPreview").innerHTML = media + nav;
}

function contarLegenda() {
  const t = $("#dLegenda").value;
  $("#dCount").textContent = `${t.length}/2200`;
  $("#dFold").textContent = t.slice(0, 125) + (t.length > 125 ? "…" : "");
}
$("#dLegenda").addEventListener("input", contarLegenda);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { fecharDetalhe(); fecharNovo(); } });

/* ------------------------------------------------------------ novo post */
let novosArquivos = [];
function abrirNovo() {
  $("#nPerfil").innerHTML = S.perfis.map((p) => `<option value="${p.id}">@${esc(p.handle)}</option>`).join("") || `<option value="">Sem perfil</option>`;
  $("#modalNovo").classList.remove("hidden");
}
function fecharNovo() {
  $("#modalNovo").classList.add("hidden"); $("#formNovo").reset(); novosArquivos = [];
  $("#nThumbs").innerHTML = ""; $("#nArqTxt").textContent = "Escolher imagem, carrossel (até 10) ou vídeo"; $("#nCount").textContent = "0/2200";
}
function setArquivos(files) {
  novosArquivos = [...files].slice(0, 10);
  $("#nArqTxt").textContent = novosArquivos.length ? `${novosArquivos.length} arquivo(s)` : "Escolher imagem, carrossel (até 10) ou vídeo";
  $("#nThumbs").innerHTML = novosArquivos.map((f) => {
    const u = URL.createObjectURL(f);
    return f.type.startsWith("video/") ? `<video src="${u}" muted></video>` : `<img src="${u}" alt="">`;
  }).join("");
}
$("#nArq").addEventListener("change", (e) => setArquivos(e.target.files));
const drop = $("#drop");
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => setArquivos(e.dataTransfer.files));
$("#nLegenda").addEventListener("input", () => { $("#nCount").textContent = `${$("#nLegenda").value.length}/2200`; });
$("#formNovo").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!novosArquivos.length) { toast("Escolha ao menos uma imagem ou vídeo", true); return; }
  const fd = new FormData();
  novosArquivos.forEach((f) => fd.append("arquivos", f));
  fd.append("legenda", $("#nLegenda").value); fd.append("titulo", $("#nTitulo").value);
  fd.append("linha", $("#nLinha").value);
  if ($("#nPerfil").value) fd.append("perfil_id", $("#nPerfil").value);
  $("#nEnviar").disabled = true;
  try { await api("/api/posts", { method: "POST", body: fd }); toast("Post na fila"); fecharNovo(); irPara("fila"); }
  catch (err) { toast(err.message, true); }
  finally { $("#nEnviar").disabled = false; }
});

/* ------------------------------------------------------------ ajustes */
const ehIOS = () => /iphone|ipad|ipod/i.test(navigator.userAgent);
const instalado = () => matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;

async function renderAjustes() {
  const el = $("#ajustes");
  const perm = "Notification" in window ? Notification.permission : "indisponivel";
  const pushTxt = perm === "granted" ? `<span class="ok">Ativadas neste aparelho</span>` : perm === "denied" ? `<span class="nok">Bloqueadas no navegador</span>` : "Desativadas neste aparelho";
  const iosAviso = ehIOS() && !instalado()
    ? `<p>No iPhone, as notificações só funcionam com o app instalado: toque em Compartilhar e depois em <b>Adicionar à Tela de Início</b>. Abra pelo ícone e ative aqui.</p>` : "";
  el.innerHTML = `
    <div class="box"><h3>Notificações</h3><p>Aviso a cada post novo na fila. Ative em cada aparelho (celular e PC).</p>${iosAviso}
      <div class="linha"><span>Status</span>${pushTxt}</div>
      <button class="btn primary" data-act="push-ativar">Ativar neste aparelho</button>
      <button class="btn" data-act="push-teste">Enviar notificação de teste</button></div>
    <div class="box"><h3>Perfis do Instagram</h3><p>Puxa as contas conectadas no Publora.</p>
      <div id="ajPerfis">${S.perfis.map((p) => `<div class="linha"><span>@${esc(p.handle)}</span><span class="muted">${esc(p.publora_platform_id || "")}</span></div>`).join("") || `<div class="linha"><span class="muted">Nenhum perfil ainda</span></div>`}</div>
      <button class="btn" data-act="sincronizar">Sincronizar com o Publora</button></div>
    <div class="box"><h3>Publicação automática</h3><p>Saem <b>sem aprovação</b>: a <b>Rodada do dia</b> (9h), os <b>resultados</b> (cada jogo e o resumo do dia) e a <b>tabela</b> no fim de cada rodada (Brasileirão, Champions, LaLiga, Premier). Se a revisão achar erro na arte, o post para na fila. Notícias sempre passam pela fila.</p>
      <div class="linha"><span>Resultados automáticos</span><span id="ajAuto" class="muted">…</span></div>
      <button class="btn" data-act="auto" id="bAuto">…</button></div>
    <div class="box"><h3>Robô de resultados</h3><p>Jogo dos principais acabou, o card de resultado sai sozinho no Instagram (é o único que não passa pela fila).</p>
      <div id="ajRes"><p>Verificando…</p></div>
      <button class="btn ghost" data-act="res-ligar" id="bRes">…</button></div>
    <div class="box"><h3>Robô de notícias</h3><p>Varre os portais, dá nota para cada notícia e escreve as mais fortes. Tudo cai na fila para você aprovar.</p>
      <div id="ajNot"><p>Verificando…</p></div>
      <button class="btn" data-act="not-rodar">Procurar notícias agora</button>
      <button class="btn ghost" data-act="not-ligar" id="bNot">…</button></div>
    <div class="box"><h3>Robô da Rodada do dia</h3><p>Todo dia às 7h monta o carrossel com os jogos das ligas prioritárias e manda para a fila, com sugestão de publicar às 9h.</p>
      <div class="linha"><span>API-Football</span><span id="ajApiF" class="muted">…</span></div>
      <button class="btn" data-act="rodada">Gerar a rodada de hoje agora</button></div>
    <div class="box"><h3>Pauta do Claude</h3><p>Posts que o Claude prepara no chat chegam por aqui sozinhos.</p>
      <div id="ajPauta"><p>Verificando…</p></div>
      <button class="btn" data-act="pauta">Buscar pauta agora</button></div>
    <div class="box"><h3>Saúde do sistema</h3><div id="ajSaude"><p>Verificando…</p></div></div>
    <div class="box"><h3>Sessão</h3><p>Sair deste aparelho.</p><button class="btn danger" data-act="sair">Sair</button></div>`;
  try {
    const s = await api("/api/saude");
    const af = $("#ajApiF"); if (af) af.innerHTML = s.api_football ? `<span class="ok">Conectada</span>` : `<span class="nok">Falta a chave</span>`;
    $("#ajSaude").innerHTML = `
      <div class="linha"><span>Publora</span>${s.publora.ok ? `<span class="ok">Conectado (${s.publora.conexoes} conta${s.publora.conexoes === 1 ? "" : "s"})</span>` : `<span class="nok">${esc(s.publora.erro)}</span>`}</div>
      <div class="linha"><span>Aparelhos com notificação</span><span>${s.push_inscritos}</span></div>
      <div class="linha"><span>API-Football</span>${s.api_football ? `<span class="ok">Chave configurada</span>` : `<span class="nok">Falta API_FOOTBALL_KEY</span>`}</div>
      <div class="linha"><span>Robôs</span>${s.robo_configurado ? `<span class="ok">Chave configurada</span>` : `<span class="muted">Sem chave (fatia 3)</span>`}</div>
      <div class="linha"><span>Endereço público</span><span class="muted">${esc(s.base_publica)}</span></div>`;
  } catch (err) { $("#ajSaude").innerHTML = `<p class="nok">${esc(err.message)}</p>`; }
  try {
    const a = await api("/api/config/auto-resultado"); S.auto = a.ligado;
    $("#ajAuto").innerHTML = a.ligado ? `<span class="ok">Ligado</span>` : `<span class="nok">Desligado</span>`;
    $("#bAuto").textContent = a.ligado ? "Desligar (tudo passa pela fila)" : "Ligar";
  } catch {}
  try {
    const r = await api("/api/robos/resultados"); S.res = r;
    $("#ajRes").innerHTML = `
      <div class="linha"><span>Status</span>${!r.tem_chave ? `<span class="nok">Falta API_FOOTBALL_KEY</span>` : r.ligado ? `<span class="ok">Ligado · a cada ${r.intervalo_min} min</span>` : `<span class="nok">Desligado</span>`}</div>
      <div class="linha"><span>Hoje</span><span>${r.postados_hoje} resultado${r.postados_hoje === 1 ? "" : "s"}${r.teto_dia ? ` de ${r.teto_dia}` : " · sem limite"}</span></div>
      <div class="linha"><span>Última conferida</span><span class="muted">${r.ultima ? quando(r.ultima) : "ainda não"}</span></div>
      ${r.erro ? `<div class="linha"><span>Erro</span><span class="nok">${esc(r.erro)}</span></div>` : ""}`;
    $("#bRes").textContent = r.ligado ? "Desligar robô" : "Ligar robô";
  } catch (err) { const el = $("#ajRes"); if (el) el.innerHTML = `<p class="nok">${esc(err.message)}</p>`; }
  try {
    const n = await api("/api/robos/noticias"); S.not = n;
    const tk = n.uso_hoje.in + n.uso_hoje.out;
    $("#ajNot").innerHTML = `
      <div class="linha"><span>Status</span>${!n.tem_chave ? `<span class="nok">Falta ANTHROPIC_API_KEY</span>` : n.ligado ? `<span class="ok">Ligado · a cada ${n.intervalo_min} min</span>` : `<span class="nok">Desligado</span>`}</div>
      <div class="linha"><span>Hoje</span><span>${n.postados_hoje} notícia${n.postados_hoje === 1 ? "" : "s"}${n.teto_dia ? ` de ${n.teto_dia}` : " · sem limite"}</span></div>
      <div class="linha"><span>Portais respondendo</span><span class="${n.feeds_total && n.feeds_ok < n.feeds_total / 2 ? "nok" : "muted"}">${n.feeds_total ? `${n.feeds_ok} de ${n.feeds_total}` : "ainda não rodou"}</span></div>
      <div class="linha"><span>Uso da IA hoje</span><span class="muted">${Math.round(tk / 1000)} mil de ${Math.round(n.limite_tokens / 1000)} mil tokens</span></div>
      <div class="linha"><span>Última varredura</span><span class="muted">${n.ultima ? quando(n.ultima) : "ainda não"}</span></div>
      ${n.erro ? `<div class="linha"><span>Erro</span><span class="nok">${esc(n.erro)}</span></div>` : ""}
      ${n.assuntos_24h.length ? `<div class="side-title" style="padding:10px 0 0">Assuntos das últimas 24h</div>` + n.assuntos_24h.map((a) => `<div class="linha"><span>${esc(a.titulo)}</span><span class="${a.nota >= 6 ? "ok" : "muted"}" title="${a.n_fontes} fonte(s) · ${esc(a.status)}">${(+a.nota).toFixed(0)}</span></div>`).join("") : ""}`;
    $("#bNot").textContent = n.ligado ? "Desligar robô" : "Ligar robô";
  } catch (err) { const el = $("#ajNot"); if (el) el.innerHTML = `<p class="nok">${esc(err.message)}</p>`; }
  try {
    const p = await api("/api/pauta/status");
    const ult = p.importados.slice(0, 5).map((i) => `<div class="linha"><span>${esc(i.path.replace(/^fila\//, ""))}</span>${i.status === "ok" ? `<span class="ok">na fila</span>` : `<span class="nok" title="${esc(i.erro || "")}">erro</span>`}</div>`).join("");
    $("#ajPauta").innerHTML = `
      <div class="linha"><span>Última checagem</span><span class="muted">${p.ultima_checagem ? quando(p.ultima_checagem) : "ainda não"}</span></div>
      <div class="linha"><span>Frequência</span><span class="muted">a cada ${Math.round(p.intervalo_s / 60 * 10) / 10} min</span></div>
      ${p.erro ? `<div class="linha"><span>Erro</span><span class="nok">${esc(p.erro)}</span></div>` : ""}${ult}`;
  } catch (err) { $("#ajPauta").innerHTML = `<p class="nok">${esc(err.message)}</p>`; }
}

async function ligarResultados() {
  try { await api("/api/robos/resultados/ligar", { method: "POST", json: { ligado: !(S.res && S.res.ligado) } }); renderAjustes(); }
  catch (err) { toast(err.message, true); }
}
async function ligarNoticias() {
  try { await api("/api/robos/noticias/ligar", { method: "POST", json: { ligado: !(S.not && S.not.ligado) } }); renderAjustes(); }
  catch (err) { toast(err.message, true); }
}
async function rodarNoticias() {
  try {
    toast("Procurando notícias… pode levar 1 a 2 minutos");
    const r = await api("/api/robos/noticias/rodar", { method: "POST" });
    toast(r.ok ? `${r.novas} manchetes novas · ${r.posts} post(s) na fila` : r.motivo, !r.ok);
    renderAjustes(); carregarResumo().then(montarNav);
  } catch (err) { toast(err.message, true); }
}

async function gerarRodada() {
  try {
    toast("Montando a rodada…");
    let r = await api("/api/robos/rodada", { method: "POST", json: {} });
    if (!r.ok && r.motivo && confirm(r.motivo + ". Gerar de novo mesmo assim?")) r = await api("/api/robos/rodada", { method: "POST", json: { forcar: true } });
    if (r.ok) toast(r.jogos ? `Rodada na fila: ${r.jogos} jogos em ${r.slides} slides` : r.motivo);
    carregarResumo().then(montarNav);
  } catch (err) { toast(err.message, true); }
}

async function alternarAuto() {
  try {
    await api("/api/config/auto-resultado", { method: "POST", json: { ligado: !S.auto } });
    toast(!S.auto ? "Resultados automáticos ligados" : "Agora tudo passa pela fila"); renderAjustes();
  } catch (err) { toast(err.message, true); }
}

async function buscarPauta() {
  try {
    const r = await api("/api/pauta/buscar", { method: "POST" });
    toast(r.novos ? `${r.novos} post(s) novo(s) na fila` : r.erros ? `${r.erros} com erro` : "Nada novo na pauta", !!r.erros && !r.novos);
    renderAjustes(); carregarResumo().then(montarNav);
  } catch (err) { toast(err.message, true); }
}

async function sincronizar() {
  try {
    const r = await api("/api/perfis/sincronizar", { method: "POST" });
    toast(`${r.sincronizados} perfil(is) sincronizado(s)`);
    await carregarPerfis(); if (S.view === "ajustes") renderAjustes();
  } catch (err) { toast(err.message, true); }
}
async function carregarPerfis() { try { S.perfis = (await api("/api/perfis")).perfis; montarNav(); } catch {} }

/* ------------------------------------------------------------ push */
function b64ToU8(b64) {
  const p = "=".repeat((4 - (b64.length % 4)) % 4), s = atob((b64 + p).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from([...s].map((c) => c.charCodeAt(0)));
}
async function ativarPush() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
    toast(ehIOS() ? "Instale o app na Tela de Início primeiro" : "Este navegador não suporta notificações", true); return;
  }
  try {
    const perm = await Notification.requestPermission();
    if (perm !== "granted") { toast("Permissão negada. Libere nas configurações do navegador.", true); renderAjustes(); return; }
    const reg = await navigator.serviceWorker.ready;
    const { publicKey } = await api("/api/push/chave");
    let sub = await reg.pushManager.getSubscription();
    if (sub) {
      const atual = sub.options && sub.options.applicationServerKey;
      const mesma = atual && btoa(String.fromCharCode(...new Uint8Array(atual))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "") === publicKey;
      if (!mesma) { await sub.unsubscribe(); sub = null; }
    }
    if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToU8(publicKey) });
    await api("/api/push/inscrever", { method: "POST", json: sub.toJSON() });
    toast("Notificações ativadas"); renderAjustes();
  } catch (err) { toast("Falha ao ativar: " + err.message, true); }
}
async function testarPush() {
  try { const r = await api("/api/push/teste", { method: "POST" }); toast(r.enviadas ? `Enviada para ${r.enviadas} aparelho(s)` : "Nenhum aparelho inscrito ainda", !r.enviadas); }
  catch (err) { toast(err.message, true); }
}
async function reinscreverSilencioso() {
  try {
    if (!("serviceWorker" in navigator) || Notification.permission !== "granted") return;
    const reg = await navigator.serviceWorker.ready, sub = await reg.pushManager.getSubscription();
    if (sub) await api("/api/push/inscrever", { method: "POST", json: sub.toJSON() });
  } catch {}
}

/* ------------------------------------------------------------ boot */
async function iniciar() {
  $("#login").classList.add("hidden"); $("#app").classList.remove("hidden");
  await carregarPerfis();
  irPara("fila");
  reinscreverSilencioso();
  const alvo = new URLSearchParams(location.search).get("post");
  if (alvo) abrirDetalhe(alvo);
}

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
  navigator.serviceWorker.addEventListener("message", (e) => {
    if (e.data && e.data.tipo === "abrir" && e.data.url) {
      const id = new URL(e.data.url, location.origin).searchParams.get("post");
      carregar(); if (id) abrirDetalhe(id);
    }
  });
}
document.addEventListener("visibilitychange", () => { if (!document.hidden && S.token && S.view !== "ajustes") carregar(); });
setInterval(() => { if (!document.hidden && S.token && S.view !== "ajustes") carregar(); }, 30000);

S.token = lsGet("dkc_token");
S.token ? iniciar() : mostrarLogin();
