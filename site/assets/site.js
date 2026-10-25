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
        var tw = pre.querySelector('#typewriter');
        var text = tw ? (tw.getAttribute('data-text') || '') : pre.innerText.replace(/\s+$/, '');
        copyText(text, btn);
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

  /* ---------------- 改版新增：滚动叙事 / 链路点亮 / 标签页 / 打字机 ----------------
     全部为渐进增强：无 IntersectionObserver 或减少动效时直接呈现最终状态。
     这些元素只出现在首页，子页面没有对应节点时各 init 自动跳过。 */

  var reduceMotion = false;
  try {
    reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch (e) { /* 忽略 */ }

  function observe(el, threshold, cb) {
    if (!el) { return; }
    if (reduceMotion || !('IntersectionObserver' in window)) { cb(el); return; }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) { cb(el); io.disconnect(); }
      });
    }, { threshold: threshold });
    io.observe(el);
  }

  function initReveal() {
    document.querySelectorAll('.reveal, .loop-wrap, .trust-diagram, .hero-stage').forEach(function (el) {
      observe(el, 0.22, function (t) { t.classList.add('inview'); });
    });
  }

  function initPain() {
    var stage = document.getElementById('pain-stage');
    if (!stage) { return; }
    observe(stage, 0.35, function (t) { t.classList.add('gather'); });
  }

  function initPipe() {
    var track = document.getElementById('pipe-track');
    if (!track) { return; }
    var steps = track.querySelectorAll('.pipe-step');
    observe(track, 0.15, function () {
      steps.forEach(function (step, i) {
        var light = function () {
          step.classList.add('lit');
          track.style.setProperty('--pipe-p', ((i + 1) / steps.length * 100) + '%');
        };
        if (reduceMotion) { light(); }
        else { window.setTimeout(light, 260 * i); }
      });
    });
  }

  function initTabs() {
    var wrap = document.getElementById('install-tabs');
    if (!wrap) { return; }
    var tabs = wrap.querySelectorAll('.tab');
    var panels = wrap.querySelectorAll('.tab-panel');
    tabs.forEach(function (tab) {
      tab.addEventListener('click', function () {
        tabs.forEach(function (t) {
          t.classList.toggle('is-on', t === tab);
          t.setAttribute('aria-selected', t === tab ? 'true' : 'false');
        });
        panels.forEach(function (p) {
          var on = p.getAttribute('data-panel') === tab.getAttribute('data-tab');
          p.classList.toggle('is-on', on);
          if (on) { p.removeAttribute('hidden'); } else { p.setAttribute('hidden', ''); }
        });
      });
    });
  }

  function initTypewriter() {
    var el = document.getElementById('typewriter');
    if (!el) { return; }
    var full = el.getAttribute('data-text') || '';
    if (reduceMotion || !('IntersectionObserver' in window)) { el.textContent = full; return; }
    observe(el, 0.4, function () {
      var i = 0;
      var timer = window.setInterval(function () {
        i += 2;
        el.textContent = full.slice(0, i);
        if (i >= full.length) { window.clearInterval(timer); }
      }, 24);
    });
  }

  initLangToggle();
  initCopyButtons();
  initReveal();
  initPain();
  initPipe();
  initTabs();
  initTypewriter();
  applyLang(readLang());
})();
