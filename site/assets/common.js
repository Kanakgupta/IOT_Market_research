/* Shared UI helpers for the Wireless Intelligence site */
(function () {
  const IDX = window.WR_INDEX || { companies: [], apps: [], categories: {} };
  const PT = "America/Los_Angeles";

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const qs = (k) => new URLSearchParams(location.search).get(k);
  const fmtPT = (iso, opts) => new Intl.DateTimeFormat("en-US", Object.assign({ timeZone: PT }, opts)).format(new Date(iso));
  const fmtDate = (iso) => fmtPT(iso, { month: "short", day: "numeric" });
  const fmtFull = (iso) => fmtPT(iso, { weekday: "short", month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" });
  const ago = (iso) => {
    const h = (Date.now() - new Date(iso).getTime()) / 36e5;
    if (h < 1) return "just now";
    if (h < 24) return Math.round(h) + "h ago";
    const d = Math.round(h / 24);
    return d + "d ago";
  };
  const coName = {};
  IDX.companies.forEach((c) => (coName[c.id] = c.n));
  const catName = IDX.categories || {};

  function icon(name) {
    const paths = {
      signal: '<path d="M3 8a14 14 0 0 1 18 0M6 12a9 9 0 0 1 12 0M9 16a4 4 0 0 1 6 0"/><circle cx="12" cy="20" r="1"/>',
      report: '<path d="M4 19V5h16v14H4ZM8 15v-3m4 3V9m4 6v-5"/>',
      company: '<path d="M4 21V3h12v18m0-12h4v12M8 7h4M8 11h4M8 15h4M2 21h20"/>',
      news: '<path d="M4 4h14v16H4V4Zm14 4h3v12h-3M7 8h8M7 12h8M7 16h5"/>',
      apps: '<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/>',
      search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
      arrow: '<path d="M4 12h16m-6-6 6 6-6 6"/>',
      shield: '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/>',
      chip: '<rect x="6" y="6" width="12" height="12" rx="3"/><path d="M9 2v4m6-4v4M9 18v4m6-4v4M2 9h4m-4 6h4m12-6h4m-4 6h4"/><rect x="10" y="10" width="4" height="4"/>'
    };
    return `<svg class="ui-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.signal}</svg>`;
  }

  const techCls = (t) => ({ "Wi-Fi": "t-wifi", Bluetooth: "t-bt", "Other wireless": "t-oth", Business: "t-biz" }[t] || "");
  const tierBadge = (t) => `<span class="badge ${t === "Leader" ? "leader" : t === "Challenger" ? "challenger" : "niche"}">${esc(t)}</span>`;
  const momHtml = (m, n7) => {
    if (m > 0.5) return `<span class="up" title="7-day coverage above 30-day trend">▲ ${n7 ?? ""}</span>`;
    if (m < -0.4) return `<span class="down" title="7-day coverage below 30-day trend">▼ ${n7 ?? ""}</span>`;
    return `<span class="flat" title="in line with trend">● ${n7 ?? ""}</span>`;
  };

  function themeChips(th, labels, max = 4) {
    const skip = new Set(["wifi", "bluetooth", "chips"]);
    return (th || []).filter((t) => !skip.has(t)).slice(0, max)
      .map((t) => `<span class="chip">${esc((labels || {})[t] || t)}</span>`).join("");
  }

  let uid = 0;
  function newsItem(n, labels, opts = {}) {
    const id = "al" + ++uid;
    const cos = (n.c || []).slice(0, 5).map((c) => coName[c] ? `<a class="chip co" href="company.html?id=${c}">${esc(coName[c])}</a>` : "").join("");
    const tech = (n.tech || []).filter((t) => t !== "General").map((t) => `<span class="chip ${techCls(t)}">${esc(t)}</span>`).join("");
    const rg = (n.rg || []).filter((r) => r !== "Global").map((r) => `<span class="chip rg">${esc(r)}</span>`).join("");
    const cov = n.cv > 1 && (n.al || []).length ? `<button type="button" class="cov" aria-expanded="false" aria-controls="${id}" onclick="const open=document.getElementById('${id}').classList.toggle('open');this.setAttribute('aria-expanded',open)">+${n.cv - 1} more outlet${n.cv > 2 ? "s" : ""} ▾</button>` : "";
    const also = (n.al || []).map((a) => `<a class="ext" href="${esc(a.u)}" target="_blank" rel="noopener">${esc(a.p)}</a>`).join("");
    return `<li>
      <a class="t ext" href="${esc(n.u)}" target="_blank" rel="noopener noreferrer">${esc(n.t)}</a>
      <div class="meta"><b>${esc(n.p)}</b><span title="${esc(fmtFull(n.d))}">${fmtDate(n.d)} · ${ago(n.d)}</span>
        ${n.w ? '<span class="badge wire">Press release</span>' : ""}${n.ed ? `<span class="badge">${esc(n.ed)} edition</span>` : ""}${cov}</div>
      ${opts.desc && n.x ? `<div class="x">${esc(n.x)}</div>` : ""}
      <div class="meta">${cos}${tech}${themeChips(n.th, labels)}${rg}</div>
      ${also ? `<div class="also" id="${id}">Also covered by: ${also}</div>` : ""}
    </li>`;
  }

  function header(active) {
    document.body.dataset.page = active;
    const cats = Object.entries(catName).map(([k, v]) => `<a href="index.html?cat=${k}#companies"><strong>${esc(v)}</strong><span>${IDX.companies.filter((c) => c.cat === k).length} companies tracked</span></a>`).join("");
    const gen = (window.WR_REPORT || window.WR_NEWS || window.WR_APPS || window.WR_RUNLOG || window.WR_COMPANY || {}).generated;
    const el = document.getElementById("hdr");
    el.innerHTML = `<a class="skip-link" href="#main-content">Skip to content</a><header class="topnav"><div class="wrap">
      <div class="row1">
        <a class="brand" href="index.html"><span class="logo">${icon("signal")}</span><span>Wireless<span class="brand-accent"> Intelligence</span><small>THE CONNECTED WORLD, DECODED.</small></span></a>
        <span class="spacer"></span>
        ${gen ? `<span class="status-pill" title="Last research run"><span class="status-dot"></span>Updated <b>${esc(fmtPT(gen, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" }))}</b></span>` : ""}
        <form class="search" onsubmit="return WR.goSearch(event)" autocomplete="off">
          ${icon("search")}<input id="global-q" aria-label="Search companies and applications" type="text" placeholder="Search companies, applications…" oninput="WR.suggest()" onkeydown="WR.navSuggest(event)" onfocus="WR.suggest()" onblur="setTimeout(WR.hideSuggest,180)">
          <div id="suggest" class="suggest" hidden></div>
        </form>
      </div>
      <nav class="navrow" aria-label="Main navigation">
        <a href="index.html" ${active === "report" ? 'aria-current="page"' : ""} class="${active === "report" ? "active" : ""}">${icon("report")} Overview</a>
        <details class="dd"><summary class="${active === "company" ? "active" : ""}">${icon("company")} Companies <span class="caret">⌄</span></summary><div class="dd-menu"><a href="index.html#companies"><strong>Explore all companies</strong><span>The full wireless ecosystem</span></a>${cats}</div></details>
        <a href="news.html" ${active === "news" ? 'aria-current="page"' : ""} class="${active === "news" ? "active" : ""}">${icon("news")} Newsroom</a>
        <a href="applications.html" ${active === "apps" ? 'aria-current="page"' : ""} class="${active === "apps" ? "active" : ""}">${icon("apps")} Applications</a>
        <a href="sources.html" ${active === "sources" ? 'aria-current="page"' : ""} class="${active === "sources" ? "active" : ""}">${icon("shield")} Sources &amp; Method</a>
      </nav></div></header>`;
    const main = document.querySelector("main");
    if (main) {
      main.id = main.id || "main-content";
      el.querySelector(".skip-link").href = "#" + main.id;
    }
    const menu = el.querySelector(".dd");
    document.addEventListener("click", (event) => { if (!menu.contains(event.target)) menu.open = false; });
    menu.addEventListener("keydown", (event) => { if (event.key === "Escape") { menu.open = false; menu.querySelector("summary").focus(); } });
    const f = document.getElementById("ftr");
    if (f) f.innerHTML = `<div class="wrap footer-grid"><div><a class="brand" href="index.html">${icon("signal")} Wireless Intelligence</a><p>Explore the companies, technologies and applications shaping the connected world.</p></div><div><span class="eyebrow">EVIDENCE, NOT GUESSWORK</span><p>Research from public news feeds and company websites. Every headline links to its source. Forecasts are indicative; verify before making decisions.</p><a href="sources.html">Explore our methodology ${icon("arrow")}</a></div></div>`;
  }

  // ---- search
  let sel = -1;
  function matches(q) {
    q = q.trim().toLowerCase();
    if (!q) return [];
    const res = [];
    IDX.companies.forEach((c) => { if (c.n.toLowerCase().includes(q) || c.id.includes(q)) res.push({ h: `company.html?id=${c.id}`, n: c.n, s: catName[c.cat] || c.cat }); });
    IDX.apps.forEach((a) => { if (a.n.toLowerCase().includes(q)) res.push({ h: `applications.html?app=${a.id}`, n: `${a.i} ${a.n}`, s: "Application" }); });
    return res.slice(0, 12);
  }
  function suggest() {
    const box = document.getElementById("suggest");
    const r = matches(document.getElementById("global-q").value);
    sel = -1;
    if (!r.length) return (box.hidden = true);
    box.innerHTML = r.map((x) => `<a href="${x.h}">${esc(x.n)}<span>${esc(x.s)}</span></a>`).join("");
    box.hidden = false;
  }
  function navSuggest(e) {
    if (e.key === "Escape") { hideSuggest(); return; }
    const items = [...document.querySelectorAll("#suggest a")];
    if (!items.length) return;
    if (e.key === "ArrowDown") sel = Math.min(sel + 1, items.length - 1);
    else if (e.key === "ArrowUp") sel = Math.max(sel - 1, 0);
    else return;
    e.preventDefault();
    items.forEach((a, i) => a.classList.toggle("on", i === sel));
  }
  function goSearch(e) {
    e.preventDefault();
    const items = [...document.querySelectorAll("#suggest a")];
    const q = document.getElementById("global-q").value.trim();
    if (!document.getElementById("suggest").hidden && items.length) location.href = items[Math.max(sel, 0)].getAttribute("href");
    else if (q) location.href = "news.html?q=" + encodeURIComponent(q);
    return false;
  }
  const hideSuggest = () => { const b = document.getElementById("suggest"); if (b) b.hidden = true; };

  // ---- SVG line chart with optional band
  function lineChart(years, mid, lo, hi, fmt) {
    const W = 860, H = 300, L = 64, R = 20, T = 18, B = 34;
    const all = mid.concat(lo || [], hi || []).filter((v) => v != null);
    const max = Math.max(...all) * 1.08, min = 0;
    const x = (i) => L + (i * (W - L - R)) / (years.length - 1);
    const y = (v) => T + (H - T - B) * (1 - (v - min) / (max - min || 1));
    let g = "";
    for (let k = 0; k <= 4; k++) {
      const v = min + ((max - min) * k) / 4;
      g += `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="#deebe4"/><text x="${L - 8}" y="${y(v) + 4}" fill="#68817a" font-size="11" text-anchor="end">${fmt(v)}</text>`;
    }
    years.forEach((yr, i) => { g += `<text x="${x(i)}" y="${H - 12}" fill="#64748b" font-size="11" text-anchor="middle">${yr}</text>`; });
    let band = "";
    if (lo && hi) {
      const top = hi.map((v, i) => `${x(i)},${y(v)}`).join(" ");
      const bot = lo.map((v, i) => `${x(i)},${y(v)}`).reverse().join(" ");
      band = `<polygon points="${top} ${bot}" fill="rgba(228,105,73,.12)" stroke="none"/>`;
    }
    const path = mid.map((v, i) => `${i ? "L" : "M"}${x(i)},${y(v)}`).join(" ");
    const area = path + ` L${x(mid.length - 1)},${y(0)} L${x(0)},${y(0)} Z`;
    const dots = mid.map((v, i) => `<circle cx="${x(i)}" cy="${y(v)}" r="3.5" fill="#07877b" stroke="#fff" stroke-width="1.5"><title>${years[i]}: ${fmt(v)}</title></circle>`).join("");
    const lab = `<text x="${x(mid.length - 1)}" y="${y(mid[mid.length - 1]) - 10}" fill="#244d46" font-size="12" font-weight="700" text-anchor="end">${fmt(mid[mid.length - 1])}</text>
           <text x="${x(0) + 4}" y="${y(mid[0]) - 10}" fill="#244d46" font-size="12" font-weight="700">${fmt(mid[0])}</text>`;
    return `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="${esc(`Market trend: ${years[0]} ${fmt(mid[0])} to ${years[years.length - 1]} ${fmt(mid[mid.length - 1])}`)}">
      <defs><linearGradient id="ag" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="rgba(8,127,117,.24)"/><stop offset="1" stop-color="rgba(8,127,117,0)"/></linearGradient></defs>
      ${g}${band}<path d="${area}" fill="url(#ag)"/><path class="trend-line" pathLength="1" d="${path}" fill="none" stroke="#07877b" stroke-width="2.5"/>${dots}${lab}</svg>`;
  }

  function bars(rows, max) {
    max = max || Math.max(1, ...rows.map((r) => r.v));
    return `<div class="bars">${rows.map((r) => `<div class="row"><div>${r.l}</div><div class="track"><div class="fill" style="width:${(100 * r.v) / max}%"></div></div><div class="n">${r.n ?? r.v}</div></div>`).join("")}</div>`;
  }

  const money = (v) => (v >= 1000 ? "$" + (v / 1000).toFixed(2) + "T" : v >= 1 ? "$" + v.toFixed(v >= 100 ? 0 : 1) + "B" : "$" + (v * 1000).toFixed(0) + "M");

  window.WR = { esc, qs, fmtDate, fmtFull, fmtPT, ago, newsItem, header, suggest, navSuggest, goSearch, hideSuggest, lineChart, bars, themeChips, tierBadge, momHtml, coName, catName, techCls, money, icon, TL: IDX.theme_labels || {} };
})();
