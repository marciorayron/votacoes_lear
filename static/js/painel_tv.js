// painel_tv.js — Painel público de resultados (Chart.js com fallback CSS).
const REFRESH_MS = 5000;         // atualizacao periodica do painel
const PALETTE = ['#38bdf8','#f472b6','#facc15','#34d399','#a78bfa','#fb923c'];

const questionEl = document.getElementById('question');
const subtitleEl = document.getElementById('subtitle');
const totalEl    = document.getElementById('total');
const pendingEl  = document.getElementById('pending');
const tokensEl   = document.getElementById('tokens');
const updatedEl  = document.getElementById('updated');
const delayEl    = document.getElementById('delay');
const delayModeEl = document.getElementById('delay-mode');
const networkEl  = document.getElementById('network-mode');
const liveDot    = document.getElementById('live-dot');
const liveText   = document.getElementById('live-text');
const canvas     = document.getElementById('chart');
const barsEl     = document.getElementById('bars');
const emptyEl    = document.getElementById('empty');

const HAS_CHART = typeof Chart !== 'undefined';
let chart = null;

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// Plugin simples para escrever o valor ao lado de cada barra.
const valueLabels = {
  id: 'valueLabels',
  afterDatasetsDraw(c) {
    const { ctx } = c;
    const ds = c.getDatasetMeta(0);
    ctx.save();
    ctx.fillStyle = cssVar('--fg') || '#eef4ff';
    ctx.font = '700 ' + Math.max(14, Math.round(c.height / 22)) + 'px system-ui';
    ctx.textBaseline = 'middle';
    ds.data.forEach((bar, i) => {
      const v = c.data.datasets[0].data[i];
      ctx.fillText(String(v), bar.x + 12, bar.y);
    });
    ctx.restore();
  }
};

function setLive(state, text) {
  liveDot.className = 'dot' + (state === 'ok' ? '' : ' ' + state);
  liveText.textContent = text;
}

function showEmpty(message) {
  if (chart) { chart.destroy(); chart = null; }
  canvas.style.display = 'none';
  barsEl.style.display = 'none';
  emptyEl.style.display = 'flex';
  emptyEl.textContent = message;
}

function renderChart(question, options, total) {
  emptyEl.style.display = 'none';
  canvas.style.display = 'block';
  barsEl.style.display = 'none';

  const labels = options.map(o => o.label);
  const data   = options.map(o => o.count);
  const colors = options.map((_, i) => PALETTE[i % PALETTE.length]);

  if (!HAS_CHART) { renderFallbackBars(options); return; }

  if (chart) {
    chart.data.labels = labels;
    chart.data.datasets[0].data = data;
    chart.data.datasets[0].backgroundColor = colors;
    chart.update();
    return;
  }

  chart = new Chart(canvas.getContext('2d'), {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        data, backgroundColor: colors, borderRadius: 12,
        borderSkipped: false, maxBarThickness: 120
      }]
    },
    options: {
      indexAxis: 'y',
      responsive: true, maintainAspectRatio: false,
      animation: { duration: 600 },
      layout: { padding: { right: 64 } },
      plugins: { legend: { display: false } },
      scales: {
        x: {
          beginAtZero: true, ticks: { color: cssVar('--muted') || '#8ea3c9',
            font: { size: Math.max(12, window.innerHeight / 40) },
            precision: 0 },
          grid: { color: cssVar('--line') || '#1c2b45' }
        },
        y: {
          ticks: { color: cssVar('--fg') || '#eef4ff',
            font: { size: Math.max(14, window.innerHeight / 30), weight: '700' } },
          grid: { display: false }
        }
      }
    },
    plugins: [valueLabels]
  });
}

// Fallback puro-CSS quando o CDN do Chart.js nao esta acessivel.
function renderFallbackBars(options) {
  canvas.style.display = 'none';
  emptyEl.style.display = 'none';
  barsEl.style.display = 'flex';

  const max = Math.max(1, ...options.map(o => o.count));
  barsEl.innerHTML = '';
  options.forEach((o, i) => {
    const pct = (o.count / max) * 100;
    const row = document.createElement('div');
    row.className = 'bar-row';
    row.innerHTML =
      '<div class="bar-top"><span>' + escapeHtml(o.label) + '</span>' +
      '<span>' + o.count + '</span></div>' +
      '<div class="bar-track"><div class="bar-fill" style="width:' + pct +
      '%;background:' + PALETTE[i % PALETTE.length] + '"></div></div>';
    barsEl.appendChild(row);
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));
}

// ------------------ Sincronizacao com o servidor ------------------
async function refresh() {
  try {
    const r = await fetch('/api/results', { cache: 'no-store' });
    const d = await r.json();

    if (d.results_delay !== undefined) delayEl.textContent = d.results_delay;
    if (delayModeEl) {
      delayModeEl.textContent = d.results_delay_minutes
        ? '(janelas de ' + d.results_delay_minutes + ' min)' : '';
    }
    if (networkEl && d.network_isolation) networkEl.textContent = d.network_isolation;

    if (!d.question || d.status === 'no_poll') {
      questionEl.textContent = 'Aguardando plebiscito…';
      subtitleEl.textContent = 'Nenhuma votação ativa no momento.';
      totalEl.textContent = '0';
      pendingEl.textContent = '0';
      tokensEl.textContent = d.voting_tokens ?? 0;
      showEmpty('📊 Nenhum plebiscito em andamento.');
    } else {
      questionEl.textContent = d.question;
      subtitleEl.textContent = d.status === 'open'
        ? 'Votação em andamento · apuração anônima'
        : 'Votação encerrada · resultado final';

      totalEl.textContent = d.total;
      pendingEl.textContent = d.total_pending_display ?? 0;
      tokensEl.textContent = d.voting_tokens ?? 0;

      const hasVotes = d.options.some(o => o.count > 0);
      if (!hasVotes) showEmpty('📊 Aguardando os primeiros votos…');
      else renderChart(d.question, d.options, d.total);
    }

    updatedEl.textContent = new Date().toLocaleTimeString('pt-BR');
    setLive('ok', d.status === 'open' ? 'ao vivo' : 'encerrado');
  } catch (err) {
    setLive('err', 'sem conexão');
  }
}

// Tema global: botao + re-render do grafico ao sincronizar com outra tela.
const themeToggle = document.getElementById('theme-toggle');
function renderThemeToggle() {
  const t = document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
  if (themeToggle) themeToggle.textContent = t === 'dark' ? '🌙 Tema' : '☀️ Tema';
}
if (themeToggle) themeToggle.addEventListener('click', () => { window.VotacaoTheme.toggle(); renderThemeToggle(); });
window.addEventListener('storage', (e) => { if (e.key === 'votacao_theme') renderThemeToggle(); });
document.addEventListener('themechange', () => {
  renderThemeToggle();
  if (chart) { chart.destroy(); chart = null; }
  refresh();
});
renderThemeToggle();

window.addEventListener('load', refresh);
setInterval(refresh, REFRESH_MS);
window.addEventListener('resize', () => { if (chart) chart.resize(); });
