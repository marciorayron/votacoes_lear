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
let lastHistory = null;   // ultimo payload de /api/admin/history (para CSV)

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

// Formata um timestamp ISO em data/hora local brasileira (DD/MM/YYYY às HH:mm).
function fmtDateTimeBR(iso) {
  if (!iso) return '—';
  const t = Date.parse(iso);
  if (isNaN(t)) return String(iso);
  const d = new Date(t);
  const p = n => String(n).padStart(2, '0');
  return p(d.getDate()) + '/' + p(d.getMonth() + 1) + '/' + d.getFullYear() +
    ' às ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

// Escapa um campo para CSV (RFC 4180) quando contiver separador, aspas ou quebra.
function csvEscape(value) {
  const s = (value === null || value === undefined) ? '' : String(value);
  return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

// Dispara o download de um arquivo gerado no cliente (CSV).
function downloadFile(filename, content, mime) {
  const blob = new Blob([content], { type: mime || 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
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
  lastHistory = data || null;
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

    // Detalhamento por opcao com barra de progresso (percentual apurado).
    const opts = (e.options || []).map(o => {
      const pct = Math.max(0, Math.min(100, Number(o.percent) || 0));
      return '<div class="hist-opt">' +
        '<div class="hist-opt-top"><span>' + escapeHtml(o.label) + '</span>' +
        '<span><b>' + (o.count ?? 0) + '</b> ' +
        '<span class="muted">(' + pct + '%)</span></span></div>' +
        '<div class="track"><div class="fill" style="width:' + pct + '%"></div></div>' +
        '</div>';
    }).join('');

    // Datas ISO -> formato local brasileiro (DD/MM/YYYY às HH:mm).
    const meta = 'ID #' + e.id + ' · ' + fmtDateTimeBR(e.created_at) +
      (e.closed_at ? ' → ' + fmtDateTimeBR(e.closed_at) : '') +
      ' · Votos: ' + (e.total_votes ?? 0) +
      (e.voters != null ? ' · Votantes: ' + e.voters : '');

    item.innerHTML =
      '<div class="hist-top"><div class="hist-q">' + escapeHtml(e.question) + '</div>' +
      badge + '</div>' +
      '<div class="hist-meta muted">' + escapeHtml(meta) + '</div>' +
      '<div class="hist-opts">' + (opts || '<span class="muted">sem opções</span>') + '</div>' +
      '<div class="hist-actions">' +
        '<button class="btn ghost small" data-export-csv="' + e.id + '" ' +
          'title="Baixar os resultados deste plebiscito em CSV">📥 Exportar CSV</button>' +
        '<button class="btn danger small" data-del-poll="' + e.id + '" ' +
          'title="Excluir definitivamente este plebiscito e seus votos">🗑️ Excluir</button>' +
      '</div>';
    list.appendChild(item);

    const csvBtn = item.querySelector('[data-export-csv]');
    if (csvBtn) csvBtn.addEventListener('click', () => exportPollCsv(e.id));
    const delBtn = item.querySelector('[data-del-poll]');
    if (delBtn) delBtn.addEventListener('click', () => deletePoll(e.id));
  });
}

// Exporta um plebiscito (votos por opcao + percentuais) em CSV.
function exportPollCsv(pollId) {
  const elections = (lastHistory && lastHistory.elections) || [];
  const e = elections.find(x => String(x.id) === String(pollId));
  if (!e) { toast('Plebiscito não encontrado no histórico.', 'err'); return; }

  const rows = ['Plebiscito,Pergunta,Opção,Votos,Percentual'];
  (e.options || []).forEach(o => {
    const pct = Math.max(0, Math.min(100, Number(o.percent) || 0));
    rows.push([
      '#' + e.id,
      csvEscape(e.question),
      csvEscape(o.label),
      String(o.count ?? 0),
      pct.toFixed(1)
    ].join(','));
  });
  rows.push(['#' + e.id, csvEscape(e.question), csvEscape('TOTAL'),
             String(e.total_votes ?? 0), (e.total_votes ? '100.0' : '0.0')].join(','));

  // BOM para o Excel reconhecer UTF-8 (acentos corretos).
  const csv = '\ufeff' + rows.join('\r\n') + '\r\n';
  downloadFile('plebiscito_' + e.id + '.csv', csv, 'text/csv;charset=utf-8');
  toast('CSV do plebiscito #' + e.id + ' gerado.', 'ok');
}

// Exclui um plebiscito individual (e seus votos) de forma irreversivel.
async function deletePoll(pollId) {
  if (!pollId) return;
  if (!confirm('Excluir o plebiscito #' + pollId +
               ' e todos os seus votos? Esta ação é irreversível.')) return;
  try {
    const { ok, data } = await api(
      '/api/admin/polls/' + encodeURIComponent(pollId), 'DELETE');
    if (!ok || data.ok === false) { toast('Falha ao excluir o plebiscito.', 'err'); return; }
    if (data.state) renderState(data.state);
    if (data.history) renderHistory(data.history);
    toast('Plebiscito #' + pollId + ' excluído.' +
          (data.was_active ? ' O plebiscito ativo foi reiniciado.' : ''), 'ok');
  } catch (err) {
    if (err.message === 'unauthorized') return;
    toast('Falha ao excluir o plebiscito.', 'err');
  }
}


// ------------------ Purga do banco ------------------
async function purgeDatabase() {
  const scope = document.getElementById('purge-scope').value;
  const typed = (document.getElementById('purge-confirm').value || '').trim().toUpperCase();
  const secondary = (document.getElementById('purge-secondary').value || '').trim();
  if (typed !== 'PURGE') { toast('Digite PURGE para confirmar.', 'err'); return; }
  if (!secondary) {
    toast('Informe o Device Token ou o PIN de verificação secundária.', 'err');
    return;
  }
  if (!confirm('CONFIRMAÇÃO FINAL: purgar dados (' + scope +
               ')? Esta ação é irreversível.')) return;

  const btn = document.getElementById('purge-btn');
  btn.disabled = true;
  try {
    const url = '/api/admin/db/purge?confirm=1&scope=' + encodeURIComponent(scope);
    const { ok, data } = await api(url, 'DELETE', { secondary: secondary });
    if (!ok || data.ok === false) {
      toast(data.detail || 'Falha ao purgar o banco.', 'err');
      return;
    }
    if (data.state) renderState(data.state);
    if (data.history) renderHistory(data.history);
    const r = data.removed || {};
    toast('Purgado: ' + (r.votes || 0) + ' voto(s), ' + (r.voters || 0) +
          ' check-in(s), ' + (r.polls || 0) + ' plebiscito(s).', 'ok');
    document.getElementById('purge-confirm').value = '';
    document.getElementById('purge-secondary').value = '';
  } catch (err) {
    if (err.message === 'unauthorized') return;
    toast('Falha ao purgar o banco.', 'err');
  } finally {
    btn.disabled = false;
  }
}

// ------------------ Limite de fila do check-in ------------------
async function saveQueueThreshold() {
  const input = document.getElementById('queue-threshold');
  const value = parseInt((input.value || '').trim(), 10);
  if (!Number.isFinite(value) || value < 1) {
    toast('Informe um limite válido (número >= 1).', 'err');
    return;
  }
  const btn = document.getElementById('queue-threshold-save');
  btn.disabled = true;
  try {
    const { ok, data } = await api('/api/admin/settings/queue-threshold', 'POST',
                                   { threshold: value });
    if (!ok || data.ok === false) {
      toast(data.detail || 'Falha ao salvar o limite.', 'err');
      return;
    }
    if (data.state) renderState(data.state);
    else if (data.queue_warning_threshold != null) input.value = data.queue_warning_threshold;
    toast('Limite de fila definido em ' + data.queue_warning_threshold + '.', 'ok');
  } catch (err) {
    if (err.message === 'unauthorized') return;
    toast('Falha ao salvar o limite.', 'err');
  } finally {
    btn.disabled = false;
  }
}

// ------------------ Backup / Restauracao do banco ------------------
// Baixa um backup consistente (.db) gerado pelo servidor (1 clique).
async function downloadBackup() {
  const btn = document.getElementById('backup-download');
  btn.disabled = true;
  try {
    const res = await fetch('/api/admin/backup/download', {
      headers: { 'X-Device-Token': token }, cache: 'no-store'
    });
    if (res.status === 401) { handleUnauthorized(); return; }
    if (!res.ok) { toast('Falha ao gerar o backup.', 'err'); return; }

    const blob = await res.blob();
    let filename = 'votacao_backup.db';
    const cd = res.headers.get('Content-Disposition') || '';
    const m = cd.match(/filename\*?=(?:UTF-8''|")?([^\";]+)/i);
    if (m && m[1]) filename = decodeURIComponent(m[1]);

    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast('Backup gerado: ' + filename, 'ok');
  } catch (err) {
    toast('Falha ao gerar o backup.', 'err');
  } finally {
    btn.disabled = false;
  }
}

// Restaura o banco a partir de um arquivo .db escolhido pelo administrador.
async function restoreBackup() {
  const input = document.getElementById('backup-file');
  if (!input || !input.files || !input.files.length) {
    toast('Selecione um arquivo de backup (.db).', 'err');
    return;
  }
  const file = input.files[0];
  if (!confirm('Restaurar o banco a partir de "' + file.name + '"?\n\n' +
               'Todos os dados atuais (plebiscitos, votos e check-ins) serão SUBSTITUÍDOS.')) return;
  if (!confirm('CONFIRMAÇÃO FINAL: o banco atual será sobrescrito. Continuar?')) return;

  const btn = document.getElementById('backup-restore');
  btn.disabled = true;
  try {
    const fd = new FormData();
    fd.append('file', file);
    const res = await fetch('/api/admin/backup/restore', {
      method: 'POST', headers: { 'X-Device-Token': token }, body: fd, cache: 'no-store'
    });
    const data = await res.json().catch(() => ({}));
    if (res.status === 401) { handleUnauthorized(); return; }
    if (!res.ok || data.ok === false) {
      toast(data.detail || data.error || 'Falha ao restaurar o banco.', 'err');
      return;
    }
    if (data.state) renderState(data.state);
    if (data.history) renderHistory(data.history);
    input.value = '';
    toast('Banco de dados restaurado com sucesso.', 'ok');
  } catch (err) {
    toast('Falha ao restaurar o banco.', 'err');
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

// Rotas das paginas de relatorio (A4). Escolhidas na view "Relatórios & Atas".
const REPORT_PAGES = {
  ata:     '/admin/relatorio',
  history: '/admin/relatorio/historico',
  booths:  '/admin/relatorio/cabines'
};

function openReport(kind) {
  if (!token) { toast('Sessão bloqueada.', 'err'); return; }
  // Abre a pagina de relatorio (A4) em nova aba; salvar como PDF via impressao.
  const path = REPORT_PAGES[kind] || REPORT_PAGES.ata;
  window.open(path + '?token=' + encodeURIComponent(token), '_blank');
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
document.getElementById('report-ata-btn').addEventListener('click', () => openReport('ata'));
document.getElementById('report-history-btn').addEventListener('click', () => openReport('history'));
document.getElementById('report-booths-btn').addEventListener('click', () => openReport('booths'));
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
document.getElementById('queue-threshold-save').addEventListener('click', saveQueueThreshold);
document.getElementById('queue-threshold').addEventListener('keydown',
  e => { if (e.key === 'Enter') saveQueueThreshold(); });
document.getElementById('backup-download').addEventListener('click', downloadBackup);
document.getElementById('backup-restore').addEventListener('click', restoreBackup);

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
function setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value;
}

// Atualiza o selo do PIN de pareamento na secao "Cabines & Pareamento".
function renderPairing(pin) {
  const badge = document.getElementById('pairing-pin');
  if (!badge) return;
  const active = !!pin;
  badge.textContent = active ? pin : 'desativado';
  badge.classList.toggle('off', !active);
}

// Minutos decorridos desde um timestamp ISO (ou null quando ausente/invalido).
function minutesSince(iso) {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (isNaN(t)) return null;
  return Math.max(0, Math.floor((Date.now() - t) / 60000));
}

function renderState(s) {
  s = s || {};
  const poll = s.poll || null;
  const open = !!poll && poll.status === 'open';
  const total = poll ? (poll.total_votes || 0) : 0;

  // Contadores da secao "Estado atual".
  setText('s-tokens', s.voting_tokens ?? 0);
  setText('s-voters', s.voters_checked_in ?? 0);
  setText('s-total', total);

  // Pergunta + linha de metadados do plebiscito.
  setText('cur-question', poll ? (poll.question || 'Plebiscito ativo')
                               : 'Nenhum plebiscito ativo.');
  const meta = [];
  if (poll) {
    meta.push('Plebiscito #' + poll.id, open ? 'aberto' : 'encerrado');
    if (poll.created_at) meta.push('criado: ' + poll.created_at);
    if (poll.closed_at)  meta.push('encerrado: ' + poll.closed_at);
  }
  setText('cur-meta', meta.join(' · '));

  // Barras de resultado (votos por opcao).
  const bars = document.getElementById('cur-bars');
  if (bars) {
    const options = (poll && poll.options) || [];
    if (!options.length || total === 0) {
      bars.innerHTML = '<div class="muted">' +
        (poll ? 'Sem votos registrados.' : 'Nenhum plebiscito ativo.') + '</div>';
    } else {
      bars.innerHTML = options.map(r => {
        const count = r.count || 0;
        const pct = Math.round((count / total) * 100);
        return '<div class="bar-item"><div class="top"><span>' + escapeHtml(r.label) +
          '</span><span><b>' + count + '</b> (' + pct + '%)</span></div>' +
          '<div class="track"><div class="fill" style="width:' + pct + '%"></div></div></div>';
      }).join('');
    }
  }

  // Botoes que dependem de haver um plebiscito aberto.
  const resetBtn = document.getElementById('reset-btn');
  const closeBtn = document.getElementById('close-btn');
  if (resetBtn) resetBtn.disabled = !open;
  if (closeBtn) closeBtn.disabled = !open;

  // Pareamento e parametros exibidos na secao de cabines.
  renderPairing(s.pairing_pin || '');
  setText('cfg-delay', s.results_delay ?? '–');
  setText('cfg-cooldown', s.cooldown ?? '–');

  // Limite de fila do check-in (Alerta Amarelo) - nao sobrescreve o campo
  // enquanto o administrador estiver editando-o.
  const QtEl = document.getElementById('queue-threshold');
  if (QtEl && s.queue_warning_threshold != null &&
      document.activeElement !== QtEl) {
    QtEl.value = s.queue_warning_threshold;
  }

  renderBooths(s.booths || []);
}

function boothStatusInfo(b) {
  if (!b.online) return { dot: 'err', txt: 'Offline' };
  if (b.assigned) return { dot: 'warn', txt: 'Em uso' };
  return { dot: 'ok', txt: 'Ociosa' };
}

function fmtSeen(mins) {
  if (mins === null || mins === undefined) return '—';
  if (mins < 1) return 'agora';
  if (mins < 60) return mins + ' min atrás';
  const h = Math.floor(mins / 60);
  return h + ' h atrás';
}

function renderBooths(booths) {
  const body = document.getElementById('booth-rows');
  if (!body) return;
  if (!booths.length) {
    body.innerHTML = '<tr><td colspan="5" class="muted">Nenhuma cabine registrada.</td></tr>';
    return;
  }
  body.innerHTML = booths.map(b => {
    const info = boothStatusInfo(b);
    return '<tr>' +
      '<td>' + escapeHtml(b.label || b.id) + '</td>' +
      '<td>' + escapeHtml(b.client_ip || '—') + '</td>' +
      '<td><span class="st"><span class="dot ' + info.dot + '"></span>' +
        escapeHtml(info.txt) + '</span></td>' +
      '<td>' + fmtSeen(minutesSince(b.last_seen)) + '</td>' +
      '<td><button class="btn ghost small" data-rm-booth="' + escapeHtml(b.id) + '">Remover</button></td>' +
      '</tr>';
  }).join('');
  body.querySelectorAll('[data-rm-booth]').forEach(btn => {
    btn.addEventListener('click', () => removeBooth(btn.getAttribute('data-rm-booth')));
  });
}

async function removeBooth(id) {
  if (!id) return;
  if (!confirm('Remover a cabine? O dispositivo precisará parear novamente.')) return;
  try {
    const { ok, data } = await api('/api/admin/booths/' + encodeURIComponent(id), 'DELETE');
    if (!ok) { toast('Falha ao remover a cabine.', 'err'); return; }
    renderState(data);
    toast('Cabine removida.', 'ok');
  } catch (err) {
    if (err.message === 'unauthorized') return;
    toast('Falha ao remover a cabine.', 'err');
  }
}
