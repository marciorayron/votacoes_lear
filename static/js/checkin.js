// checkin.js — Mesa de check-in com leitor NFC (HID). Cooldown anti duplo-swap.
const COOLDOWN_MS = Math.round((window.VOTACAO_COOLDOWN ?? 2) * 1000);
const badgeInput  = document.getElementById('badge');
const statusBox   = document.getElementById('status');
const statusMsg   = statusBox.querySelector('.msg');
const statusSub   = document.getElementById('sub');
const tokensEl    = document.getElementById('tokens');
const queueHint   = document.getElementById('queue-hint');
const badgeView   = document.getElementById('badge-view');
const progress    = document.getElementById('progress');
const cooldownLbl = document.getElementById('cooldown-label');
const boothBanner = document.getElementById('booth-banner');
const boothCap    = document.getElementById('booth-cap');
const boothNumber = document.getElementById('booth-number');
const boothSub    = document.getElementById('booth-sub');
const panel       = document.getElementById('checkin-panel');
const queueWarning = document.getElementById('queue-warning');
const queueWarningText = document.getElementById('queue-warning-text');

let cooling = false;
let lastBadge = '';
let lastBadgeAt = 0;

// Alerta de fila cheia: limite vem do servidor (settings.queue_warning_threshold).
let queueThreshold = 5;    // valor padrao ate a primeira resposta do servidor
let queueWarned = false;   // evita repetir o bipe enquanto a fila permanece cheia

// ---------------- Áudio (Web Audio API — sem arquivos externos) ----------------
// Bipe agudo e agradável = sucesso/liberação; buzina grave = erro.
let audioCtx = null;
let soundOn = true;

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

// Toca um tom simples: freq (Hz), atraso (s), duração (s).
function tone(freq, startAt, dur, type, peak) {
  if (!soundOn) return;
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
// Bipe SUAVE e ascendente de alerta (fila cheia) — avisa sem assustar.
function beepWarn()    { tone(660, 0, 0.16, 'sine', 0.16); tone(880, 0.18, 0.20, 'sine', 0.14); }

// O navegador só libera áudio após um gesto do usuário.
let audioPrimed = false;
function primeAudio() { if (audioPrimed) return; audioPrimed = true; audioReady(); }
['pointerdown', 'keydown', 'touchstart'].forEach(ev =>
  window.addEventListener(ev, primeAudio, { passive: true }));

const soundToggle = document.getElementById('sound-toggle');
if (soundToggle) {
  soundToggle.addEventListener('click', () => {
    soundOn = !soundOn;
    soundToggle.textContent = soundOn ? '🔊 Som' : '🔇 Mudo';
    soundToggle.setAttribute('aria-pressed', soundOn ? 'true' : 'false');
    if (soundOn) beepSuccess();
  });
}

function setStatus(kind, msg, sub) {
  statusBox.className = 'status ' + kind;
  statusMsg.textContent = msg;
  statusSub.textContent = sub || '';
}

// Extrai o rotulo da cabine de forma tolerante (label, nome ou numero).
function boothLabel(booth, fallbackLabel) {
  if (booth) {
    if (booth.label) return String(booth.label);
    if (booth.name) return String(booth.name);
    if (booth.number !== undefined && booth.number !== null) {
      return 'Cabine ' + String(booth.number).padStart(2, '0');
    }
  }
  return fallbackLabel ? String(fallbackLabel) : '';
}

// Destaca a cabine alocada em fonte gigante (leitura a distancia).
// `opts.reserved === false` = cabine especifica ainda ocupada (fila): o
// eleitor deve se dirigir a ela e aguardar a liberacao.
function showBoothBanner(booth, instruction, opts) {
  const options = opts || {};
  const label = boothLabel(booth, options.label).toUpperCase();
  const waiting = options.reserved === false;
  boothCap.textContent = label
    ? (waiting ? 'Aguarde a liberação da' : 'Dirija-se à')
    : 'Dirija-se a';
  boothNumber.textContent = label || 'QUALQUER CABINE';
  boothSub.textContent = instruction || (label
    ? (waiting
        ? 'A cabine será liberada assim que o eleitor anterior concluir.'
        : 'Dirija-se a esta cabine para votar.')
    : 'Dirija-se a qualquer cabine disponível.');
  boothBanner.classList.add('show');
}

function hideBoothBanner() {
  boothBanner.classList.remove('show');
}

// Aplica o estado da fila: liga o ALERTA AMARELO quando a fila enche.
// Também exibe o banner e toca um bipe suave na transição para "fila cheia".
function applyQueueState(tokens, threshold) {
  if (Number.isFinite(threshold) && threshold >= 1) queueThreshold = threshold;
  const full = Number.isFinite(tokens) && tokens >= queueThreshold;
  if (panel) panel.classList.toggle('warning-high-queue', full);
  if (queueWarning) queueWarning.hidden = !full;
  if (full && queueWarningText) {
    queueWarningText.textContent = 'FILA CHEIA (' + tokens +
      ' aguardando cabine) — Aguarde liberação das cabines';
  }
  if (full && !queueWarned) { beepWarn(); queueWarned = true; }
  else if (!full) { queueWarned = false; }
}

function refreshTokens() {
  fetch('/api/booth/status', { cache: 'no-store' })
    .then(r => r.json())
    .then(d => {
      tokensEl.textContent = (d.voting_tokens ?? '–');
      queueHint.textContent = d.poll_open ? (d.available ? 'Há vaga' : 'Sem vaga') : 'Sem plebiscito';
      applyQueueState(d.voting_tokens, d.queue_warning_threshold);
    })
    .catch(() => { tokensEl.textContent = '–'; queueHint.textContent = 'offline'; });
}

// Bloqueio do input durante o cooldown (anti duplo-swap).
function startCooldown() {
  cooling = true;
  badgeInput.disabled = true;
  badgeInput.value = '';
  const start = performance.now();
  function tick(now) {
    const left = Math.max(0, COOLDOWN_MS - (now - start));
    progress.style.width = (left / COOLDOWN_MS * 100) + '%';
    if (left <= 0) {
      cooling = false;
      badgeInput.disabled = false;
      progress.style.width = '0%';
      badgeInput.focus();
      return;
    }
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

async function submitBadge(raw) {
  const badge = (raw || '').trim();
  if (!badge) return;

  // Debounce: ignora leituras repetidas do MESMO crachá em curto intervalo.
  const now = Date.now();
  if (badge === lastBadge && (now - lastBadgeAt) < COOLDOWN_MS) {
    setStatus('warn', 'Leitura repetida ignorada', 'Aguarde o cooldown antes da próxima leitura.');
    return;
  }
  lastBadge = badge;
  lastBadgeAt = now;
  badgeView.textContent = badge;

  setStatus('info', 'Validando crachá...', '');

  try {
    const resp = await fetch('/api/checkin', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ badge_id: badge })
    });
    const data = await resp.json().catch(() => ({}));
    hideBoothBanner();

    if (resp.ok && data.ok) {
      beepSuccess();
      showBoothBanner(data.booth, data.instruction, {
        label: data.booth_label,
        reserved: data.reserved
      });
      setStatus('ok', 'Liberado para votar ✅',
                data.instruction || 'Dirija-se a uma cabine de votação.');
    } else if (data.status === 'already_voted') {
      beepError();
      setStatus('warn', 'Crachá já votou', 'Este crachá já realizou o check-in.');
    } else if (data.status === 'no_poll') {
      beepError();
      setStatus('err', 'Nenhum plebiscito aberto', 'Aguarde o administrador iniciar a votação.');
    } else if (resp.status === 401) {
      beepError();
      setStatus('err', 'Acesso negado', 'Falha de autenticação do dispositivo.');
    } else {
      beepError();
      setStatus('err', 'Falha no check-in', data.detail || data.error || 'Erro inesperado.');
    }
  } catch (err) {
    beepError();
    hideBoothBanner();
    setStatus('err', 'Servidor indisponível', 'Verifique a rede/roteador e tente novamente.');
  } finally {
    refreshTokens();
    startCooldown();
  }
}

// O leitor HID "digita" o crachá e envia Enter -> aqui tratamos.
badgeInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    if (cooling) return;
    submitBadge(badgeInput.value);
  }
});

// Mantém o foco no campo (essencial para o leitor HID).
setInterval(() => {
  if (!cooling && document.activeElement !== badgeInput) badgeInput.focus();
}, 800);

cooldownLbl.textContent = (window.VOTACAO_COOLDOWN ?? 2);
// Tema global (lê localStorage e sincroniza com as demais telas/abas).
const themeToggle = document.getElementById('theme-toggle');
function renderThemeToggle() {
  const t = document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
  if (themeToggle) themeToggle.textContent = t === 'dark' ? '🌙 Tema' : '☀️ Tema';
}
if (themeToggle) themeToggle.addEventListener('click', () => { window.VotacaoTheme.toggle(); renderThemeToggle(); });
window.addEventListener('storage', (e) => { if (e.key === 'votacao_theme') renderThemeToggle(); });
renderThemeToggle();
window.addEventListener('load', () => { badgeInput.focus(); refreshTokens(); });
setInterval(refreshTokens, 3000);
