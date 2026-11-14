'use strict';
/* Painel XHAKINHA CLIPS AI — DOM seguro (textContent), sem innerHTML com dados. */
const $app = document.getElementById('app');
const H = { 'X-Requested-With': 'XHK' };
let tab = (location.hash || '#painel').slice(1), timer = null, me = null;

function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === 'class') e.className = v; else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (v === true) e.setAttribute(k, ''); else if (v !== false && v != null) e.setAttribute(k, v);
  }
  for (const k of kids.flat()) if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k)));
  return e;
}
async function api(path, opt = {}) {
  const o = { credentials: 'same-origin', headers: { ...H, 'Content-Type': 'application/json' }, ...opt };
  if (o.body && typeof o.body !== 'string' && !(o.body instanceof File)) o.body = JSON.stringify(o.body);
  const r = await fetch(path, o);
  let j = null; try { j = await r.json(); } catch (e) {}
  if (r.status === 401 && path !== '/api/login') { me = null; render(); throw new Error('sessão expirada'); }
  if (!r.ok) throw new Error((j && j.error) || ('HTTP ' + r.status));
  return j;
}
const fmtT = t => t ? new Date(t * 1000).toLocaleString('pt-BR', { dateStyle: 'short', timeStyle: 'short' }) : '—';
const fmtB = b => b > 1e9 ? (b / 1e9).toFixed(1) + ' GB' : (b / 1e6).toFixed(0) + ' MB';
const pill = (t, c) => h('span', { class: 'pill ' + (c || '') }, t);
const stc = s => ({ published: 'ok', processed: 'ok', awaiting: '', scheduled: 'warn', failed: 'bad', publish_failed: 'bad', retry_wait: 'warn', dead: 'bad' }[s] || '');
function toast(m, bad) { const t = h('div', { class: 'card', style: 'position:fixed;top:60px;left:12px;right:12px;z-index:20;border-color:' + (bad ? '#ff5c6c' : '#2ecc8f') }, m); document.body.append(t); setTimeout(() => t.remove(), 3500); }
async function act(fn, ok) { try { await fn(); if (ok) toast(ok); refresh(); } catch (e) { toast(e.message, true); } }

const TABS = [['painel', '📊', 'Painel'], ['fontes', '🔌', 'Fontes'], ['clipes', '🎬', 'Clipes'], ['pubs', '🚀', 'Posts'], ['falhas', '⚠️', 'Falhas'], ['config', '⚙️', 'Config'], ['logs', '📜', 'Logs']];

function render() {
  if (!me) return renderLogin();
  $app.replaceChildren(
    h('header', {}, h('h1', {}, 'XHAKINHA CLIPS AI'), h('span', { id: 'eng' })),
    h('main', { id: 'main' }),
    h('nav', {}, TABS.map(([k, i, n]) => h('button', { class: k === tab ? 'on' : '', onclick: () => { tab = k; location.hash = k; render(); } }, i, h('br'), n))));
  refresh(); clearInterval(timer); timer = setInterval(() => { if (tab === 'painel' || tab === 'pubs' || tab === 'falhas') refresh(); }, 6000);
}
function renderLogin() {
  const u = h('input', { placeholder: 'usuário', autocomplete: 'username' }), p = h('input', { type: 'password', placeholder: 'senha', autocomplete: 'current-password' });
  const go = () => act(async () => { await api('/api/login', { method: 'POST', body: { username: u.value, password: p.value } }); me = u.value; render(); });
  p.addEventListener('keydown', e => e.key === 'Enter' && go());
  $app.replaceChildren(h('div', { class: 'card login' }, h('h2', {}, 'XHAKINHA CLIPS AI'), u, p, h('button', { class: 'b', style: 'width:100%', onclick: go }, 'Entrar')));
}
async function refresh() {
  const m = document.getElementById('main'); if (!m) return;
  try { const v = await ({ painel: vPainel, fontes: vFontes, clipes: vClipes, pubs: vPubs, falhas: vFalhas, config: vConfig, logs: vLogs }[tab])(); if (v) m.replaceChildren(...v); } catch (e) { if (m) m.replaceChildren(h('div', { class: 'card err' }, e.message)); }
}

/* ---------------- Painel ---------------- */
let rtTimer = null;
async function vPainel() {
  const s = await api('/api/status'), st = s.storage, eng = document.getElementById('eng');
  if (eng) eng.replaceChildren(pill(s.engine === 'running' ? '● rodando' : s.engine === 'paused' ? '⏸ pausado' : '■ parado', s.engine === 'running' ? 'ok' : 'warn'));
  const d = s.deps, warn = [];
  if (!d.ffmpeg) warn.push('FFmpeg ausente'); if (!d.yt_dlp.ok) warn.push('yt-dlp: ' + (d.yt_dlp.error || 'indisponível'));
  if (!d.whisper) warn.push('Transcrição local (faster-whisper) não instalada: análise usa só áudio+vídeo'); if (!d.face_detector) warn.push('Detector de rosto indisponível: enquadramento central');
  if (!d.public_url_https) warn.push('PUBLIC_BASE_URL https ausente: Instagram não poderá publicar');
  const rt = h('div', { id: 'rt' });
  const up = h('input', { type: 'file', accept: 'video/*' }), ck = h('input', { type: 'checkbox' });
  const kp = (b, t) => h('div', { class: 'kpi' }, h('b', {}, b), h('span', {}, t));
  const cards = [
    h('div', { class: 'card' }, h('div', { class: 'row' },
      ...['start', 'pause', 'resume', 'stop'].map(a => h('button', { class: 'b sec', onclick: () => act(() => api('/api/control', { method: 'POST', body: { action: a } }), 'OK') }, { start: '▶ Iniciar', pause: '⏸ Pausar', resume: '⏯ Retomar', stop: '■ Parar' }[a])))),
    h('div', { class: 'card' }, h('button', { class: 'b big', id: 'rtbtn', onclick: startRT }, 'TESTAR PROCESSAMENTO REAL'), h('p', { class: 'muted' }, 'Pega 1 vídeo real da sua fonte autorizada, gera 1 clipe e envia de verdade (TikTok em modo privado; Instagram só após sua confirmação).'), rt),
    warn.length ? h('div', { class: 'card' }, h('h3', {}, 'Atenção'), warn.map(w => h('p', { class: 'err' }, '• ' + w))) : null,
    ...Object.entries(s.platforms).map(([p, t]) => h('div', { class: 'card' }, h('h3', {}, p + ' — hoje ', pill(t.connected ? 'conectado' : 'NÃO conectado', t.connected ? 'ok' : 'bad')),
      h('div', { class: 'grid' }, kp(t.published, 'publicados'), kp(t.target, 'meta de hoje'), kp(`${t.min}/${t.normal}/${t.max}`, 'mín/normal/máx'), kp(t.scheduled, 'agendados'), kp(t.stock, 'no estoque'), kp(t.produced, 'produzidos (todas)')),
      t.reasons.length ? h('p', { class: 'muted' }, 'Ajuste: ' + t.reasons.join('; ')) : null, h('p', { class: 'muted' }, 'Próximos: ' + (t.next.map(fmtT).join(' · ') || '—')))),
    h('div', { class: 'card' }, h('h3', {}, 'Sistema'), h('div', { class: 'grid' }, kp(s.cpu + '%', 'CPU'), kp(s.mem.percent + '%', 'Memória'), kp(`${s.workers.busy.length}/${s.workers.target}`, 'workers ativos/alvo'), kp(s.queue.pending, 'fila'), kp(s.queue.running, 'rodando'), kp(s.failed_jobs, 'falhas (fila de falhas)')),
      h('p', { class: 'muted' }, `Disco livre ${fmtB(st.disk_free)} · produção ${fmtB(st.production_bytes)} · clipes ${fmtB(st.clips_bytes)} · backups ${fmtB(st.backups_bytes)}`), h('div', { class: 'bar' }, h('i', { style: `width:${Math.min(100, 100 * (1 - st.disk_free / st.disk_total)).toFixed(0)}%` })),
      h('p', { class: 'muted' }, `Cota YouTube hoje: ${s.quota_youtube.used}/${s.quota_youtube.limit}`)),
    h('div', { class: 'card' }, h('h3', {}, 'Acervo antigo'), h('div', { class: 'grid' }, kp(s.archive.found, 'encontrados'), kp(s.archive.analyzed, 'analisados'), kp(s.archive.clips, 'clipes gerados'), kp(s.archive.awaiting, 'aguardando')), h('p', { class: 'muted' }, 'Já aproveitados: ' + (s.archive.used.join(' | ') || '—'))),
    h('div', { class: 'card' }, h('h3', {}, 'Aprendizado por métricas'), h('p', { class: 'muted' }, s.learning.active ? `Ativo com ${s.learning.samples} amostras reais.` : `Inativo: ${s.learning.samples}/${s.learning.min_samples} amostras reais (não finge aprender).`)),
    h('div', { class: 'card' }, h('h3', {}, 'Enviar vídeo (upload)'), up, h('label', { class: 'ck' }, ck, 'Declaro que o vídeo é meu ou tenho autorização para cortá-lo e republicá-lo'),
      h('button', { class: 'b', onclick: () => act(async () => { if (!up.files[0]) throw new Error('escolha um arquivo'); const r = await fetch('/api/upload?name=' + encodeURIComponent(up.files[0].name), { method: 'PUT', headers: { ...H, 'X-Authorized': ck.checked ? '1' : '0' }, body: up.files[0], credentials: 'same-origin' }); const j = await r.json(); if (!r.ok) throw new Error(j.error); }, 'Upload enviado — processando') }, 'Enviar')),
  ];
  setTimeout(pollRT, 50);
  return cards;
}
async function startRT() { try { await api('/api/realtest', { method: 'POST', body: {} }); toast('Teste iniciado'); pollRT(); } catch (e) { toast(e.message, true); } }
async function pollRT() {
  const box = document.getElementById('rt'); if (!box) return;
  const r = await api('/api/realtest/latest').catch(() => null); if (!r || !r.id) return;
  const res = r.result || {}, kids = [h('p', {}, pill(r.status, r.status === 'done' ? 'ok' : r.status === 'failed' ? 'bad' : 'warn'), ' ', res.elapsed_s ? res.elapsed_s + 's' : '')];
  for (const s of r.steps) kids.push(h('div', { class: 'step' }, s.ok === true ? '✅' : s.ok === false ? '❌' : '⏳', h('div', {}, h('b', {}, s.name), h('div', { class: 'muted' }, s.detail))));
  if (res.source_video) kids.push(h('p', { class: 'muted' }, 'Origem: ' + res.source_video.title + ' — ' + res.source_video.url));
  if (res.moment) kids.push(h('p', { class: 'muted' }, `Momento: ${res.moment[0]}s–${res.moment[1]}s · nota ${res.score}`));
  if (res.preview) kids.push(h('video', { src: res.preview, controls: true, playsinline: true }));
  if (res.description) kids.push(h('pre', {}, res.description));
  if (res.platform) kids.push(h('p', { class: 'muted' }, `Plataforma: ${res.platform} · status real: ${res.send_status || '—'} · visibilidade: ${res.visibility || '—'} · id: ${res.public_id || '—'}`));
  if (res.diagnostic) kids.push(h('p', { class: 'muted' }, 'Funcionou: ' + (res.diagnostic.funcionou.join(', ') || '—') + ' | Precisa correção: ' + (res.diagnostic.precisa_correcao.join(', ') || 'nada')));
  if (r.status === 'awaiting_confirmation') kids.push(h('button', { class: 'b ok', onclick: () => act(() => api('/api/realtest/' + r.id + '/confirm', { method: 'POST' }), 'Publicando…') }, 'CONFIRMAR PUBLICAÇÃO PÚBLICA no Instagram'));
  box.replaceChildren(...kids);
  const btn = document.getElementById('rtbtn'); if (btn) btn.disabled = r.status === 'running';
  clearTimeout(rtTimer); if (r.status === 'running') rtTimer = setTimeout(pollRT, 3000);
}

/* ---------------- Fontes ---------------- */
async function vFontes() {
  const rows = await api('/api/sources');
  const plat = h('select', {}, ...['youtube', 'twitch', 'instagram', 'url'].map(x => h('option', { value: x }, x === 'instagram' ? 'instagram (sua conta)' : x === 'url' ? 'URL avulsa' : x)));
  const name = h('input', { placeholder: 'Nome (ex.: Meu canal)' }), ref = h('input', { placeholder: 'Canal/@handle/UC… | login Twitch | URL https' }), iv = h('input', { type: 'number', value: 300, min: 60 }), ck = h('input', { type: 'checkbox' });
  return [h('div', { class: 'card' }, h('h3', {}, 'Cadastrar fonte'), plat, name, ref, h('label', {}, 'Intervalo de monitoramento (s)'), iv,
    h('label', { class: 'ck' }, ck, 'Declaro que o conteúdo desta fonte é meu ou tenho autorização para cortá-lo e republicá-lo'),
    h('button', { class: 'b', onclick: () => act(() => api('/api/sources', { method: 'POST', body: { platform: plat.value, name: name.value, ref: ref.value, interval_s: +iv.value, authorized: ck.checked } }), 'Fonte cadastrada') }, 'Cadastrar')),
  ...rows.map(r => h('div', { class: 'card' }, h('b', {}, r.name), ' ', pill(r.platform), ' ', pill(r.enabled ? 'ativa' : 'desativada', r.enabled ? 'ok' : 'warn'),
    h('p', { class: 'muted' }, `${r.ref} · a cada ${r.interval_s}s · último ok: ${fmtT(r.last_ok)} · ${r.contents} conteúdos · acervo ${r.archive_scanned ? 'varrido' : 'pendente'}`), r.last_error ? h('p', { class: 'err' }, r.last_error) : null,
    h('div', { class: 'row' }, h('button', { class: 'b sec', onclick: () => act(async () => { const t = await api(`/api/sources/${r.id}/test`, { method: 'POST' }); toast((t.ok ? '✅ ' : '❌ ') + t.detail, !t.ok); }) }, 'Testar conexão'),
      h('button', { class: 'b sec', onclick: () => act(() => api(`/api/sources/${r.id}`, { method: 'PATCH', body: { enabled: !r.enabled } })) }, r.enabled ? 'Desativar' : 'Ativar'))))];
}

/* ---------------- Clipes / Posts / Falhas ---------------- */
let cf = '';
async function vClipes() {
  const rows = await api('/api/clips' + (cf ? '?status=' + cf : ''));
  const sel = h('select', { onchange: e => { cf = e.target.value; refresh(); } }, ...[['', 'todos'], ['awaiting', 'aguardando'], ['scheduled', 'agendado'], ['published', 'publicado'], ['retry_wait', 'nova tentativa'], ['publish_failed', 'falha'], ['archived', 'arquivado'], ['processing', 'processando']].map(([v, n]) => h('option', { value: v, selected: v === cf }, n)));
  return [h('div', { class: 'card' }, sel), ...rows.map(c => h('div', { class: 'card' }, h('b', {}, '#' + c.id), ' ', pill(c.status, stc(c.status)), ' ', pill(c.category || ''), ' ', pill('nota ' + c.score, c.score >= 70 ? 'ok' : ''), ' ', pill(c.origin || ''),
    h('p', { class: 'muted' }, `${(c.duration || 0).toFixed(0)}s · conteúdo ${c.content_id} · ${fmtT(c.created)} · ` + Object.entries(c.scores).map(([k, v]) => k.slice(0, 5) + ' ' + Math.round(v)).join(' · ')), c.error ? h('p', { class: 'err' }, c.error) : null,
    c.has_file ? h('video', { src: `/api/clips/${c.id}/video`, controls: true, preload: 'none', playsinline: true }) : null,
    h('div', { class: 'row' }, c.status === 'archived' ? h('button', { class: 'b sec', onclick: () => act(() => api(`/api/clips/${c.id}/restore`, { method: 'POST' })) }, 'Restaurar') : ['awaiting', 'publish_failed'].includes(c.status) ? h('button', { class: 'b sec', onclick: () => act(() => api(`/api/clips/${c.id}/archive`, { method: 'POST' })) }, 'Arquivar') : null)))];
}
async function vPubs() {
  const rows = await api('/api/publications');
  return [h('div', { class: 'card' }, h('table', {}, h('tr', {}, ...['#', 'rede', 'status', 'quando', ''].map(x => h('th', {}, x))), ...rows.map(p => h('tr', {}, h('td', {}, p.id), h('td', {}, p.platform), h('td', {}, pill(p.status, stc(p.status)), p.visibility ? h('div', { class: 'muted' }, p.visibility) : null, p.error ? h('div', { class: 'err' }, p.error) : null),
    h('td', {}, fmtT(p.published_at || p.scheduled_at), p.url ? h('div', {}, h('a', { href: p.url, target: '_blank', rel: 'noopener', style: 'color:#7c5cff' }, 'abrir')) : null, p.public_id ? h('div', { class: 'muted' }, 'id ' + p.public_id) : null),
    h('td', {}, ['failed', 'retry_wait'].includes(p.status) ? h('button', { class: 'b sec', onclick: () => act(() => api(`/api/publications/${p.id}/retry`, { method: 'POST' }), 'Reenviado à fila') }, '↻') : null,
      p.status === 'awaiting_confirmation' ? h('button', { class: 'b ok', onclick: () => act(() => api(`/api/publications/${p.id}/confirm`, { method: 'POST' }), 'Confirmado') }, 'Publicar') : null,
      ['scheduled', 'failed', 'retry_wait'].includes(p.status) ? h('button', { class: 'b bad', onclick: () => confirm('Cancelar?') && act(() => api(`/api/publications/${p.id}/cancel`, { method: 'POST' })) }, '✕') : null)))))];
}
async function vFalhas() {
  const rows = await api('/api/jobs?status=dead');
  return [h('div', { class: 'card' }, h('h3', {}, 'Fila de falhas (aguardam você)'), rows.length ? null : h('p', { class: 'muted' }, 'Nenhuma falha 🎉'),
    ...rows.map(j => h('div', { style: 'border-top:1px solid #262b45;padding:8px 0' }, h('b', {}, `#${j.id} ${j.type}`), ' ', pill(j.error_kind || '', 'bad'), h('p', { class: 'err' }, j.last_error), h('button', { class: 'b sec', onclick: () => act(() => api(`/api/jobs/${j.id}/retry`, { method: 'POST' }), 'Reenfileirado') }, 'Tentar de novo'))))];
}

/* ---------------- Config ---------------- */
const FIELDS = [['daily_min', 'Meta mínima/dia (por rede)', 'n'], ['daily_normal', 'Meta normal/dia', 'n'], ['daily_max', 'Meta máxima/dia (limite de PUBLICAÇÃO)', 'n'], ['min_publish_score', 'Nota mínima p/ publicar', 'n'], ['min_clip_score', 'Nota mínima p/ gerar clipe', 'n'],
  ['min_interval_min', 'Intervalo mínimo entre posts (min)', 'n'], ['window_start', 'Início da janela (HH:MM)', 't'], ['window_end', 'Fim da janela (HH:MM)', 't'], ['archive_clips_per_week', 'Clipes do acervo antigo por semana', 'n'],
  ['handle_instagram', '@ do Instagram na marca d’água', 't'], ['handle_tiktok', '@ do TikTok na marca d’água', 't'], ['watermark_opacity', 'Opacidade da marca (0.65 = 65%)', 'n'], ['watermark_position', 'Posição (top_left, top_right, bottom_left, bottom_right, top_center)', 't'],
  ['tiktok_privacy', 'Privacidade TikTok (só vale se app auditado)', 't'], ['original_grace_h', 'Carência p/ apagar original processado (h)', 'n'], ['published_clip_retention_h', 'Retenção do clipe após publicar (h)', 'n'], ['unpublished_retention_days', 'Retenção de arquivados (dias; 0 = nunca)', 'n'], ['backup_every_h', 'Backup a cada (h)', 'n'], ['backup_keep', 'Backups guardados', 'n']];
async function vConfig() {
  const [cfg, acc, bks] = await Promise.all([api('/api/settings'), api('/api/accounts'), api('/api/backups')]);
  const inp = {}; const form = FIELDS.map(([k, l, t]) => { inp[k] = h('input', { value: cfg[k], type: t === 'n' ? 'number' : 'text', step: 'any' }); return h('div', {}, h('label', {}, l), inp[k]); });
  const audit = h('input', { type: 'checkbox', checked: !!cfg.tiktok_audited }), cred = h('input', { type: 'checkbox', checked: !!cfg.credit_line });
  const tok = h('input', { placeholder: 'Access token do Instagram' }), uid = h('input', { placeholder: 'ID numérico da conta (ig_user_id)' });
  const pl = ['instagram', 'tiktok'].map(p => h('label', { class: 'ck' }, h('input', { type: 'checkbox', 'data-p': p, checked: cfg.platforms_enabled.includes(p) }), 'Publicar no ' + p));
  return [h('div', { class: 'card' }, h('h3', {}, 'Contas'),
    h('p', {}, 'Instagram: ', pill(acc.instagram.connected ? 'conectado @' + (acc.instagram.username || '?') : 'não conectado', acc.instagram.connected ? 'ok' : 'bad'), acc.instagram.connected ? h('button', { class: 'b sec', onclick: () => act(() => api('/api/accounts/instagram', { method: 'DELETE' })) }, 'Desconectar') : null),
    tok, uid, h('button', { class: 'b', onclick: () => act(() => api('/api/accounts/instagram', { method: 'POST', body: { access_token: tok.value, ig_user_id: uid.value } }), 'Instagram conectado') }, 'Conectar Instagram'),
    h('p', {}, 'TikTok: ', pill(acc.tiktok.connected ? 'conectado' + (acc.tiktok.username ? ' @' + acc.tiktok.username : '') : 'não conectado', acc.tiktok.connected ? 'ok' : 'bad')),
    h('div', { class: 'row' }, h('button', { class: 'b', onclick: () => act(async () => { const r = await api('/api/oauth/tiktok/start'); location.href = r.url; }) }, 'Conectar TikTok'), acc.tiktok.connected ? h('button', { class: 'b sec', onclick: () => act(() => api('/api/accounts/tiktok', { method: 'DELETE' })) }, 'Desconectar') : null)),
  h('div', { class: 'card' }, h('h3', {}, 'Metas, horários e regras'), ...form, h('label', { class: 'ck' }, audit, 'Meu app do TikTok já foi AUDITADO (libera publicação pública)'), h('label', { class: 'ck' }, cred, 'Incluir linha “Créditos: fonte” na descrição'), ...pl,
    h('button', { class: 'b', onclick: () => act(() => { const b = {}; for (const [k, , t] of FIELDS) b[k] = t === 'n' ? +inp[k].value : inp[k].value; b.tiktok_audited = audit.checked; b.credit_line = cred.checked; b.platforms_enabled = [...document.querySelectorAll('[data-p]')].filter(x => x.checked).map(x => x.dataset.p); return api('/api/settings', { method: 'PUT', body: b }); }, 'Configurações salvas') }, 'Salvar')),
  h('div', { class: 'card' }, h('h3', {}, 'Backup'), h('button', { class: 'b', onclick: () => act(() => api('/api/backup', { method: 'POST' }), 'Backup criado') }, 'Criar backup agora'), ...bks.slice(0, 8).map(b => h('p', {}, h('a', { href: '/api/backups/' + b.name, style: 'color:#7c5cff' }, b.name), ' ', h('span', { class: 'muted' }, fmtB(b.size))))),
  h('div', { class: 'card' }, h('button', { class: 'b bad', onclick: () => act(async () => { await api('/api/logout', { method: 'POST' }); me = null; render(); }) }, 'Sair'))];
}
async function vLogs() {
  const [lg, at] = await Promise.all([api('/api/logs?limit=150'), api('/api/attempts')]);
  return [h('div', { class: 'card' }, h('h3', {}, 'Eventos'), ...lg.map(l => h('div', { class: l.level === 'error' ? 'err muted' : 'muted' }, `${fmtT(l.ts)} [${l.level}] ${l.component}: ${l.message}`))),
    h('div', { class: 'card' }, h('h3', {}, 'Histórico de tentativas'), ...at.map(a => h('div', { class: a.ok ? 'muted' : 'err muted' }, `${fmtT(a.ts)} ${a.ref_type}#${a.ref_id} ${a.op} ${a.ok ? 'ok' : 'FALHA'} ${a.message || ''}`)))];
}

if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
api('/api/me').then(r => { me = r.user; render(); }).catch(() => render());
