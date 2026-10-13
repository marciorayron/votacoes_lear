// admin.js — Painel Administrativo (X-Device-Token).
const PREFILL_TOKEN = window.VOTACAO_PREFILL_TOKEN ?? "";
const TOKEN_KEY = 'votacao_device_token';
const THEME_KEY = 'votacao_theme';

const unlockSection = document.getElementById('unlock');
const panelSection  = document.getElementById('panel');
const tokenInput    = document.getElementById('token-input');
const unlockHint    = document.getElementById('unlock-hint');
const optionsList   = document.getElementById('options-list');
const toastEl       = document.getElementById('toast');

let token = '';
let refreshTimer = null;
let historyLoaded = false;

// ------------------ Utilidades ------------------
function toast(message, kind) {
  toastEl.textContent = message;
  toastEl.className = 'toast show ' + (kind || '');
  clearTimeout(toastEl._t);
  toastEl._t = setTimeout(() => { toastEl.className = 'toast ' + (kind || ''); }, 2600);
}

async function api(path, method = 'GET', body = null) {
  const opts = {
    method,
    headers: { 'X-Device-Token': token, 'Cache-Control': 'no-store' },
    cache: 'no-store'
  };
  if (body !== null) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) { handleUnauthorized(); throw new Error('unauthorized'); }
  if (!res.ok && !data.error) throw new Error('http_' + res.status);
  return { ok: res.ok, status: res.status, data };
}

// Sessao expirada / token invalido: bloqueia o painel e reabre o modal Unlock.
const SESSION_EXPIRED_MSG =
  'Sessão expirada ou token inválido. Informe o novo Device Token exibido no terminal.';

function handleUnauthorized() {
  const atUnlock = unlockSection && !unlockSection.classList.contains('hidden');
  if (atUnlock) {
    if (unlockHint) unlockHint.textContent = SESSION_EXPIRED_MSG;
  } else {
    lock(SESSION_EXPIRED_MSG);
  }
  toast(SESSION_EXPIRED_MSG, 'err');
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));
}

// ------------------ Opções dinamicas ------------------
function addOption(value) {
  const row = document.createElement('div');
  row.className = 'option-row';
  row.innerHTML =
    '<input type="text" placeholder="Texto da opção" value="" />' +
    '<button class="icon-btn" title="Remover">✕</button>';
  row.querySelector('input').value = value || '';
  row.querySelector('.icon-btn').addEventListener('click', () => row.remove());
  optionsList.appendChild(row);
  return row;
}

function setPresetYesNo() {
  optionsList.innerHTML = '';
  addOption('Sim');
  addOption('Não');
}

function collectOptions() {
  return Array.from(optionsList.querySelectorAll('input'))
    .map(i => i.value.trim())
    .filter(v => v.length > 0);
}

// ------------------ Pareamento ------------------
async function generatePairingPin() {
  try {
    const { data } = await api('/api/admin/pairing', 'POST', { generate: true });
    renderState(data);
    toast('PIN de pareamento: ' + (data.pairing_pin || '–'), 'ok');
  } catch (err) {
    if (err.message === 'unauthorized') return;
    toast('Falha ao gerar o PIN.', 'err');
  }
}

async function setPairingPin() {
  const input = document.getElementById('pairing-input');
  const pin = (input.value || '').trim();
  if (!pin) { toast('Informe um PIN alfanumérico.', 'err'); return; }
  try {
    const { ok, data } = await api('/api/admin/pairing', 'POST', { pin: pin });
    if (!ok || (data && data.ok === false)) {
      const code = (data && data.error) || '';
      if (code === 'pin_length' || code === 'pin_alphanumeric')
        toast('PIN inválido (4–16 caracteres alfanuméricos).', 'err');
      else toast('Falha ao definir o PIN.', 'err');
      return;
    }
    renderState(data);
    input.value = '';
    toast('PIN de pareamento definido.', 'ok');
  } catch (err) {
    if (err.message === 'unauthorized') return;
    toast('Falha ao definir o PIN.', 'err');
  }
}

async function clearPairingPin() {
  try {
    const { data } = await api('/api/admin/pairing', 'POST', { clear: true });
    renderState(data);
    toast('Pareamento desativado.', 'ok');
  } catch (err) {
    if (err.message === 'unauthorized') return;
    toast('Falha ao desativar o pareamento.', 'err');
  }
}

function copyPairingPin() {
  const badge = document.getElementById('pairing-pin');
  const pin = (badge.textContent || '').trim();
  if (!pin || badge.classList.contains('off')) { toast('Nenhum PIN ativo.', 'err'); return; }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(pin)
      .then(() => toast('PIN copiado.', 'ok'))
      .catch(() => toast('Não foi possível copiar.', 'err'));
  } else {
    toast('Cópia não suportada neste navegador.', 'err');
  }
}
async function refreshState(showError) {
  try {
    const { data } = await api('/api/admin/state');
    renderState(data);
    document.getElementById('clock').textContent = 'Servidor: ' + (data.server_time || '–');
  } catch (err) {
    if (err.message === 'unauthorized') return;
    if (showError) toast('Falha ao consultar o servidor.', 'err');
  }
}

// ------------------ Bloqueio / desbloqueio ------------------
function startAutoRefresh() {
  clearInterval(refreshTimer);
  refreshTimer = setInterval(() => refreshState(false), 3000);
}

function unlock() {
  token = (tokenInput.value || '').trim();
  if (!token) { unlockHint.textContent = 'Informe um token.'; return; }
  unlockHint.textContent = 'Validando…';
  api('/api/admin/state')
    .then(({ data }) => {
      sessionStorage.setItem(TOKEN_KEY, token);
      unlockSection.classList.add('hidden');
      panelSection.classList.remove('hidden');
      renderState(data);
      startAutoRefresh();
      toast('Acesso liberado.', 'ok');
      if (currentView === 'db') loadHistory(false);
    })
    .catch(err => {
      unlockHint.textContent = err.message === 'unauthorized'
        ? 'Token recusado. Confira o valor exibido no console do servidor.'
        : 'Não foi possível contatar o servidor.';
    });
}

function lock(message) {
  clearInterval(refreshTimer);
  token = '';
  sessionStorage.removeItem(TOKEN_KEY);
  panelSection.classList.add('hidden');
  unlockSection.classList.remove('hidden');
  tokenInput.value = '';
  unlockHint.textContent = message || '';
  tokenInput.focus();
}

// ------------------ Navegacao (views) ------------------
const VIEW_TITLES = {
  home: '📊 Painel Principal',
  db: '🗄️ Banco de Dados & Histórico',
  booths: '🖥️ Cabines & Pareamento',
  reports: '🧾 Relatórios & Atas'
};
let currentView = 'home';

function showView(name) {
  if (!VIEW_TITLES[name]) name = 'home';
  currentView = name;
  document.querySelectorAll('.view').forEach(v =>
    v.classList.toggle('hidden', v.id !== 'view-' + name));
  document.querySelectorAll('.nav-item').forEach(b =>
    b.classList.toggle('active', b.dataset.view === name));
  document.getElementById('view-title').textContent = VIEW_TITLES[name];
  if (name === 'db') loadHistory(false);
}

// ------------------ Tema (claro / escuro) ------------------
function applyTheme(theme, persist) {
  const t = theme === 'light' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', t);
  const btn = document.getElementById('theme-toggle');
  btn.textContent = t === 'dark' ? '🌙 Modo escuro' : '☀️ Modo claro';
  if (persist !== false) { try { localStorage.setItem(THEME_KEY, t); } catch (e) {} }
}

function toggleTheme() {
  const cur = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
  applyTheme(cur);
}

// Sincroniza com as demais abas/terminais quando o tema muda em qualquer tela.
window.addEventListener('storage', (e) => {
  if (e.key === THEME_KEY && e.newValue) applyTheme(e.newValue, false);
});
// ------------------ Historico do banco ------------------
async function loadHistory(showError) {
  try {
    const { data } = await api('/api/admin/history');
    renderHistory(data);
    historyLoaded = true;
  } catch (err) {
    if (err.message === 'unauthorized') return;
    if (showError) toast('Falha ao carregar o histórico.', 'err');
  }
}

function renderHistory(data) {
  const list = document.getElementById('history-list');
  const elections = (data && data.elections) || [];
  const count = (data && data.count != null) ? data.count : elections.length;
  document.getElementById('db-count').textContent = count;
  const active = elections.find(e => e.active);
  document.getElementById('db-active').textContent = active ? ('#' + active.id) : '—';

  if (!elections.length) {
    list.innerHTML = '<div class="muted">Nenhum plebiscito registrado.</div>';
    return;
  }
  list.innerHTML = '';
  elections.forEach(e => {
    const item = document.createElement('div');
    item.className = 'hist-item' + (e.active ? ' active' : '');
    const badge = e.active
      ? '<span class="badge b-open">ATIVO</span>'
      : '<span class="badge b-' + (e.status === 'closed' ? 'closed' : 'open') + '">' +
        escapeHtml(String(e.status || '').toUpperCase()) + '</span>';
    const opts = (e.options || []).map(o =>
      '<span class="hist-opt">' + escapeHtml(o.label) + ' <b>' + o.count + '</b>' +
      ' <span class="muted">(' + (o.percent ?? 0) + '%)</span></span>').join('');
    const meta = 'ID #' + e.id + ' · ' + (e.created_at || '–') +
      (e.closed_at ? ' → ' + e.closed_at : '') +
      ' · Votos: ' + (e.total_votes ?? 0) +
      (e.voters != null ? ' · Votantes: ' + e.voters : '');
    item.innerHTML =
      '<div class="hist-top"><div class="hist-q">' + escapeHtml(e.question) + '</div>' +
      badge + '</div>' +
      '<div class="hist-meta muted">' + escapeHtml(meta) + '</div>' +
      '<div class="hist-opts">' + (opts || '<span class="muted">sem opções</span>') + '</div>';
    list.appendChild(item);
  });
}

// ------------------ Purga do banco ------------------
async function purgeDatabase() {
  const scope = document.getElementById('purge-scope').value;
  const typed = (document.getElementById('purge-confirm').value || '').trim().toUpperCase();
  if (typed !== 'PURGE') { toast('Digite PURGE para confirmar.', 'err'); return; }
  if (!confirm('CONFIRMAÇÃO FINAL: purgar dados (' + scope +
               ')? Esta ação é irreversível.')) return;

  const btn = document.getElementById('purge-btn');
  btn.disabled = true;
  try {
    const url = '/api/admin/db/purge?confirm=1&scope=' + encodeURIComponent(scope);
    const { ok, data } = await api(url, 'DELETE');
    if (!ok || data.ok === false) { toast('Falha ao purgar o banco.', 'err'); return; }
    if (data.state) renderState(data.state);
    if (data.history) renderHistory(data.history);
    const r = data.removed || {};
    toast('Purgado: ' + (r.votes || 0) + ' voto(s), ' + (r.voters || 0) +
          ' check-in(s), ' + (r.polls || 0) + ' plebiscito(s).', 'ok');
    document.getElementById('purge-confirm').value = '';
  } catch (err) {
    if (err.message === 'unauthorized') return;
    toast('Falha ao purgar o banco.', 'err');
  } finally {
    btn.disabled = false;
  }
}
// ------------------ Ações ------------------
async function startPoll() {
  const question = document.getElementById('question').value.trim();
  const options  = collectOptions();

  if (!question) { toast('Informe a pergunta.', 'err'); return; }
  if (options.length < 2) { toast('Informe ao menos 2 opções.', 'err'); return; }

  const btn = document.getElementById('start-btn');
  btn.disabled = true;
  try {
    const { data } = await api('/api/admin/poll', 'POST', { question, options });
    renderState(data);
    toast('Plebiscito iniciado.', 'ok');
  } catch (err) {
    if (err.message === 'unauthorized') return;
    toast('Erro ao iniciar o plebiscito.', 'err');
  } finally {
    btn.disabled = false;
  }
}

async function resetPoll() {
  if (!confirm('Zerar os contadores do plebiscito atual? Todos os crachás deverão fazer check-in novamente.')) return;
  try {
    const { data } = await api('/api/admin/reset', 'POST', { clear_voters: true });
    renderState(data);
    toast('Contadores zerados.', 'ok');
  } catch (err) {
    if (err.message === 'unauthorized') return;
    toast('Nada a zerar (sem plebiscito ativo?)', 'err');
  }
}

async function closePoll() {
  if (!confirm('Encerrar a votação atual? As cabines deixarão de registrar votos.')) return;
  try {
    const { data } = await api('/api/admin/close', 'POST');
    renderState(data);
    toast('Votação encerrada.', 'ok');
  } catch (err) {
    if (err.message === 'unauthorized') return;
    toast('Nada a encerrar (sem plebiscito ativo?)', 'err');
  }
}

function openReport() {
  if (!token) { toast('Sessão bloqueada.', 'err'); return; }
  // Abre a Ata de Fechamento (A4) em nova aba; salvar como PDF via impressao.
  window.open('/admin/relatorio?token=' + encodeURIComponent(token), '_blank');
}
// ------------------ Wiring ------------------
document.getElementById('unlock-btn').addEventListener('click', unlock);
tokenInput.addEventListener('keydown', e => { if (e.key === 'Enter') unlock(); });
document.getElementById('forget-btn').addEventListener('click', () => lock('Token removido.'));
document.getElementById('lock-btn').addEventListener('click', () => lock('Sessão bloqueada.'));
document.getElementById('refresh-btn').addEventListener('click', () => {
  refreshState(true);
  if (currentView === 'db') loadHistory(true);
});
document.getElementById('add-option').addEventListener('click', () => addOption(''));
document.getElementById('preset-yesno').addEventListener('click', setPresetYesNo);
document.getElementById('start-btn').addEventListener('click', startPoll);
document.getElementById('reset-btn').addEventListener('click', resetPoll);
document.getElementById('close-btn').addEventListener('click', closePoll);
document.getElementById('report-btn').addEventListener('click', openReport);
document.getElementById('pairing-generate').addEventListener('click', generatePairingPin);
document.getElementById('pairing-copy').addEventListener('click', copyPairingPin);
document.getElementById('pairing-clear').addEventListener('click', clearPairingPin);
document.getElementById('pairing-set').addEventListener('click', setPairingPin);
document.getElementById('pairing-input').addEventListener('keydown',
  e => { if (e.key === 'Enter') setPairingPin(); });

// Sidebar: navegacao entre secoes + tema.
document.querySelectorAll('.nav-item[data-view]').forEach(btn =>
  btn.addEventListener('click', () => showView(btn.dataset.view)));
document.getElementById('theme-toggle').addEventListener('click', toggleTheme);
document.getElementById('history-refresh').addEventListener('click', () => loadHistory(true));
document.getElementById('purge-btn').addEventListener('click', purgeDatabase);

// ------------------ Init ------------------
(function init() {
  let savedTheme = 'dark';
  try { savedTheme = localStorage.getItem(THEME_KEY) || 'dark'; } catch (e) {}
  applyTheme(savedTheme, false);

  setPresetYesNo();
  showView('home');
  const stored = sessionStorage.getItem(TOKEN_KEY);
  if (PREFILL_TOKEN) {
    tokenInput.value = PREFILL_TOKEN;
  } else if (stored) {
    tokenInput.value = stored;
  }
  if (tokenInput.value) unlock();
  else { unlockHint.textContent = ''; tokenInput.focus(); }
})();

// ------------------ Painel de status ------------------
function renderState(s) {
  const open = !!s.poll_open;
  const pollId = s.poll_id ?? '–';
  document.getElementById('state-poll').textContent = open ? ('#' + pollId) : 'Fechado';
  document.getElementById('state-tokens').textContent = s.voting_tokens ?? 0;
  document.getElementById('state-total').textContent = s.total_votes ?? 0;
  document.getElementById('poll-question').textContent = s.poll_question || 'Nenhum plebiscito ativo';
  document.getElementById('poll-id-text').textContent = open ? ('Plebiscito #' + pollId) : 'Sem plebiscito aberto';

  document.getElementById('btn-open').disabled = open;
  document.getElementById('btn-close').disabled = !open;
  document.getElementById('btn-purge').disabled = !s.has_results;

  const results = s.results || [];
  const total = s.total_votes || 0;
  const bars = document.getElementById('results-bars');
  const empty = document.getElementById('results-empty');
  if (!results.length) {
    bars.innerHTML = '';
    empty.classList.remove('hidden');
  } else {
    empty.classList.add('hidden');
    bars.innerHTML = results.map(r => {
      const pct = total > 0 ? Math.round((r.count / total) * 100) : 0;
      return '<div class="bar-item"><div class="top"><span>' + escapeHtml(r.text) +
        '</span><span><b>' + r.count + '</b> (' + pct + '%)</span></div>' +
        '<div class="track"><div class="fill" style="width:' + pct + '%"></div></div></div>';
    }).join('');
  }

  renderBooths(s.booths || []);
  document.getElementById('new-token').value = s.current_token || '';
  document.getElementById('new-token').placeholder = 'Nova senha de dispositivo';
}

function boothStatusInfo(mins) {
  if (mins === null || mins === undefined) return { dot: '', txt: 'Sem sinal' };
  if (mins < 2) return { dot: 'ok', txt: 'Online há menos de 2 min' };
  if (mins < 10) return { dot: 'warn', txt: 'Última vez há ' + mins + ' min' };
  return { dot: 'err', txt: 'Inativo há ' + mins + ' min' };
}

function fmtSeen(mins) {
  if (mins === null || mins === undefined) return '—';
  if (mins < 1) return 'agora';
  if (mins < 60) return mins + ' min atrás';
  const h = Math.floor(mins / 60);
  return h + ' h atrás';
}

function renderBooths(booths) {
  const body = document.getElementById('booth-body');
  const empty = document.getElementById('booths-empty');
  const table = document.getElementById('booth-table');
  document.getElementById('booth-count').textContent = booths.length;
  if (!booths.length) {
    body.innerHTML = '';
    table.classList.add('hidden');
    empty.classList.remove('hidden');
    return;
  }
  table.classList.remove('hidden');
  empty.classList.add('hidden');
  body.innerHTML = booths.map(b => {
    const info = boothStatusInfo(b.last_seen_min);
    const pin = b.pairing_pin
      ? '<span class="pin-badge">' + escapeHtml(b.pairing_pin) + '</span>'
      : '<span class="pin-badge off">—</span>';
    return '<tr>' +
      '<td>' + escapeHtml(b.id) + '</td>' +
      '<td><span class="st"><span class="dot ' + info.dot + '"></span>' +
        escapeHtml(info.txt) + '</span></td>' +
      '<td>' + fmtSeen(b.last_seen_min) + '</td>' +
      '<td>' + pin + '</td>' +
      '<td><button class="btn ghost small" data-rm-booth="' + escapeHtml(b.id) + '">Remover</button></td>' +
      '</tr>';
  }).join('');
  body.querySelectorAll('[data-rm-booth]').forEach(btn => {
    btn.addEventListener('click', () => removeBooth(btn.getAttribute('data-rm-booth')));
  });
}
