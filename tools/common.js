/* Shared helpers for every page (inlined by the server / kg build). */
(function () {
  const old = { dark: "ink", light: "paper" };   // values saved by earlier versions
  let saved = localStorage.getItem("kgTheme");
  if (old[saved]) { saved = old[saved]; localStorage.setItem("kgTheme", saved); }
  if (saved && saved !== "auto") document.documentElement.dataset.theme = saved;
})();

const THEMES = [["auto", "🌓 Auto"], ["ink", "🌑 Ink"], ["paper", "📜 Paper"], ["mocha", "🌸 Mocha"], ["rose", "🌹 Rosé Pine"], ["gruvbox", "🟠 Gruvbox"]];

const KG = {
  esc: s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
  md: s => (window.marked && window.DOMPurify) ? DOMPurify.sanitize(marked.parse(String(s ?? "")))
                                                : KG.esc(s).replace(/\n/g, "<br>"),
  reducedMotion: () => window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  served: () => location.protocol.startsWith("http"),

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
