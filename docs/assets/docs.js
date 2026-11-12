// Скрипты архитектурной документации Telebrief.

// =======================================================================
// UI CONTROLS: THEME SWITCHER & COLLAPSIBLE SIDEBAR
// =======================================================================

window.setAppTheme = function (t) {
  const root = document.documentElement;
  if (t === "light") {
    root.dataset.theme = "light";
  } else if (t === "dark") {
    root.dataset.theme = "dark";
  } else {
    delete root.dataset.theme;
    t = "auto";
  }
  try {
    localStorage.setItem("telebrief-theme", t);
  } catch (e) {}

  document.querySelectorAll(".theme-switch-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.getAttribute("data-theme-val") === t);
  });

  if (typeof window.renderMermaidDiagrams === "function") {
    window.renderMermaidDiagrams();
  }
};

window.toggleSidebar = function (forceState) {
  const shells = document.querySelectorAll(".shell");
  const text = document.getElementById("sidebar-toggle-text");
  if (!shells.length) return;

  const willCollapse =
    typeof forceState === "boolean"
      ? forceState
      : !shells[0].classList.contains("sidebar-collapsed");
  shells.forEach((shell) => shell.classList.toggle("sidebar-collapsed", willCollapse));

  if (text) {
    text.textContent = willCollapse ? "Развернуть меню" : "Свернуть меню";
  }

  try {
    localStorage.setItem("telebrief-sidebar-collapsed", willCollapse ? "true" : "false");
  } catch (e) {}
};

window.activeShell = function () {
  return document.querySelector(".shell");
};

// =======================================================================
// FULLSCREEN VECTOR SLIDESHOW CONTROLLER (WITH PAN & ZOOM)
// =======================================================================

let slideshowIndex = 0;
let slideshowItems = [];
let currentScale = 1.0;
let currentPanX = 0;
let currentPanY = 0;
let isPanning = false;
let startPanX = 0;
let startPanY = 0;
let natSvgW = 1000;
let natSvgH = 800;

function updateSlideshowTransform() {
  const canvas = document.getElementById("slideshow-canvas");
  const zoomVal = document.getElementById("slideshow-zoom-val");
  const btnOrig = document.getElementById("btn-zoom-original");
  if (!canvas) return;

  canvas.style.transform = `translate(${currentPanX}px, ${currentPanY}px) scale(${currentScale})`;
  if (zoomVal) {
    zoomVal.textContent = Math.round(currentScale * 100) + "%";
  }
  if (btnOrig) {
    btnOrig.classList.toggle("active", Math.abs(currentScale - 1.0) < 0.03);
  }
}

window.zoomSlideshow = function (delta) {
  currentScale = Math.max(0.15, Math.min(4.0, currentScale + delta));
  updateSlideshowTransform();
};

window.setSlideshowScale = function (scale) {
  currentScale = scale;
  currentPanX = 0;
  currentPanY = 0;
  updateSlideshowTransform();
};

window.fitSlideshowToScreen = function () {
  const viewport = document.getElementById("slideshow-viewport");
  if (!viewport || !natSvgW || !natSvgH) {
    window.setSlideshowScale(1.0);
    return;
  }
  const availW = Math.max(200, viewport.clientWidth - 100);
  const availH = Math.max(200, viewport.clientHeight - 100);
  let fitScale = Math.min(availW / natSvgW, availH / natSvgH);
  if (fitScale > 1.25) fitScale = 1.25;
  if (fitScale < 0.15) fitScale = 0.15;
  currentScale = Math.round(fitScale * 100) / 100;
  currentPanX = 0;
  currentPanY = 0;
  updateSlideshowTransform();
};

function registerSlideshowItems() {
  slideshowItems = [];
  const shell = window.activeShell();
  if (!shell) return;
  shell.querySelectorAll("pre.mermaid, figure.l1-arch-fig").forEach((pre, index) => {
    // Select the actual diagram SVG, ignoring the badge icon
    const svg =
      pre.querySelector("svg.l1-arch") ||
      pre.querySelector("svg[id^='mermaid-']") ||
      pre.querySelector("svg:not(.diagram-expand-icon)");
    if (!svg) return;
    const section = pre.closest("section");
    const level = section ? section.getAttribute("data-level") || "DIAGRAM" : "DIAGRAM";
    let title = "Архитектурная диаграмма";
    if (section) {
      const h = section.querySelector("h2, h3, h1");
      if (h) title = h.textContent.trim();
    }
    slideshowItems.push({
      index: index,
      pre: pre,
      svg: svg,
      level: level,
      title: title,
    });
  });
}

window.openSlideshow = function (index) {
  registerSlideshowItems();
  if (slideshowItems.length === 0) return;

  if (typeof index !== "number" || index < 0 || index >= slideshowItems.length) {
    index = 0;
  }
  slideshowIndex = index;
  renderCurrentSlide();

  const modal = document.getElementById("slideshow-modal");
  if (modal) {
    modal.classList.add("open");
    modal.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
  }
};

window.closeSlideshow = function () {
  const modal = document.getElementById("slideshow-modal");
  if (modal) {
    modal.classList.remove("open");
    modal.setAttribute("aria-hidden", "true");
    document.body.style.overflow = "";
  }
};

window.navigateSlideshow = function (direction) {
  if (slideshowItems.length <= 1) return;
  slideshowIndex =
    (slideshowIndex + direction + slideshowItems.length) % slideshowItems.length;
  renderCurrentSlide();
};

function renderCurrentSlide() {
  const item = slideshowItems[slideshowIndex];
  if (!item) return;

  const counterEl = document.getElementById("slideshow-counter");
  const badgeEl = document.getElementById("slideshow-badge");
  const titleEl = document.getElementById("slideshow-title");
  const container = document.getElementById("slideshow-svg-container");

  if (counterEl) counterEl.textContent = `${item.index + 1} / ${slideshowItems.length}`;
  if (badgeEl) badgeEl.textContent = item.level;
  if (titleEl) titleEl.textContent = item.title;

  if (container) {
    container.innerHTML = "";
    // Clone original Mermaid vector SVG
    const clone = item.svg.cloneNode(true);

    // Natural dimensions from viewBox for 100% crisp vector typography
    const vb = (clone.getAttribute("viewBox") || "").split(/[ ,]+/).map(Number);
    natSvgW = vb[2] || (clone.getBBox ? clone.getBBox().width : 1000);
    natSvgH = vb[3] || (clone.getBBox ? clone.getBBox().height : 800);

    clone.removeAttribute("style");
    clone.setAttribute("width", Math.round(natSvgW));
    clone.setAttribute("height", Math.round(natSvgH));
    clone.style.width = Math.round(natSvgW) + "px";
    clone.style.height = Math.round(natSvgH) + "px";
    clone.style.display = "block";

    container.appendChild(clone);
  }

  // Calculate default view:
  // If diagram is reasonably sized (fits without severe shrinkage >= 65%), fit it.
  // Otherwise open at 100% crisp 1:1 original scale so text is never microscopic!
  const viewport = document.getElementById("slideshow-viewport");
  const availW = viewport ? viewport.clientWidth - 100 : 1200;
  const availH = viewport ? viewport.clientHeight - 100 : 700;
  const fitRatio = Math.min(availW / natSvgW, availH / natSvgH);

  if (fitRatio >= 0.65) {
    window.fitSlideshowToScreen();
  } else {
    window.setSlideshowScale(1.0);
  }
}

function initSlideshowListeners() {
  const viewport = document.getElementById("slideshow-viewport");
  if (!viewport) return;

  // Smooth wheel zoom
  viewport.addEventListener(
    "wheel",
    (e) => {
      e.preventDefault();
      const factor = e.deltaY < 0 ? 1.12 : 0.88;
      currentScale = Math.max(0.15, Math.min(4.0, currentScale * factor));
      updateSlideshowTransform();
    },
    { passive: false },
  );

  // Pan / Drag
  let clickStartX = 0,
    clickStartY = 0;

  viewport.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    isPanning = true;
    clickStartX = e.clientX;
    clickStartY = e.clientY;
    startPanX = e.clientX - currentPanX;
    startPanY = e.clientY - currentPanY;
    viewport.classList.add("grabbing");
  });

  window.addEventListener("mousemove", (e) => {
    if (!isPanning) return;
    currentPanX = e.clientX - startPanX;
    currentPanY = e.clientY - startPanY;
    updateSlideshowTransform();
  });

  window.addEventListener("mouseup", () => {
    if (isPanning) {
      isPanning = false;
      viewport.classList.remove("grabbing");
    }
  });

  // Double click toggles 1:1 vs Fit
  viewport.addEventListener("dblclick", () => {
    if (Math.abs(currentScale - 1.0) < 0.05) {
      window.fitSlideshowToScreen();
    } else {
      window.setSlideshowScale(1.0);
    }
  });

  // Close on backdrop click (if didn't drag)
  viewport.addEventListener("click", (e) => {
    const dist = Math.hypot(e.clientX - clickStartX, e.clientY - clickStartY);
    if (dist < 6 && (e.target === viewport || e.target.id === "slideshow-canvas")) {
      window.closeSlideshow();
    }
  });

  // Keyboard navigation
  window.addEventListener("keydown", (e) => {
    const modal = document.getElementById("slideshow-modal");
    if (!modal || !modal.classList.contains("open")) return;

    if (e.key === "Escape") {
      window.closeSlideshow();
    } else if (e.key === "ArrowLeft") {
      window.navigateSlideshow(-1);
    } else if (e.key === "ArrowRight") {
      window.navigateSlideshow(1);
    } else if (e.key === "+" || e.key === "=") {
      window.zoomSlideshow(0.15);
    } else if (e.key === "-" || e.key === "_") {
      window.zoomSlideshow(-0.15);
    } else if (e.key === "1") {
      window.setSlideshowScale(1.0);
    } else if (e.key === "0") {
      window.fitSlideshowToScreen();
    }
  });
}

(function () {
  function initUI() {
    // 1. Theme initialization
    try {
      const savedTheme = localStorage.getItem("telebrief-theme") || "auto";
      window.setAppTheme(savedTheme);
    } catch (e) {
      window.setAppTheme("auto");
    }

    // 2. Sidebar collapsed initialization
    try {
      const isSavedCollapsed = localStorage.getItem("telebrief-sidebar-collapsed") === "true";
      if (isSavedCollapsed) {
        window.toggleSidebar(true);
      }
    } catch (e) {}

    // 3. Hotkey: Ctrl+B or Cmd+B to toggle sidebar
    window.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key && e.key.toLowerCase() === "b") {
        e.preventDefault();
        window.toggleSidebar();
      }
    });

    initSlideshowListeners();

    if (window.matchMedia) {
      window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
        const theme = document.documentElement.dataset.theme;
        if (!theme || theme === "auto") {
          renderMermaidDiagrams();
        }
      });
    }
  }

  let isMermaidRendering = false;
  let pendingMermaidRender = false;

  function renderMermaidDiagrams() {
    if (typeof mermaid === "undefined") {
      setTimeout(renderMermaidDiagrams, 80);
      return;
    }
    if (isMermaidRendering) {
      pendingMermaidRender = true;
      return;
    }

    const shell = window.activeShell();
    if (!shell) return;
    const themeKey = document.documentElement.dataset.theme || "auto";
    const renderKey =
      themeKey +
      (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
        ? ":sys-dark"
        : ":sys-light");
    if (shell.dataset.mermaidKey === renderKey) {
      window.pendingScrollId = null;
      return;
    }
    shell.dataset.mermaidKey = renderKey;
    const diagrams = shell.querySelectorAll("pre.mermaid");

    isMermaidRendering = true;

    // 1. Ensure raw code is preserved, and restore pre elements to raw syntax
    diagrams.forEach((pre) => {
      if (!pre.dataset.rawCode) {
        pre.dataset.rawCode = pre.textContent;
      } else {
        pre.textContent = pre.dataset.rawCode;
        pre.removeAttribute("data-processed");
      }
    });

    const theme = document.documentElement.dataset.theme;
    const isDark =
      theme === "dark" ||
      (theme !== "light" &&
        window.matchMedia &&
        window.matchMedia("(prefers-color-scheme: dark)").matches);

    const lightVars = {
      darkMode: false,
      background: "#ffffff",
      mainBkg: "#ffffff",
      textColor: "#1a1e24",
      primaryColor: "#f4f8f7",
      primaryTextColor: "#1a1e24",
      primaryBorderColor: "#0e6b5c",
      lineColor: "#1e5aa8",
      secondaryColor: "#eef4f2",
      secondaryTextColor: "#1a1e24",
      secondaryBorderColor: "#0e6b5c",
      tertiaryColor: "#f8faf9",
      tertiaryTextColor: "#1a1e24",
      tertiaryBorderColor: "#dedad0",
      clusterBkg: "#f9fbfb",
      clusterBorder: "#dedad0",
      edgeLabelBackground: "#ffffff",
      actorBkg: "#ffffff",
      actorBorder: "#0e6b5c",
      actorTextColor: "#1a1e24",
      actorLineColor: "#0e6b5c",
      signalColor: "#1e5aa8",
      signalTextColor: "#1a1e24",
      labelBoxBkgColor: "#ffffff",
      labelBoxBorderColor: "#0e6b5c",
      labelTextColor: "#1a1e24",
      loopTextColor: "#1a1e24",
      noteBkgColor: "#fff9e6",
      noteBorderColor: "#dfb235",
      noteTextColor: "#1a1e24",
      activationBorderColor: "#0e6b5c",
      activationBkgColor: "#e5f2ee",
      sequenceNumberColor: "#ffffff",
    };

    const darkVars = {
      darkMode: true,
      background: "#1b1f24",
      mainBkg: "#1e242c",
      textColor: "#e8e6e1",
      primaryColor: "#222a35",
      primaryTextColor: "#e8e6e1",
      primaryBorderColor: "#4ec2a6",
      lineColor: "#6ba0eb",
      secondaryColor: "#1e242c",
      secondaryTextColor: "#e8e6e1",
      secondaryBorderColor: "#4ec2a6",
      tertiaryColor: "#161b22",
      tertiaryTextColor: "#e8e6e1",
      tertiaryBorderColor: "#30363d",
      clusterBkg: "rgba(33, 38, 45, 0.6)",
      clusterBorder: "#30363d",
      edgeLabelBackground: "#1e242c",
      actorBkg: "#222a35",
      actorBorder: "#4ec2a6",
      actorTextColor: "#e8e6e1",
      actorLineColor: "#4ec2a6",
      signalColor: "#6ba0eb",
      signalTextColor: "#e8e6e1",
      labelBoxBkgColor: "#1e242c",
      labelBoxBorderColor: "#4ec2a6",
      labelTextColor: "#e8e6e1",
      loopTextColor: "#e8e6e1",
      noteBkgColor: "#2a2517",
      noteBorderColor: "#9c8030",
      noteTextColor: "#f0f6fc",
      activationBorderColor: "#4ec2a6",
      activationBkgColor: "#1c2e28",
      sequenceNumberColor: "#ffffff",
    };

    mermaid.initialize({
      startOnLoad: false,
      securityLevel: "loose",
      theme: isDark ? "dark" : "neutral",
      fontFamily: "Source Sans 3, system-ui, sans-serif",
      fontSize: 14,
      themeVariables: isDark ? darkVars : lightVars,
    });

    mermaid
      .run({
        nodes: diagrams,
      })
      .then(() => {
        // Setup diagram click and badges (Mermaid + inline SVG figures)
        shell.querySelectorAll("pre.mermaid, figure.l1-arch-fig").forEach((pre, idx) => {
          if (!pre.querySelector(".diagram-expand-badge")) {
            const badge = document.createElement("button");
            badge.className = "diagram-expand-badge";
            badge.type = "button";
            badge.title = "Открыть на весь экран (Esc для выхода)";
            badge.setAttribute("aria-label", "Открыть на весь экран");
            badge.innerHTML = `<svg class="diagram-expand-icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 3 21 3 21 9"></polyline><polyline points="9 21 3 21 3 15"></polyline><line x1="21" y1="3" x2="14" y2="10"></line><line x1="3" y1="21" x2="10" y2="14"></line></svg>`;
            pre.appendChild(badge);
          }
          pre.title = "Нажмите, чтобы открыть диаграмму на весь экран (Esc для выхода)";
          pre.onclick = () => {
            window.openSlideshow(idx);
          };
        });
        registerSlideshowItems();

        const pendingId = window.pendingScrollId;
        const target = pendingId && document.getElementById(pendingId);
        if (target && shell.contains(target)) {
          window.pendingScrollId = null;
          target.scrollIntoView();
        }

        // If slideshow modal is currently open, refresh the active slide to update its colors immediately
        const modal = document.getElementById("slideshow-modal");
        if (modal && modal.classList.contains("open")) {
          renderCurrentSlide();
        }
      })
      .catch((err) => {
        console.error("Mermaid render error:", err);
      })
      .finally(() => {
        isMermaidRendering = false;
        if (pendingMermaidRender) {
          pendingMermaidRender = false;
          renderMermaidDiagrams();
        }
      });
  }

  window.renderMermaidDiagrams = renderMermaidDiagrams;

  // Active navigation highlight on scroll
  function initNav() {
    const navState = new Map();
    function navFor(shell) {
      if (!navState.has(shell)) {
        const navLinks = shell.querySelectorAll(".side a");
        const sections = Array.from(navLinks)
          .map((link) => {
            const href = link.getAttribute("href");
            return href && href.startsWith("#") ? document.querySelector(href) : null;
          })
          .filter(Boolean);
        navState.set(shell, { navLinks, sections });
      }
      return navState.get(shell);
    }

    function highlight() {
      const shell = window.activeShell();
      if (!shell) return;
      const state = navFor(shell);
      const { navLinks, sections } = state;
      {
        let currentId = "";
        const scrollPos = window.scrollY + 120;
        for (const section of sections) {
          if (section.offsetTop <= scrollPos) {
            currentId = "#" + section.id;
          }
        }
        navLinks.forEach((link) => {
          link.classList.toggle("on", link.getAttribute("href") === currentId);
        });
        const groups = shell.querySelectorAll(".side .ng");
        groups.forEach((group) => {
          group.classList.toggle("has-on", !!group.querySelector("a.on"));
        });

        // Auto-fold: when the active group changes, open it and fold the rest
        const activeLink = shell.querySelector(".side a.on");
        const activeGroup = activeLink ? activeLink.closest(".ng") : null;
        if (activeGroup && activeGroup !== state.activeGroup) {
          groups.forEach((group) => {
            if (group.setFolded) group.setFolded(group !== activeGroup);
          });
        }
        state.activeGroup = activeGroup;
      }
    }

    window.addEventListener("scroll", highlight, { passive: true });
    highlight();
  }

  // Nav group folding: folded by default, open groups kept in localStorage
  function initNavFolding() {
    const KEY = "telebrief-nav-open";
    let saved = {};
    try {
      saved = JSON.parse(localStorage.getItem(KEY) || "{}") || {};
    } catch (e) {}

    document.querySelectorAll(".shell").forEach((shell) => {
      const project = shell.dataset.project || shell.id;
      shell.querySelectorAll(".side .ng").forEach((group, i) => {
        const head = group.querySelector(".nh");
        const list = group.querySelector("ul");
        if (!head || !list) return;
        const key = project + ":" + i;
        if (!list.id) list.id = "ng-" + project + "-" + i;

        const chev = document.createElement("span");
        chev.className = "nh-chev";
        chev.setAttribute("aria-hidden", "true");
        chev.textContent = "▾";
        head.appendChild(chev);
        head.setAttribute("role", "button");
        head.setAttribute("tabindex", "0");
        head.setAttribute("aria-controls", list.id);

        function apply(folded) {
          group.classList.toggle("folded", folded);
          head.setAttribute("aria-expanded", folded ? "false" : "true");
        }
        apply(!saved[key]);

        function setFolded(folded) {
          apply(folded);
          if (folded) delete saved[key];
          else saved[key] = true;
          try {
            localStorage.setItem(KEY, JSON.stringify(saved));
          } catch (e) {}
        }
        group.setFolded = setFolded;

        function toggle() {
          setFolded(!group.classList.contains("folded"));
        }
        head.addEventListener("click", toggle);
        head.addEventListener("keydown", (e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            toggle();
          }
        });
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
      initUI();
      initNavFolding();
      renderMermaidDiagrams();
      initNav();
    });
  } else {
    initUI();
    initNavFolding();
    renderMermaidDiagrams();
    initNav();
  }
})();

    <!-- ======================================================================= -->
    <!-- COMMENTS: комментарии к тексту и блокам (comments.json через serve.js, иначе localStorage) -->
    <!-- ======================================================================= -->
(function () {
  "use strict";

  var STORE_KEY = "telebrief-comments-v1";
  var AUTHOR_KEY = "telebrief-comments-author";
  var BLOCKS = "p, li, blockquote, .tw, pre, figure, h1, h2, h3, h4, dl, .fs-card";
  var SKIP_TEXT = "script, style, svg, pre.mermaid, .cmt-ui";

  var main = document.getElementById("projects");
  if (!main) return;

  var comments = [];
  var blockMap = new Map();
  var hoverBlock = null;
  var pendingSel = null;
  var filterMode = "open";
  var DOC_SLUG = "telebrief";
  var DOC_TITLE = "Telebrief";

  /* ---------- storage: comments.json через serve.js, иначе localStorage ---------- */
  var API_URL = "api/comments";
  var storage = "local"; // "file", когда отвечает serve.js
  var saving = Promise.resolve();

  function loadLocal() {
    try {
      var raw = localStorage.getItem(STORE_KEY);
      var arr = raw ? JSON.parse(raw) : [];
      return Array.isArray(arr) ? arr : [];
    } catch (e) {
      return [];
    }
  }
  function loadFile() {
    return fetch(API_URL, { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        var arr = Array.isArray(data) ? data : data && data.comments;
        return Array.isArray(arr) ? arr : [];
      });
  }
  function saveComments() {
    if (storage === "file") {
      var body = serialize({ updatedAt: new Date().toISOString(), comments: comments });
      // Последовательные PUT: позднее состояние не перезапишется ранним.
      saving = saving
        .then(function () {
          return fetch(API_URL, { method: "PUT", headers: { "Content-Type": "application/json" }, body: body });
        })
        .then(function (r) {
          if (!r.ok) throw new Error("HTTP " + r.status);
          return true;
        })
        .catch(function () {
          toast("Не удалось записать comments.json. Проверьте, что serve.js запущен.");
          return false;
        });
      return saving;
    }
    try {
      localStorage.setItem(STORE_KEY, serialize(comments));
      return Promise.resolve(true);
    } catch (e) {
      toast("Не удалось сохранить в браузере. Сделайте экспорт JSON.");
      return Promise.resolve(false);
    }
  }
  /* Слияние по id: побеждает более новый updatedAt. Возвращает число новых. */
  function mergeComments(incoming) {
    var byId = {};
    comments.forEach(function (c) {
      byId[c.id] = c;
    });
    var added = 0;
    incoming.forEach(function (c) {
      if (!c || !c.id || !c.sectionId || (c.kind !== "text" && c.kind !== "block")) return;
      var cur = byId[c.id];
      if (!cur) added++;
      if (!cur || (c.updatedAt || 0) > (cur.updatedAt || 0)) byId[c.id] = c;
    });
    comments = Object.keys(byId).map(function (k) {
      return byId[k];
    });
    return added;
  }
  function initStorage() {
    if (location.protocol === "file:") {
      comments = loadLocal();
      applyAll();
      return;
    }
    loadFile().then(
      function (list) {
        storage = "file";
        comments = list;
        // Разовый перенос комментариев, сохранённых ранее в браузере.
        var local = loadLocal();
        var moved = local.length ? mergeComments(local) : 0;
        applyAll();
        if (!local.length) return;
        saveComments().then(function (ok) {
          if (!ok) return;
          try {
            localStorage.removeItem(STORE_KEY);
          } catch (e) {}
          toast("Перенесено из браузера в comments.json: " + moved);
        });
      },
      function () {
        comments = loadLocal();
        applyAll();
        toast("serve.js недоступен. Комментарии сохраняются в браузере.");
      }
    );
  }
  /* Другая вкладка могла изменить файл: перечитать при возврате. */
  document.addEventListener("visibilitychange", function () {
    if (storage !== "file" || document.visibilityState !== "visible") return;
    saving.then(loadFile).then(
      function (list) {
        if (pop.classList.contains("show")) return; // не терять черновик
        if (serialize(list) === serialize(comments)) return;
        comments = list;
        applyAll();
      },
      function () {}
    );
  });
  function serialize(list) {
    return JSON.stringify(list, function (k, v) {
      return k.charAt(0) === "_" ? undefined : v;
    });
  }
  function getAuthor() {
    try {
      return localStorage.getItem(AUTHOR_KEY) || "";
    } catch (e) {
      return "";
    }
  }
  function setAuthor(name) {
    try {
      localStorage.setItem(AUTHOR_KEY, name);
    } catch (e) {}
  }
  function uid() {
    return Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
  }

  /* ---------- helpers ---------- */
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function btn(label, cls, onClick, title) {
    var b = el("button", "cmt-btn" + (cls ? " " + cls : ""), label);
    b.type = "button";
    if (title) b.title = title;
    b.addEventListener("click", function (e) {
      e.stopPropagation();
      onClick(e);
    });
    return b;
  }
  function fmtDate(ts) {
    try {
      return new Date(ts).toLocaleString("uk-UA", {
        day: "2-digit",
        month: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch (e) {
      return "";
    }
  }
  function sectionLabel(id) {
    var sec = document.getElementById(id);
    var h = sec && sec.querySelector("h1, h2");
    return h ? h.textContent.replace(/\s+/g, " ").trim() : id;
  }
  function clip(s, n) {
    s = (s || "").replace(/\s+/g, " ").trim();
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }
  var toastEl = el("div", "cmt-ui cmt-toast");
  var toastTimer = null;
  function toast(msg) {
    toastEl.textContent = msg;
    toastEl.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      toastEl.classList.remove("show");
    }, 2200);
  }

  /* ---------- text model ---------- */
  function textNodes(root) {
    var out = [];
    var w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function (n) {
        var p = n.parentElement;
        return p && !p.closest(SKIP_TEXT) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
      },
    });
    var n;
    while ((n = w.nextNode())) out.push(n);
    return out;
  }

  function selectionAnchor() {
    var sel = window.getSelection();
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return null;
    var r = sel.getRangeAt(0);
    var anc = r.commonAncestorContainer;
    var host = anc.nodeType === 1 ? anc : anc.parentElement;
    if (!host || !main.contains(host) || host.closest(SKIP_TEXT)) return null;
    var sec = host.closest("section[id]");
    if (!sec) return null;

    var nodes = textNodes(sec);
    var full = "";
    var start = -1;
    var end = -1;
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      var len = n.nodeValue.length;
      if (r.intersectsNode(n)) {
        var s = n === r.startContainer ? r.startOffset : 0;
        var e = n === r.endContainer ? r.endOffset : len;
        if (start < 0) start = full.length + s;
        end = full.length + e;
      }
      full += n.nodeValue;
    }
    if (start < 0 || end <= start) return null;
    var quote = full.slice(start, end);
    if (!quote.trim()) return null;
    return {
      kind: "text",
      sectionId: sec.id,
      quote: quote,
      prefix: full.slice(Math.max(0, start - 40), start),
      suffix: full.slice(end, end + 40),
      rect: r.getBoundingClientRect(),
    };
  }

  function sharedTail(a, b) {
    var n = 0;
    while (n < a.length && n < b.length && a[a.length - 1 - n] === b[b.length - 1 - n]) n++;
    return n;
  }
  function sharedHead(a, b) {
    var n = 0;
    while (n < a.length && n < b.length && a[n] === b[n]) n++;
    return n;
  }

  function locateText(c, sec) {
    var nodes = textNodes(sec);
    var full = nodes
      .map(function (n) {
        return n.nodeValue;
      })
      .join("");
    var best = -1;
    var bestScore = -1;
    var idx = full.indexOf(c.quote);
    while (idx >= 0) {
      var before = full.slice(Math.max(0, idx - (c.prefix || "").length), idx);
      var after = full.slice(idx + c.quote.length, idx + c.quote.length + (c.suffix || "").length);
      var score = sharedTail(before, c.prefix || "") + sharedHead(after, c.suffix || "");
      if (score > bestScore) {
        best = idx;
        bestScore = score;
      }
      idx = full.indexOf(c.quote, idx + 1);
    }
    return best < 0 ? null : { nodes: nodes, start: best, end: best + c.quote.length };
  }

  function wrapText(loc, c) {
    var acc = 0;
    var targets = [];
    loc.nodes.forEach(function (n) {
      var len = n.nodeValue.length;
      var s = Math.max(loc.start, acc);
      var e = Math.min(loc.end, acc + len);
      if (e > s) targets.push([n, s - acc, e - acc]);
      acc += len;
    });
    targets.forEach(function (t) {
      var n = t[0];
      var s = t[1];
      var e = t[2];
      if (!n.nodeValue.slice(s, e).trim()) return;
      var p = n.parentElement;
      if (p && /^(TABLE|TBODY|THEAD|TFOOT|TR|UL|OL|DL)$/.test(p.tagName)) return;
      if (e < n.nodeValue.length) n.splitText(e);
      var mid = s > 0 ? n.splitText(s) : n;
      var m = document.createElement("mark");
      m.className = "cmt-mark" + (c.resolved ? " is-resolved" : "");
      m.setAttribute("data-cid", c.id);
      m.title = "Комментарий: " + clip(c.text, 80);
      mid.parentNode.insertBefore(m, mid);
      m.appendChild(mid);
    });
  }

  /* ---------- block model ---------- */
  function blockList(sec) {
    return Array.prototype.slice.call(sec.querySelectorAll(BLOCKS));
  }
  function blockSnippet(b, list) {
    if (b.matches("pre.mermaid")) {
      var diagrams = list.filter(function (x) {
        return x.matches("pre.mermaid");
      });
      return "diagram:" + diagrams.indexOf(b);
    }
    return clip(b.textContent, 80);
  }
  function blockAnchor(b) {
    var sec = b.closest("section[id]");
    if (!sec) return null;
    var list = blockList(sec);
    return {
      kind: "block",
      sectionId: sec.id,
      blockIndex: list.indexOf(b),
      snippet: blockSnippet(b, list),
      tag: b.tagName.toLowerCase(),
    };
  }
  function locateBlock(c, sec) {
    var list = blockList(sec);
    var b = list[c.blockIndex];
    if (b && blockSnippet(b, list) === c.snippet) return b;
    for (var i = 0; i < list.length; i++) {
      if (blockSnippet(list[i], list) === c.snippet) return list[i];
    }
    return null;
  }
  function blockLabel(c) {
    if ((c.snippet || "").indexOf("diagram:") === 0) return "Диаграмма";
    var names = { p: "Абзац", li: "Пункт списка", blockquote: "Цитата", pre: "Код", dl: "Список" };
    if (/^h\d$/.test(c.tag)) return "Заголовок";
    return names[c.tag] || (c.tag === "div" ? "Таблица / блок" : "Блок");
  }

  /* ---------- apply anchors ---------- */
  function clearAnchors() {
    main.querySelectorAll("mark.cmt-mark").forEach(function (m) {
      var p = m.parentNode;
      while (m.firstChild) p.insertBefore(m.firstChild, m);
      p.removeChild(m);
      p.normalize();
    });
    main.querySelectorAll(".cmt-block-on").forEach(function (b) {
      b.classList.remove("cmt-block-on", "is-resolved");
    });
    blockMap = new Map();
  }

  function applyAll() {
    clearAnchors();
    comments.forEach(function (c) {
      c._orphan = false;
      var sec = document.getElementById(c.sectionId);
      if (!sec) {
        c._orphan = true;
        return;
      }
      if (c.kind === "text") {
        var loc = locateText(c, sec);
        if (loc) wrapText(loc, c);
        else c._orphan = true;
      } else {
        var b = locateBlock(c, sec);
        if (!b) {
          c._orphan = true;
          return;
        }
        if (!blockMap.has(b)) blockMap.set(b, []);
        blockMap.get(b).push(c);
      }
    });
    blockMap.forEach(function (list, b) {
      var open = list.some(function (c) {
        return !c.resolved;
      });
      b.classList.add("cmt-block-on");
      b.classList.toggle("is-resolved", !open);
    });
    renderPanel();
    updateCount();
  }

  /* ---------- UI elements ---------- */
  var hoverBtn = el("button", "cmt-ui cmt-float-btn");
  hoverBtn.type = "button";
  hoverBtn.title = "Комментировать блок";

  var selBtn = el("button", "cmt-ui cmt-float-btn cmt-sel-btn", "💬 Комментировать");
  selBtn.type = "button";

  var pop = el("div", "cmt-ui cmt-popover");
  pop.setAttribute("role", "dialog");
  pop.setAttribute("aria-label", "Комментарии");

  var panel = el("aside", "cmt-ui cmt-panel");
  panel.setAttribute("aria-label", "Панель комментариев");
  var fileInput = el("input");
  fileInput.type = "file";
  fileInput.accept = "application/json,.json";
  fileInput.hidden = true;

  document.body.appendChild(hoverBtn);
  document.body.appendChild(selBtn);
  document.body.appendChild(pop);
  document.body.appendChild(panel);
  document.body.appendChild(toastEl);
  document.body.appendChild(fileInput);

  /* top bar toggle */
  var toggle = el("button", "sidebar-toggle-btn cmt-toggle");
  toggle.type = "button";
  toggle.title = "Комментарии: выделите текст или наведите на блок";
  toggle.innerHTML =
    '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path></svg>' +
    '<span class="cmt-label">Комментарии</span><span class="cmt-count">0</span>';
  toggle.addEventListener("click", function () {
    panel.classList.toggle("open");
  });
  var topRight = document.querySelector(".top-right");
  if (topRight) topRight.insertBefore(toggle, topRight.firstChild);
  else panel.classList.add("open");

  function updateCount() {
    var open = comments.filter(function (c) {
      return !c.resolved;
    }).length;
    toggle.querySelector(".cmt-count").textContent = String(open);
    toggle.classList.toggle("has-open", open > 0);
  }

  /* ---------- hover block button ---------- */
  function placeHoverBtn(b) {
    var r = b.getBoundingClientRect();
    var list = blockMap.get(b) || [];
    hoverBtn.textContent = list.length ? "💬 " + list.length : "💬";
    hoverBtn.classList.toggle("has-comments", list.length > 0);
    hoverBtn.style.top = r.top + window.scrollY + "px";
    hoverBtn.style.left = Math.max(4, r.left + window.scrollX - 44) + "px";
    hoverBtn.classList.add("show");
  }
  main.addEventListener("mousemove", function (e) {
    var b = e.target.closest ? e.target.closest(BLOCKS) : null;
    if (!b || !main.contains(b) || !b.closest("section[id]")) return;
    if (b !== hoverBlock) {
      hoverBlock = b;
      placeHoverBtn(b);
    }
  });
  main.addEventListener("mouseleave", function (e) {
    if (e.relatedTarget === hoverBtn) return;
    hoverBtn.classList.remove("show");
    hoverBlock = null;
  });
  hoverBtn.addEventListener("mouseleave", function (e) {
    if (e.relatedTarget && main.contains(e.relatedTarget)) return;
    hoverBtn.classList.remove("show");
    hoverBlock = null;
  });
  hoverBtn.addEventListener("click", function (e) {
    e.stopPropagation();
    if (!hoverBlock) return;
    var anchor = blockAnchor(hoverBlock);
    if (!anchor) return;
    openPopover(hoverBlock.getBoundingClientRect(), blockMap.get(hoverBlock) || [], anchor);
  });

  /* ---------- selection button ---------- */
  var selTimer = null;
  function refreshSelBtn() {
    var a = selectionAnchor();
    pendingSel = a;
    if (!a || pop.classList.contains("show")) {
      selBtn.classList.remove("show");
      return;
    }
    var top = a.rect.top - 36;
    if (top < 60) top = a.rect.bottom + 8;
    var left = Math.min(Math.max(8, a.rect.left + a.rect.width / 2 - 60), window.innerWidth - 140);
    selBtn.style.top = top + "px";
    selBtn.style.left = left + "px";
    selBtn.classList.add("show");
  }
  document.addEventListener("selectionchange", function () {
    clearTimeout(selTimer);
    selTimer = setTimeout(refreshSelBtn, 180);
  });
  selBtn.addEventListener("mousedown", function (e) {
    e.preventDefault();
  });
  selBtn.addEventListener("click", function (e) {
    e.stopPropagation();
    if (!pendingSel) return;
    var a = pendingSel;
    selBtn.classList.remove("show");
    openPopover(a.rect, [], a);
  });
  window.addEventListener(
    "scroll",
    function () {
      selBtn.classList.remove("show");
    },
    { passive: true }
  );

  /* click on highlighted text */
  main.addEventListener(
    "click",
    function (e) {
      var m = e.target.closest ? e.target.closest("mark.cmt-mark") : null;
      if (!m) return;
      var sel = window.getSelection();
      if (sel && !sel.isCollapsed) return;
      e.preventDefault();
      e.stopPropagation();
      var ids = [];
      var cur = m;
      while (cur && cur.matches && cur.matches("mark.cmt-mark")) {
        ids.push(cur.getAttribute("data-cid"));
        cur = cur.parentElement;
      }
      var list = comments.filter(function (c) {
        return ids.indexOf(c.id) >= 0;
      });
      openPopover(m.getBoundingClientRect(), list, null);
    },
    true
  );

  /* ---------- popover ---------- */
  var popState = null;

  function openPopover(rect, threads, newAnchor) {
    popState = { rect: rect, threadIds: threads.map((c) => c.id), anchor: newAnchor };
    renderPopover();
    pop.classList.add("show");
    positionPopover(rect);
    var ta = pop.querySelector(".cmt-composer textarea");
    if (ta) ta.focus();
  }
  function closePopover() {
    pop.classList.remove("show");
    popState = null;
  }
  function positionPopover(rect) {
    var w = pop.offsetWidth;
    var h = pop.offsetHeight;
    var top = rect.bottom + 8;
    if (top + h > window.innerHeight - 8) top = Math.max(60, rect.top - h - 8);
    if (top + h > window.innerHeight - 8) top = Math.max(60, window.innerHeight - h - 8);
    var left = Math.min(Math.max(16, rect.left), window.innerWidth - w - 16);
    pop.style.top = top + "px";
    pop.style.left = left + "px";
  }

  function renderPopover() {
    pop.textContent = "";
    if (!popState) return;
    var threads = comments.filter(function (c) {
      return popState.threadIds.indexOf(c.id) >= 0;
    });
    threads.forEach(function (c) {
      pop.appendChild(renderThread(c));
    });
    if (popState.anchor) pop.appendChild(renderComposer(popState.anchor));
    if (!threads.length && !popState.anchor) closePopover();
  }

  function authorRow() {
    var name = el("input");
    name.type = "text";
    name.placeholder = "Ваше имя";
    name.value = getAuthor();
    name.maxLength = 60;
    return name;
  }

  function submitOnMetaEnter(ta, fn) {
    ta.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        fn();
      }
    });
  }

  function renderComposer(anchor) {
    var box = el("div", "cmt-composer");
    var title = anchor.kind === "text" ? "Новый комментарий к фрагменту" : "Новый комментарий к блоку";
    box.appendChild(el("div", "cmt-meta", title));
    if (anchor.kind === "text") box.appendChild(el("div", "cmt-quote", clip(anchor.quote, 300)));
    var name = authorRow();
    var ta = el("textarea");
    ta.placeholder = "Комментарий… (⌘/Ctrl + Enter — сохранить)";
    var row1 = el("div", "cmt-row");
    row1.appendChild(name);
    var row2 = el("div", "cmt-row");
    row2.appendChild(ta);
    var row3 = el("div", "cmt-row");
    row3.appendChild(el("span", "cmt-spacer"));
    function submit() {
      var text = ta.value.trim();
      if (!text) {
        ta.focus();
        return;
      }
      var author = name.value.trim() || "Аноним";
      setAuthor(author);
      var now = Date.now();
      var c = {
        id: uid(),
        kind: anchor.kind,
        sectionId: anchor.sectionId,
        author: author,
        text: text,
        createdAt: now,
        updatedAt: now,
        resolved: false,
        replies: [],
      };
      if (anchor.kind === "text") {
        c.quote = anchor.quote;
        c.prefix = anchor.prefix;
        c.suffix = anchor.suffix;
      } else {
        c.blockIndex = anchor.blockIndex;
        c.snippet = anchor.snippet;
        c.tag = anchor.tag;
      }
      comments.push(c);
      saveComments();
      var sel = window.getSelection();
      if (sel) sel.removeAllRanges();
      applyAll();
      closePopover();
      toast("Комментарий сохранён");
    }
    submitOnMetaEnter(ta, submit);
    row3.appendChild(btn("Отмена", "", closePopover));
    row3.appendChild(btn("Сохранить", "primary", submit));
    box.appendChild(row1);
    box.appendChild(row2);
    box.appendChild(row3);
    return box;
  }

  function renderThread(c) {
    var box = el("div", "cmt-thread");
    var meta = el("div", "cmt-meta");
    meta.appendChild(el("strong", "", c.author || "Аноним"));
    meta.appendChild(el("span", "", fmtDate(c.createdAt)));
    if (c.resolved) meta.appendChild(el("span", "cmt-tag", "решено"));
    var actions = el("span", "cmt-actions");
    actions.appendChild(
      btn(c.resolved ? "↺" : "✓", "icon", function () {
        c.resolved = !c.resolved;
        c.updatedAt = Date.now();
        saveComments();
        applyAll();
        renderPopover();
      }, c.resolved ? "Открыть снова" : "Отметить как решённое")
    );
    actions.appendChild(
      btn("✎", "icon", function () {
        editBody(box, c);
      }, "Редактировать")
    );
    actions.appendChild(
      btn("🗑", "icon", function () {
        if (!confirm("Удалить комментарий вместе с ответами?")) return;
        comments = comments.filter(function (x) {
          return x.id !== c.id;
        });
        saveComments();
        applyAll();
        if (popState) {
          popState.threadIds = popState.threadIds.filter(function (id) {
            return id !== c.id;
          });
        }
        renderPopover();
      }, "Удалить")
    );
    meta.appendChild(actions);
    box.appendChild(meta);
    if (c.kind === "text") box.appendChild(el("div", "cmt-quote", clip(c.quote, 300)));
    box.appendChild(el("p", "cmt-body", c.text));

    var replies = el("div", "cmt-replies");
    (c.replies || []).forEach(function (r) {
      var rb = el("div", "cmt-reply");
      var rm = el("div", "cmt-meta");
      rm.appendChild(el("strong", "", r.author || "Аноним"));
      rm.appendChild(el("span", "", fmtDate(r.createdAt)));
      var ra = el("span", "cmt-actions");
      ra.appendChild(
        btn("🗑", "icon", function () {
          c.replies = c.replies.filter(function (x) {
            return x.id !== r.id;
          });
          c.updatedAt = Date.now();
          saveComments();
          renderPanel();
          renderPopover();
        }, "Удалить ответ")
      );
      rm.appendChild(ra);
      rb.appendChild(rm);
      rb.appendChild(el("p", "cmt-body", r.text));
      replies.appendChild(rb);
    });
    var rta = el("textarea");
    rta.placeholder = "Ответить…";
    rta.style.minHeight = "40px";
    var rrow = el("div", "cmt-row");
    rrow.appendChild(rta);
    var rbtns = el("div", "cmt-row");
    rbtns.appendChild(el("span", "cmt-spacer"));
    function reply() {
      var text = rta.value.trim();
      if (!text) return;
      var author = getAuthor() || prompt("Ваше имя", "") || "Аноним";
      setAuthor(author);
      c.replies = c.replies || [];
      c.replies.push({ id: uid(), author: author, text: text, createdAt: Date.now() });
      c.updatedAt = Date.now();
      saveComments();
      renderPanel();
      renderPopover();
    }
    submitOnMetaEnter(rta, reply);
    rbtns.appendChild(btn("Ответить", "", reply));
    replies.appendChild(rrow);
    replies.appendChild(rbtns);
    box.appendChild(replies);
    return box;
  }

  function editBody(box, c) {
    var body = box.querySelector(".cmt-body");
    var ta = el("textarea");
    ta.value = c.text;
    var row = el("div", "cmt-row");
    row.appendChild(el("span", "cmt-spacer"));
    function done() {
      var v = ta.value.trim();
      if (!v) return;
      c.text = v;
      c.updatedAt = Date.now();
      saveComments();
      applyAll();
      renderPopover();
    }
    submitOnMetaEnter(ta, done);
    row.appendChild(btn("Отмена", "", renderPopover));
    row.appendChild(btn("Сохранить", "primary", done));
    body.replaceWith(ta);
    ta.after(row);
    ta.focus();
  }

  /* ---------- panel ---------- */
  function renderPanel() {
    panel.textContent = "";
    var head = el("div", "cmt-panel-head");
    var h = el("h4", "", "Комментарии");
    h.appendChild(
      btn("✕", "icon", function () {
        panel.classList.remove("open");
      }, "Закрыть")
    );
    head.appendChild(h);
    head.appendChild(
      el(
        "p",
        "cmt-hint",
        "Выделите текст или наведите курсор на блок и нажмите 💬. " +
          (storage === "file"
            ? "Комментарии сохраняются в файле comments.json рядом с документацией."
            : "Комментарии сохраняются в этом браузере. Для записи в comments.json запустите node serve.js.")
      )
    );
    var tools = el("div", "cmt-tools");
    var sel = el("select");
    [
      ["open", "Открытые"],
      ["all", "Все"],
      ["resolved", "Решённые"],
    ].forEach(function (o) {
      var opt = el("option", "", o[1]);
      opt.value = o[0];
      if (o[0] === filterMode) opt.selected = true;
      sel.appendChild(opt);
    });
    sel.addEventListener("change", function () {
      filterMode = sel.value;
      renderPanel();
    });
    tools.appendChild(sel);
    tools.appendChild(btn("⬇ JSON", "", exportJSON, "Экспортировать комментарии в файл JSON"));
    tools.appendChild(
      btn("⬆ Импорт", "", function () {
        fileInput.click();
      }, "Импортировать комментарии из файла JSON")
    );
    tools.appendChild(btn("⧉ Markdown", "", copyMarkdown, "Скопировать комментарии как Markdown"));
    tools.appendChild(btn("Очистить", "", clearAll, "Удалить все комментарии"));
    head.appendChild(tools);
    panel.appendChild(head);

    var list = el("div", "cmt-list");
    var items = orderedComments().filter(function (c) {
      if (filterMode === "open") return !c.resolved;
      if (filterMode === "resolved") return c.resolved;
      return true;
    });
    if (!items.length) {
      list.appendChild(
        el("div", "cmt-empty", comments.length ? "Нет комментариев в этом фильтре." : "Комментариев пока нет.")
      );
    }
    items.forEach(function (c) {
      var it = el("button", "cmt-item" + (c.resolved ? " is-resolved" : ""));
      it.type = "button";
      var secRow = el("div", "cmt-item-sec");
      secRow.appendChild(document.createTextNode(clip(sectionLabel(c.sectionId), 60)));
      it.appendChild(secRow);
      var meta = el("div", "cmt-meta");
      meta.appendChild(el("strong", "", c.author || "Аноним"));
      meta.appendChild(el("span", "", fmtDate(c.createdAt)));
      meta.appendChild(el("span", "cmt-tag", c.kind === "text" ? "Текст" : blockLabel(c)));
      if (c.replies && c.replies.length) meta.appendChild(el("span", "cmt-tag", "↩ " + c.replies.length));
      if (c.resolved) meta.appendChild(el("span", "cmt-tag", "решено"));
      if (c._orphan) meta.appendChild(el("span", "cmt-tag warn", "фрагмент не найден"));
      it.appendChild(meta);
      if (c.kind === "text") it.appendChild(el("div", "cmt-quote", clip(c.quote, 140)));
      it.appendChild(el("p", "cmt-body", clip(c.text, 220)));
      it.addEventListener("click", function () {
        goTo(c);
      });
      list.appendChild(it);
    });
    panel.appendChild(list);
  }

  function orderedComments() {
    var secIds = Array.prototype.map.call(main.querySelectorAll("section[id]"), function (s) {
      return s.id;
    });
    return comments.slice().sort(function (a, b) {
      var sa = secIds.indexOf(a.sectionId);
      var sb = secIds.indexOf(b.sectionId);
      if (sa !== sb) return (sa < 0 ? 1e9 : sa) - (sb < 0 ? 1e9 : sb);
      return a.createdAt - b.createdAt;
    });
  }

  function targetOf(c) {
    if (c.kind === "text") return main.querySelector('mark.cmt-mark[data-cid="' + c.id + '"]');
    var found = null;
    blockMap.forEach(function (list, b) {
      if (list.indexOf(c) >= 0) found = b;
    });
    return found;
  }

  function goTo(c) {
    var t = targetOf(c);
    if (!t) {
      var sec = document.getElementById(c.sectionId);
      if (sec) sec.scrollIntoView({ behavior: "smooth", block: "start" });
      toast("Фрагмент изменился. Показываю раздел.");
      return;
    }
    t.scrollIntoView({ behavior: "smooth", block: "center" });
    t.classList.remove("cmt-flash");
    void t.offsetWidth;
    t.classList.add("cmt-flash");
    setTimeout(function () {
      t.classList.remove("cmt-flash");
      openPopover(t.getBoundingClientRect(), [c], null);
    }, 450);
  }

  /* ---------- export / import ---------- */
  function download(name, text, type) {
    var blob = new Blob([text], { type: type });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () {
      URL.revokeObjectURL(a.href);
      a.remove();
    }, 0);
  }
  function stamp() {
    return new Date().toISOString().slice(0, 10);
  }
  function exportJSON() {
    var payload = {
      doc: DOC_SLUG + "_architecture",
      exportedAt: new Date().toISOString(),
      comments: JSON.parse(serialize(comments)),
    };
    download(DOC_SLUG + "-comments-" + stamp() + ".json", JSON.stringify(payload, null, 2), "application/json");
  }
  fileInput.addEventListener("change", function () {
    var f = fileInput.files && fileInput.files[0];
    if (!f) return;
    var reader = new FileReader();
    reader.onload = function () {
      try {
        var data = JSON.parse(reader.result);
        var incoming = Array.isArray(data) ? data : data.comments;
        if (!Array.isArray(incoming)) throw new Error("format");
        var added = mergeComments(incoming);
        saveComments();
        applyAll();
        toast("Импортировано. Новых комментариев: " + added);
      } catch (e) {
        toast("Ошибка импорта: неверный JSON");
      }
      fileInput.value = "";
    };
    reader.readAsText(f);
  });
  function toMarkdown() {
    var out = ["# Комментарии к документации " + DOC_TITLE, "", "_Экспорт: " + new Date().toLocaleString("ru-RU") + "_", ""];
    var lastSec = null;
    orderedComments().forEach(function (c) {
      if (c.sectionId !== lastSec) {
        out.push("## " + sectionLabel(c.sectionId), "");
        lastSec = c.sectionId;
      }
      var what = c.kind === "text" ? "> " + clip(c.quote, 400) : "> [" + blockLabel(c) + "] " + clip(c.snippet, 120);
      out.push(what, "");
      out.push("- **" + (c.author || "Аноним") + "** (" + fmtDate(c.createdAt) + ")" + (c.resolved ? " ✅ решено" : "") + ": " + c.text.replace(/\n/g, "\n  "));
      (c.replies || []).forEach(function (r) {
        out.push("  - **" + (r.author || "Аноним") + "** (" + fmtDate(r.createdAt) + "): " + r.text.replace(/\n/g, "\n    "));
      });
      out.push("");
    });
    return out.join("\n");
  }
  function copyMarkdown() {
    if (!comments.length) {
      toast("Нет комментариев для копирования");
      return;
    }
    var md = toMarkdown();
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(md).then(
        function () {
          toast("Markdown скопирован");
        },
        function () {
          download(DOC_SLUG + "-comments-" + stamp() + ".md", md, "text/markdown");
        }
      );
    } else {
      download(DOC_SLUG + "-comments-" + stamp() + ".md", md, "text/markdown");
    }
  }
  function clearAll() {
    if (!comments.length) return;
    if (!confirm("Удалить комментарии «" + DOC_TITLE + "» (" + comments.length + ")? Рекомендуется сначала сделать экспорт JSON.")) return;
    comments = [];
    saveComments();
    applyAll();
    closePopover();
  }

  /* ---------- global keys & outside click ---------- */
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    if (pop.classList.contains("show")) closePopover();
    else if (panel.classList.contains("open")) panel.classList.remove("open");
  });
  document.addEventListener("mousedown", function (e) {
    if (!pop.classList.contains("show")) return;
    if (pop.contains(e.target) || e.target === hoverBtn || e.target === selBtn) return;
    if (e.target.closest && e.target.closest("mark.cmt-mark")) return;
    closePopover();
  });
  window.addEventListener("resize", function () {
    if (popState && pop.classList.contains("show")) positionPopover(popState.rect);
  });

  initStorage();
})();
