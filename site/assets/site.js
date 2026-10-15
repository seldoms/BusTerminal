/* BusTerminal 站点脚本：语言切换 + 代码复制
   无依赖、无外部请求。 */
(function () {
  'use strict';

  var STORAGE_KEY = 'bt-lang';
  var DEFAULT_LANG = 'zh';
  var dict = window.I18N || {};

  function readLang() {
    var fromUrl = null;
    try {
      fromUrl = new URLSearchParams(window.location.search).get('lang');
    } catch (e) {
      fromUrl = null;
    }
    if (fromUrl && dict[fromUrl]) { return fromUrl; }
    try {
      var saved = window.localStorage.getItem(STORAGE_KEY);
      if (saved && dict[saved]) { return saved; }
    } catch (e) { /* 隐私模式下 localStorage 可能抛异常 */
    }
    return DEFAULT_LANG;
  }

  function applyLang(lang) {
    var table = dict[lang];
    if (!table) { return; }

    document.documentElement.setAttribute('lang', lang === 'zh' ? 'zh-CN' : 'en');

    document.querySelectorAll('[data-i18n]').forEach(function (el) {
      var v = table[el.getAttribute('data-i18n')];
      if (typeof v === 'string') { el.textContent = v; }
    });

    document.querySelectorAll('[data-i18n-html]').forEach(function (el) {
      var v = table[el.getAttribute('data-i18n-html')];
      if (typeof v === 'string') { el.innerHTML = v; }
    });

    var titleKey = document.body.getAttribute('data-title-key');
    if (titleKey && table[titleKey]) { document.title = table[titleKey]; }

    // 复制按钮的文案（未点击状态下）
    document.querySelectorAll('.code .copy').forEach(function (btn) {
      if (!btn.dataset.copied) { btn.textContent = table['common.copy'] || btn.textContent; }
    });

    var toggle = document.getElementById('lang-toggle');
    if (toggle) {
      toggle.textContent = lang === 'zh' ? 'EN' : '中文';
      toggle.setAttribute('aria-label',
        lang === 'zh' ? 'Switch to English' : '切换到中文');
    }

    try { window.localStorage.setItem(STORAGE_KEY, lang); } catch (e) { /* 忽略 */ }
  }

  function setLang(lang) {
    applyLang(lang);
    try {
      var url = new URL(window.location.href);
      if (lang === DEFAULT_LANG) { url.searchParams.delete('lang'); }
      else { url.searchParams.set('lang', lang); }
      window.history.replaceState(null, '', url.toString());
    } catch (e) { /* 忽略 */ }
  }

  function copyText(text, btn) {
    var done = function () {
      var table = dict[readLang()] || {};
      btn.dataset.copied = '1';
      btn.textContent = table['common.copied'] || '✓';
      window.setTimeout(function () {
        delete btn.dataset.copied;
        var t = dict[readLang()] || {};
        btn.textContent = t['common.copy'] || 'Copy';
      }, 1400);
    };

    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () { fallback(text, done); });
    } else {
      fallback(text, done);
    }
  }

  function fallback(text, done) {
    try {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      done();
    } catch (e) { /* 复制失败时静默，用户可以手动选中 */ }
  }

  function initCopyButtons() {
    document.querySelectorAll('.code .copy').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var pre = btn.parentNode.querySelector('pre');
        if (!pre) { return; }
        copyText(pre.innerText.replace(/\s+$/, ''), btn);
      });
    });
  }

  function initLangToggle() {
    var toggle = document.getElementById('lang-toggle');
    if (!toggle) { return; }
    toggle.addEventListener('click', function () {
      setLang(readLang() === 'zh' ? 'en' : 'zh');
    });
  }

  initLangToggle();
  initCopyButtons();
  applyLang(readLang());
})();
