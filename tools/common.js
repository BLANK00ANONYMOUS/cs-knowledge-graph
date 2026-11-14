/* Shared helpers for every page (inlined by the server / kg build). */
(function () {
  const old = { dark: "ink", light: "paper" };   // values saved by earlier versions
  let saved = localStorage.getItem("kgTheme");
  if (old[saved]) { saved = old[saved]; localStorage.setItem("kgTheme", saved); }
  if (saved && saved !== "auto") document.documentElement.dataset.theme = saved;
})();

/* App shell: each section lives in a persistent frame of the shell page ("/"), so switching sections keeps
   in-progress quizzes, lessons and generations. A section opened directly is moved into the shell. */
const KG_SECTIONS = { "/today": "today", "/graph": "graph", "/build": "build", "/arena": "arena", "/jobs": "jobs" };
const KG_EMBEDDED = (() => {
  try { return window.self !== window.top && window.parent.location.origin === location.origin; } catch (e) { return false; }
})();
(function () {
  if (!location.protocol.startsWith("http")) return;
  if (KG_EMBEDDED) document.documentElement.classList.add("embedded");
  else if (KG_SECTIONS[location.pathname])
    location.replace("/?open=" + encodeURIComponent(location.pathname + location.search + location.hash));
})();

const THEMES = [["auto", "🌓 Auto"], ["ink", "🌑 Ink"], ["paper", "📜 Paper"], ["mocha", "🌸 Mocha"], ["rose", "🌹 Rosé Pine"], ["gruvbox", "🟠 Gruvbox"]];

const KG = {
  esc: s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
  md: s => (window.marked && window.DOMPurify) ? DOMPurify.sanitize(marked.parse(String(s ?? "")))
                                                : KG.esc(s).replace(/\n/g, "<br>"),
  reducedMotion: () => window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  served: () => location.protocol.startsWith("http"),
  embedded: KG_EMBEDDED,

  /* Navigate to another section (keeps every section's state when running inside the shell). */
  go(href) {
    if (KG_EMBEDDED) window.parent.postMessage({ kg: "nav", href }, location.origin);
    else location.href = href;
  },
  /* Called with a URL when the shell routes a deep link (e.g. /graph?learn=x) to this already-open page. */
  onRoute(fn) { KG._route = fn; },
  /* Tell the shell this section is working on something long (shows a spinner on its tab). */
  busy(on, label = "") { if (KG_EMBEDDED) window.parent.postMessage({ kg: "busy", on: !!on, label }, location.origin); },

  async api(path, body, signal) {
    const r = await fetch(path, { method: body ? "POST" : "GET", signal,
      headers: { "Content-Type": "application/json", "X-KG": "1" }, body: body ? JSON.stringify(body) : undefined });
    const j = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
    if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
    return j;
  },

  /* theme: auto (follows OS) → ink → paper → mocha → rosé pine → gruvbox */
  themeLabel() {
    const t = localStorage.getItem("kgTheme") || "auto";
    return (THEMES.find(x => x[0] === t) || THEMES[0])[1];
  },
  cycleTheme() {
    const ids = THEMES.map(x => x[0]);
    const next = ids[(ids.indexOf(localStorage.getItem("kgTheme") || "auto") + 1) % ids.length];
    if (next === "auto") { localStorage.removeItem("kgTheme"); delete document.documentElement.dataset.theme; }
    else { localStorage.setItem("kgTheme", next); document.documentElement.dataset.theme = next; }
    document.dispatchEvent(new CustomEvent("kg-theme"));
    return KG.themeLabel();
  },
  themeButton(el) {
    el.textContent = KG.themeLabel();
    el.title = "Colour theme (auto follows your OS)";
    el.onclick = () => { el.textContent = KG.cycleTheme(); };
  },
  cssVar: name => getComputedStyle(document.documentElement).getPropertyValue(name).trim(),

  toast(html, { error = false, ms = 6000 } = {}) {
    const t = document.createElement("div");
    t.className = "toast" + (error ? " err" : "");
    t.setAttribute("role", error ? "alert" : "status");
    t.innerHTML = `<div>${html}</div><button class="ghost" aria-label="Dismiss">✕</button>`;
    t.querySelector("button").onclick = () => t.remove();
    document.body.appendChild(t);
    if (ms) setTimeout(() => t.remove(), ms);
    return t;
  },
};

/* Theme changes made in one frame (or the shell) apply everywhere. */
window.addEventListener("storage", e => {
  if (e.key !== "kgTheme") return;
  if (e.newValue && e.newValue !== "auto") document.documentElement.dataset.theme = e.newValue;
  else delete document.documentElement.dataset.theme;
  document.dispatchEvent(new CustomEvent("kg-theme"));
});

if (KG_EMBEDDED) {
  /* Links to other sections switch the shell's tab instead of reloading this frame. */
  document.addEventListener("click", e => {
    const a = e.target.closest && e.target.closest("a[href]");
    if (!a || e.defaultPrevented || a.target === "_blank" || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey) return;
    const u = new URL(a.getAttribute("href"), location.href);
    if (u.origin !== location.origin || !(KG_SECTIONS[u.pathname] || u.pathname === "/")) return;
    if (u.pathname === location.pathname && !u.search) return;   // in-page anchor
    e.preventDefault();
    KG.go(u.pathname + u.search + u.hash);
  });
  window.addEventListener("message", e => {
    if (e.origin !== location.origin || !e.data || typeof e.data !== "object") return;
    if (e.data.kg === "route") {
      const u = new URL(e.data.href, location.origin);
      if (KG._route && u.pathname === location.pathname) KG._route(u); else location.href = u.href;
    } else if (e.data.kg === "shown") document.dispatchEvent(new CustomEvent("kg-shown"));
  });
  /* Alt+1…5 switches sections from anywhere. */
  document.addEventListener("keydown", e => {
    if (e.altKey && !e.ctrlKey && !e.metaKey && /^[1-5]$/.test(e.key)) { e.preventDefault(); window.parent.postMessage({ kg: "tab", n: +e.key }, location.origin); }
  });
}