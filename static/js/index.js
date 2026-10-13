// index.js — Hub de Controle: consulta periodica do estado do sistema.
const REFRESH_MS = 5000;
const RESULTS_DELAY = window.VOTACAO_RESULTS_DELAY ?? 5;
const $ = (id) => document.getElementById(id);
const overallDot = $('overall-dot'), overallText = $('overall-text');
const clockEl = $('clock');

function setDot(el, kind) { el.className = 'dot' + (kind ? ' ' + kind : ''); }
function tickClock() { clockEl.textContent = new Date().toLocaleString('pt-BR'); }

async function refresh() {
  tickClock();
  try {
    const [sRes, rRes] = await Promise.all([
      fetch('/api/booth/status', { cache: 'no-store' }),
      fetch('/api/results', { cache: 'no-store' }),
    ]);
    const s = await sRes.json();
    const r = await rRes.json().catch(() => ({}));

    const pollOpen = !!s.poll_open;
    const tokens = s.voting_tokens ?? 0;
    const boothsOnline = s.booths_online ?? 0;
    const totalVotes = r.total ?? 0;

    $('m-poll').textContent = pollOpen ? ('#' + (s.poll_id ?? '–')) : 'Fechado';
    $('m-tokens').textContent = tokens;
    $('m-booths').textContent = boothsOnline;
    $('m-votes').textContent = totalVotes;

    setDot(overallDot, pollOpen ? 'ok' : 'warn');
    overallText.textContent = pollOpen ? 'Votação aberta' : 'Aguardando plebiscito';

    if (!pollOpen) { setDot($('st-checkin'), 'warn'); $('st-checkin-txt').textContent = 'Fechado'; }
    else if (tokens > 0) { setDot($('st-checkin'), 'ok'); $('st-checkin-txt').textContent = 'Fila: ' + tokens; }
    else { setDot($('st-checkin'), 'ok'); $('st-checkin-txt').textContent = 'Aberto'; }

    if (boothsOnline > 0) { setDot($('st-cabine'), 'ok'); $('st-cabine-txt').textContent = boothsOnline + ' online'; }
    else { setDot($('st-cabine'), 'warn'); $('st-cabine-txt').textContent = 'Nenhuma online'; }

    setDot($('st-painel'), pollOpen ? 'ok' : ''); $('st-painel-txt').textContent = RESULTS_DELAY + 's';
    setDot($('st-admin'), ''); $('st-admin-txt').textContent = 'Protegido por token';
  } catch (err) {
    setDot(overallDot, 'err');
    overallText.textContent = 'Sem conexão';
  }
}

const themeBtn = $('theme-toggle');
function renderThemeBtn() {
  const t = document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
  themeBtn.textContent = t === 'dark' ? '🌙 Modo escuro' : '☀️ Modo claro';
}
themeBtn.addEventListener('click', () => { window.VotacaoTheme.toggle(); renderThemeBtn(); });
window.addEventListener('storage', (e) => { if (e.key === 'votacao_theme') renderThemeBtn(); });
renderThemeBtn();

$('refresh-btn').addEventListener('click', refresh);
window.addEventListener('load', refresh);
setInterval(refresh, REFRESH_MS);
