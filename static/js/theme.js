// Tema global sincronizado entre todas as telas (localStorage: votacao_theme).
// Deve ser carregado de forma SÍNCRONA no <head> (antes do <style>) para
// evitar "flash" de tema ao abrir a pagina.
(function () {
  var K = 'votacao_theme';
  function norm(t) { return t === 'light' ? 'light' : 'dark'; }
  var saved = 'dark';
  try { saved = localStorage.getItem(K) || 'dark'; } catch (e) {}
  document.documentElement.setAttribute('data-theme', norm(saved));
  window.VotacaoTheme = {
    key: K,
    get: function () { return norm(document.documentElement.getAttribute('data-theme')); },
    apply: function (t) {
      var v = norm(t);
      document.documentElement.setAttribute('data-theme', v);
      try { localStorage.setItem(K, v); } catch (e) {}
      document.dispatchEvent(new CustomEvent('themechange', { detail: { theme: v } }));
      return v;
    },
    toggle: function () { return this.apply(this.get() === 'light' ? 'dark' : 'light'); }
  };
  window.addEventListener('storage', function (e) {
    if (e.key === K && e.newValue) {
      document.documentElement.setAttribute('data-theme', norm(e.newValue));
      document.dispatchEvent(new CustomEvent('themechange', { detail: { theme: norm(e.newValue) } }));
    }
  });
})();
