// cabine.js — Kiosk de votação: registro/pareamento, heartbeat e ciclo de voto.
const SCREEN_WAIT = document.getElementById('screen-wait');
const SCREEN_VOTE = document.getElementById('screen-vote');
const SCREEN_DONE = document.getElementById('screen-done');
const SCREEN_PAIR = document.getElementById('screen-pair');
const waitTitle   = document.getElementById('wait-title');
const waitSub     = document.getElementById('wait-sub');
const waitDot     = document.getElementById('wait-dot');
const waitStatus  = document.getElementById('wait-status');
const footDot     = document.getElementById('foot-dot');
const footStatus  = document.getElementById('foot-status');
const questionEl  = document.getElementById('question');
const optionsEl   = document.getElementById('options');
const confirmBtn  = document.getElementById('confirm-btn');
const cancelBtn   = document.getElementById('cancel-btn');
const doneIcon    = document.getElementById('done-icon');
const doneTitle   = document.getElementById('done-title');
const doneSub     = document.getElementById('done-sub');
const pairInput   = document.getElementById('pair-input');
const pairBtn     = document.getElementById('pair-btn');
const pairMsg     = document.getElementById('pair-msg');

const POLL_INTERVAL_MS = 1000;   // cabine checa disponibilidade a cada 1s
const DONE_HOLD_MS     = 4000;   // tempo exibindo a confirmacao

let state = 'wait';        // wait | vote | done | pair
let currentPollId = null;
let selectedOptionId = null;
let submitting = false;
let statusTimer = null;

const HEARTBEAT_MS = 5000;              // cadência do heartbeat da cabine
const titleEl = document.getElementById('title');
let boothId = null;                     // id estável deste kiosk (localStorage)
let boothLabel = 'Cabine';
let pairingToken = null;                // prova de pareamento (localStorage)
let wasAssigned = false;                // já estava liberada? (bip na transição)
let heartbeatTimer = null;

// ---------------- Áudio (Web Audio API — sem arquivos externos) ----------------
// Bipe agudo e agradável = sucesso/liberação; buzina grave = erro.
let audioCtx = null;
function audioReady() {
  if (audioCtx) {
    if (audioCtx.state === 'suspended') audioCtx.resume();
    return audioCtx;
  }
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return null;
    audioCtx = new Ctx();
  } catch (e) { audioCtx = null; }
  return audioCtx;
}
function tone(freq, startAt, dur, type, peak) {
  const ctx = audioReady();
  if (!ctx) return;
  const t0 = ctx.currentTime + (startAt || 0);
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.type = type || 'sine';
  osc.frequency.setValueAtTime(freq, t0);
  gain.gain.setValueAtTime(0.0001, t0);
  gain.gain.exponentialRampToValueAtTime(peak || 0.25, t0 + 0.012);
  gain.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
  osc.connect(gain); gain.connect(ctx.destination);
  osc.start(t0); osc.stop(t0 + dur + 0.03);
}
function beepSuccess() { tone(880, 0, 0.12, 'sine', 0.28); tone(1320, 0.11, 0.16, 'sine', 0.24); }
function beepError()   { tone(200, 0, 0.18, 'square', 0.20); tone(150, 0.16, 0.30, 'square', 0.18); }
let audioPrimed = false;
function primeAudio() { if (audioPrimed) return; audioPrimed = true; audioReady(); }
['pointerdown', 'keydown', 'touchstart'].forEach(ev =>
  window.addEventListener(ev, primeAudio, { passive: true }));

// ---------------- Identidade e registro do kiosk ----------------
function makeBoothId() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  return 'booth-' + Math.random().toString(36).slice(2) + '-' + Date.now().toString(36);
}
function loadBoothId() {
  try {
    let id = localStorage.getItem('voting_booth_id');
    if (!id) { id = makeBoothId(); localStorage.setItem('voting_booth_id', id); }
    return id;
  } catch (e) { return makeBoothId(); }
}
function loadPairingToken() {
  try { return localStorage.getItem('voting_pairing_token') || null; }
  catch (e) { return null; }
}
function savePairingToken(token) {
  pairingToken = token;
  try { if (token) localStorage.setItem('voting_pairing_token', token); }
  catch (e) { /* localStorage indisponivel */ }
}

// Registra a cabine. Retorna {ok} | {pairing:true} | {offline:true}.
async function registerBooth(pin) {
  const body = { booth_id: boothId };
  if (pairingToken) body.pairing_token = pairingToken;
  if (pin) body.pairing_pin = pin;
  try {
    const r = await fetch('/api/booth/register', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    const d = await r.json().catch(() => ({}));
    if (r.ok && d.ok && d.booth) {
      boothLabel = d.booth.label;
      if (d.booth.pairing_token) savePairingToken(d.booth.pairing_token);
      titleEl.textContent = boothLabel + ' — Votação';
      document.title = boothLabel + ' — Votação';
      return { ok: true };
    }
    if (r.status === 403) return { pairing: true };   // PIN necessario/incorreto
    return { ok: false };
  } catch (e) { return { offline: true }; }
}

// ---------------- Pareamento (PIN) ----------------
function showPairing(message) {
  state = 'pair';
  pairMsg.textContent = message || '';
  show(SCREEN_PAIR);
  pairInput.value = '';
  pairInput.focus();
}
async function submitPairing() {
  const pin = (pairInput.value || '').trim();
  if (!pin) { pairMsg.textContent = 'Digite o PIN.'; return; }
  pairBtn.disabled = true;
  pairMsg.textContent = 'Validando…';
  const res = await registerBooth(pin);
  pairBtn.disabled = false;
  if (res.ok) {
    beepSuccess();
    state = 'wait';
    show(SCREEN_WAIT);
    checkStatus();
  } else if (res.pairing) {
    beepError();
    pairMsg.textContent = 'PIN incorreto. Tente novamente.';
  } else if (res.offline) {
    pairMsg.textContent = 'Servidor indisponível. Tente novamente.';
  } else {
    pairMsg.textContent = 'Não foi possível parear. Tente novamente.';
  }
}
pairBtn.addEventListener('click', submitPairing);
pairInput.addEventListener('keydown', e => { if (e.key === 'Enter') submitPairing(); });

async function sendHeartbeat(status) {
  if (!boothId || state === 'pair') return;
  const body = { booth_id: boothId };
  if (pairingToken) body.pairing_token = pairingToken;
  if (status) body.status = status;
  try {
    const r = await fetch('/api/booth/heartbeat', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    if (r.status === 404) await reRegister();   // servidor reiniciou / cabine removida
  } catch (e) { /* silencioso */ }
}
async function reRegister() {
  const res = await registerBooth();
  if (res.pairing) showPairing('Cabine não pareada. Informe o PIN.');
}
async function initBooth() {
  boothId = loadBoothId();
  pairingToken = loadPairingToken();
  const res = await registerBooth();
  if (res.pairing) showPairing('');
  heartbeatTimer = setInterval(
    () => sendHeartbeat(state === 'wait' ? null : 'voting'),
    HEARTBEAT_MS);
}

function show(screen) {
  [SCREEN_WAIT, SCREEN_VOTE, SCREEN_DONE, SCREEN_PAIR].forEach(s => s.classList.remove('active'));
  screen.classList.add('active');
}

function setFooter(ok, text) {
  footDot.className = 'dot ' + (ok ? 'ok' : 'err');
  footStatus.textContent = text;
}

function setWait(kind, title, sub) {
  waitDot.className = 'dot ' + kind;
  waitTitle.textContent = title;
  waitSub.textContent = sub;
  waitStatus.textContent = kind === 'ok' ? 'Liberado'
    : kind === 'warn' ? 'Aguardando' : kind === 'err' ? 'Indisponível' : 'Conectando…';
}
// ------------------ Ciclo de disponibilidade ------------------
async function checkStatus() {
  if (state === 'pair') return;   // aguardando pareamento
  try {
    const url = '/api/booth/status' +
      (boothId ? '?booth_id=' + encodeURIComponent(boothId) : '');
    const r = await fetch(url, { cache: 'no-store' });
    const d = await r.json();
    setFooter(true, 'online');

    // Modo legado: nenhuma cabine registrada -> usa a disponibilidade global.
    const legacy = (d.booths_online || 0) === 0;
    const mine = Boolean(d.assigned) || (legacy && d.available);

    if (mine && d.available) {
      if (!wasAssigned) { beepSuccess(); wasAssigned = true; }
      setWait('ok', 'Cabine liberada!', 'Preparando o voto…');
      // O servidor ja devolve o plebiscito ativo quando esta cabine foi
      // atribuida (reserva dirigida ou token global sem dono).
      if (state === 'wait') await loadPoll(d.active_poll);
    } else if (d.available) {
      // Há vaga, mas a liberação foi direcionada a outra cabine.
      wasAssigned = false;
      setWait('', 'Aguardando a sua vez', 'Há uma liberação em andamento em outra cabine.');
    } else if (!d.poll_open) {
      wasAssigned = false;
      setWait('warn', 'Votação não iniciada', 'Aguarde o administrador iniciar o plebiscito.');
    } else {
      wasAssigned = false;
      setWait('', 'Aguardando liberação', 'Faça o check-in na mesa para liberar a cabine.');
    }
  } catch (err) {
    setFooter(false, 'offline');
    setWait('err', 'Servidor indisponível', 'Verifique a rede e aguarde reconexão.');
  }
}

// ------------------ Carregar plebiscito ------------------
// Recebe opcionalmente o plebiscito ja resolvido pelo /api/booth/status
// (assignedPoll). Sem ele, busca /api/poll como fallback.
async function loadPoll(assignedPoll) {
  try {
    let poll = assignedPoll || null;
    if (!poll) {
      const r = await fetch('/api/poll', { cache: 'no-store' });
      if (!r.ok) { return; } // sem plebiscito -> permanece na espera
      const d = await r.json();
      if (!d.ok || !d.poll) return;
      poll = d.poll;
    }

    currentPollId = poll.id;
    selectedOptionId = null;
    renderPoll(poll);
    state = 'vote';
    show(SCREEN_VOTE);
    sendHeartbeat('voting');   // renova a reserva desta cabine
  } catch (err) {
    setFooter(false, 'offline');
  }
}

function renderPoll(poll) {
  questionEl.textContent = poll.question;
  optionsEl.innerHTML = '';
  confirmBtn.disabled = true;

  poll.options.forEach((opt, idx) => {
    const btn = document.createElement('button');
    btn.className = 'option';
    btn.type = 'button';
    btn.dataset.optionId = opt.id;
    btn.innerHTML = '<span class="num">' + (idx + 1) + '</span>' +
                    '<span class="label">' + escapeHtml(opt.label) + '</span>' +
                    '<span class="check" aria-hidden="true">✓</span>';
    btn.addEventListener('click', () => selectOption(btn, opt.id));
    optionsEl.appendChild(btn);
  });
}

function selectOption(btn, optionId) {
  if (submitting) return;
  optionsEl.querySelectorAll('.option').forEach(b => b.classList.remove('selected'));
  btn.classList.add('selected');
  selectedOptionId = optionId;
  confirmBtn.disabled = false;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));
}

// ------------------ Confirmar voto ------------------
async function confirmVote() {
  if (!selectedOptionId || submitting) return;
  submitting = true;
  confirmBtn.disabled = true;
  confirmBtn.textContent = 'Registrando…';

  try {
    const r = await fetch('/api/vote', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ option_id: selectedOptionId, poll_id: currentPollId })
    });
    const d = await r.json().catch(() => ({}));

    if (r.ok && d.ok) {
      beepSuccess();
      showDone('✅', 'Voto registrado!', 'Obrigado por participar.');
    } else if (d.status === 'no_token') {
      beepError();
      showDone('⏳', 'Token indisponível', 'Sua vez foi liberada em outra cabine.');
    } else if (d.status === 'no_poll') {
      beepError();
      showDone('ℹ️', 'Votação encerrada', 'O plebiscito não está mais ativo.');
    } else {
      beepError();
      showDone('⚠️', 'Não foi possível registrar', d.detail || d.error || 'Tente novamente.');
    }
  } catch (err) {
    beepError();
    showDone('📡', 'Servidor indisponível', 'Verifique a rede antes de tentar novamente.');
  } finally {
    submitting = false;
    confirmBtn.textContent = 'Confirmar voto';
  }
}

function showDone(icon, title, sub) {
  state = 'done';
  doneIcon.textContent = icon;
  doneTitle.textContent = title;
  doneSub.textContent = sub;
  show(SCREEN_DONE);
  setTimeout(returnToWait, DONE_HOLD_MS);
}

function returnToWait() {
  state = 'wait';
  currentPollId = null;
  selectedOptionId = null;
  optionsEl.innerHTML = '';
  confirmBtn.disabled = true;
  show(SCREEN_WAIT);
  wasAssigned = false;
  sendHeartbeat('idle');   // libera a reserva desta cabine no servidor
  checkStatus();
}

confirmBtn.addEventListener('click', confirmVote);
cancelBtn.addEventListener('click', returnToWait);

// Regressa a espera se a cabine ficar ociosa na tela de voto (evita travar).
let idleTimer = setTimeout(returnToWait, 90000);
['click', 'touchstart', 'keydown'].forEach(ev =>
  document.addEventListener(ev, () => {
    clearTimeout(idleTimer);
    if (state === 'vote') idleTimer = setTimeout(returnToWait, 90000);
  }, { passive: true }));

// Tema global (localStorage: votacao_theme) sincronizado entre as telas.
const themeToggle = document.getElementById('theme-toggle');
function renderThemeToggle() {
  const t = document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
  if (themeToggle) themeToggle.textContent = t === 'dark' ? '🌙 Tema' : '☀️ Tema';
}
if (themeToggle) themeToggle.addEventListener('click', () => { window.VotacaoTheme.toggle(); renderThemeToggle(); });
window.addEventListener('storage', (e) => { if (e.key === 'votacao_theme') renderThemeToggle(); });
renderThemeToggle();

// Loop de verificacao.
window.addEventListener('load', async () => { await initBooth(); checkStatus(); });
statusTimer = setInterval(() => {
  if (state === 'wait') checkStatus();
  else setFooter(true, 'online');
}, POLL_INTERVAL_MS);
