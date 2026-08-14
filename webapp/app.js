/* ── Earth Crypto Telegram Mini App ─────────────────────────────── */

(() => {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  if (tg) {
    try {
      tg.ready();
      tg.expand();
      if (tg.disableVerticalSwipes) tg.disableVerticalSwipes();
      tg.setHeaderColor && tg.setHeaderColor("#0d1117");
    } catch (e) { /* non-fatal */ }
  }

  const INIT_DATA = (tg && tg.initData) || "";
  const DEV_UID = new URLSearchParams(location.search).get("dev_uid");

  /* ── i18n ──────────────────────────────────────────────────────── */
  const I18N = {
    en: {
      sub: "Prices · Portfolio · Market",
      prices: "Prices", portfolio: "Portfolio", market: "Market",
      alerts: "Alerts", wallets: "Wallets",
      search: "Search coins…",
      price: "Price", change24: "24h",
      pv_total: "Portfolio value",
      pv_pnl: "Profit / Loss",
      alloc: "Allocation",
      holdings: "Holdings",
      no_holdings: "No holdings yet",
      no_holdings_sub: "Add coins with /set in the bot, then come back here.",
      mkt_total_mcap: "Total Market Cap",
      mkt_vol: "24h Volume",
      mkt_btc_dom: "BTC Dominance",
      mkt_eth_dom: "ETH Dominance",
      mkt_active: "Active Coins",
      mkt_24h: "Market 24h Change",
      fng: "Fear & Greed",
      trending: "Trending",
      no_alerts: "No alerts yet",
      no_alerts_sub: "Set price alerts with /alert in the bot.",
      above: "above", below: "below",
      triggered: "Triggered", target: "Target", current: "Now",
      del: "Delete",
      wallets_title: "Wallets",
      no_wallets: "No wallets yet",
      no_wallets_sub: "Add TRON addresses with /wallets in the bot.",
      total_trx: "Total TRX",
      refresh: "Refreshed",
      err: "Something went wrong. Pull the bot back up and retry.",
      err_unauthorized: "Not authorized — open this app from Telegram.",
      err_timeout: "Request timed out. Pull the bot back up and retry.",
      err_network: "No connection. Pull the bot back up and retry.",
      err_badjson: "Unexpected server reply. Pull the bot back up and retry.",
      lang_btn: "EN",
      back: "Back",
    },
    fa: {
      sub: "قیمت · پرتفو · بازار",
      prices: "قیمت‌ها", portfolio: "پرتفو", market: "بازار",
      alerts: "هشدارها", wallets: "کیف پول",
      search: "جستجوی ارزها…",
      price: "قیمت", change24: "۲۴ ساعت",
      pv_total: "ارزش پرتفو",
      pv_pnl: "سود / زیان",
      alloc: "ترکیب پرتفو",
      holdings: "دارایی‌ها",
      no_holdings: "هنوز دارایی ندارید",
      no_holdings_sub: "با /set در ربات ارزها را ثبت کنید و دوباره بیایید.",
      mkt_total_mcap: "ارزش کل بازار",
      mkt_vol: "حجم ۲۴ ساعت",
      mkt_btc_dom: "سلطه بیت‌کوین",
      mkt_eth_dom: "سلطه اتریوم",
      mkt_active: "ارزهای فعال",
      mkt_24h: "تغییر ۲۴ ساعت بازار",
      fng: "ترس و طمع",
      trending: "داغ‌ترین ارزها",
      no_alerts: "هنوز هشداری ندارید",
      no_alerts_sub: "با /alert در ربات هشدار قیمت تنظیم کنید.",
      above: "بالای", below: "پایین",
      triggered: "فعال شده", target: "هدف", current: "اکنون",
      del: "حذف",
      wallets_title: "کیف پول‌ها",
      no_wallets: "هنوز کیف پولی ندارید",
      no_wallets_sub: "با /wallets در ربات آدرس ترون اضافه کنید.",
      total_trx: "مجموع TRX",
      refresh: "به‌روزرسانی شد",
      err: "مشکلی پیش آمد. دوباره تلاش کنید.",
      err_unauthorized: "دسترسی غیرمجاز — این برنامه را از تلگرام باز کنید.",
      err_timeout: "زمان درخواست تمام شد. ربات را دوباره بالا بیاورید و تلاش کنید.",
      err_network: "اتصال برقرار نشد. ربات را دوباره بالا بیاورید و تلاش کنید.",
      err_badjson: "پاسخ سرور نامعتبر بود. ربات را دوباره بالا بیاورید و تلاش کنید.",
      lang_btn: "فا",
      back: "بازگشت",
    }
  };

  let lang = (tg && tg.initDataUnsafe && tg.initDataUnsafe.user && tg.initDataUnsafe.user.language_code || "en").toLowerCase().startsWith("fa") ? "fa" : "en";
  const T = (k) => I18N[lang][k] || I18N.en[k] || k;

  /* ── helpers ───────────────────────────────────────────────────── */
  const $ = (sel) => document.querySelector(sel);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function fmtNum(n, maxD = 2) {
    if (n == null || isNaN(n)) return "—";
    return Number(n).toLocaleString(lang === "fa" ? "fa-IR" : "en-US", { maximumFractionDigits: maxD });
  }
  function fmtUsd(n, maxD = 2) {
    if (n == null || isNaN(n)) return "—";
    return "$" + fmtNum(n, maxD);
  }
  function fmtCoin(n) {
    if (n == null || isNaN(n)) return "—";
    const v = Number(n);
    if (v >= 1000) return fmtNum(v, 0);
    if (v >= 1) return fmtNum(v, 2);
    if (v >= 0.0001) return fmtNum(v, 5);
    return fmtNum(v, 8);
  }

  let toastTimer = null;
  function toast(msg) {
    const el = $("#toast");
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 2000);
  }

  function showLoader(on) { $("#loader").classList.toggle("hidden", !on); }
  function setError(msg) {
    const el = document.createElement("div");
    el.className = "error-box";
    el.textContent = msg;
    const main = $("#main");
    main.prepend(el);
    setTimeout(() => el.remove(), 6000);
  }

  /* ── API ───────────────────────────────────────────────────────── */
  async function api(path) {
    const headers = {};
    if (INIT_DATA) headers["X-Telegram-Init-Data"] = INIT_DATA;
    const url = (DEV_UID && !INIT_DATA) ? `${path}${path.includes("?") ? "&" : "?"}dev_uid=${encodeURIComponent(DEV_UID)}` : path;
    const ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
    const timer = ctrl ? setTimeout(() => ctrl.abort(), 15000) : null;
    let res;
    try {
      res = await fetch(url, { headers, signal: ctrl ? ctrl.signal : undefined });
    } catch (e) {
      throw new Error(e && e.name === "AbortError" ? "timeout" : "network");
    } finally {
      if (timer) clearTimeout(timer);
    }
    if (res.status === 401) throw new Error("unauthorized");
    if (!res.ok) throw new Error("http_" + res.status);
    let data;
    try {
      data = await res.json();
    } catch (e) {
      throw new Error("badjson");
    }
    if (!data || data.ok !== true) throw new Error((data && data.error) || "api");
    return data;
  }

  async function request(path) {
    showLoader(true);
    try {
      return await api(path);
    } catch (e) {
      console.error(e);
      const msg =
        e.message === "unauthorized" ? T("err_unauthorized") :
        e.message === "timeout" ? T("err_timeout") :
        e.message === "network" ? T("err_network") :
        e.message === "badjson" ? T("err_badjson") :
        T("err");
      setError(msg);
      throw e;
    } finally {
      showLoader(false);
    }
  }

  /* ── state & tabs ──────────────────────────────────────────────── */
  let pricesCache = null;

  function setLang(l) {
    lang = l;
    $("#btn-lang").textContent = T("lang_btn");
    document.documentElement.lang = l;
    document.body.dir = l === "fa" ? "rtl" : "ltr";
    $("#brand-sub").textContent = T("sub");
    document.querySelectorAll(".tab-btn .lbl").forEach((el) => {
      const t = el.closest(".tab-btn").dataset.tab;
      el.textContent = T(t);
    });
  }

  function switchTab(name, force) {
    const current = $("#tabbar .tab-btn.active")?.dataset.tab;
    if (current === name && !force) return;
    document.querySelectorAll("#tabbar .tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    document.querySelectorAll("#main .tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
    const loaders = { prices: loadPrices, portfolio: loadPortfolio, market: loadMarket, alerts: loadAlerts, wallets: loadWallets };
    loaders[name] && loaders[name]();
  }

  $("#tabbar").addEventListener("click", (e) => {
    const btn = e.target.closest(".tab-btn");
    if (btn) switchTab(btn.dataset.tab);
  });
  $("#btn-refresh").addEventListener("click", () => {
    switchTab($("#tabbar .tab-btn.active").dataset.tab, true);
    toast(T("refresh") + " ✓");
  });
  $("#btn-lang").addEventListener("click", () => {
    setLang(lang === "en" ? "fa" : "en");
    switchTab($("#tabbar .tab-btn.active").dataset.tab, true);
  });

  /* ── rendering: prices ─────────────────────────────────────────── */
  function sparklineSVG(points) {
    if (!points || points.length < 2) return "";
    const min = Math.min(...points), max = Math.max(...points);
    const range = max - min || 1;
    const W = 58, H = 30, P = 2;
    const step = (W - P * 2) / (points.length - 1);
    const coords = points.map((p, i) => [P + i * step, H - P - ((p - min) / range) * (H - P * 2)]);
    const up = points[points.length - 1] >= points[0];
    const stroke = up ? "var(--green)" : "var(--red)";
    const path = coords.map(([x, y], i) => (i === 0 ? "M" : "L") + x.toFixed(1) + " " + y.toFixed(1)).join(" ");
    return `<svg class="spark" viewBox="0 0 ${W} ${H}"><polyline points="${coords.map(([x, y]) => x.toFixed(1) + "," + y.toFixed(1)).join(" ")}" style="stroke:${stroke}" /></svg>`;
  }

  function changeBadge(chg) {
    if (chg == null) return `<span class="chg-badge flat">—</span>`;
    const cls = chg > 0 ? "up" : chg < 0 ? "down" : "flat";
    const arrow = chg > 0 ? "▲" : chg < 0 ? "▼" : "•";
    return `<span class="chg-badge ${cls}">${arrow} ${fmtNum(Math.abs(chg), 2)}%</span>`;
  }

  function renderPrices(data) {
    pricesCache = data;
    const el = $("#tab-prices");
    const coins = data.coins || [];
    const html = `
      <div class="search-wrap"><span class="search-ic" aria-hidden="true">🔎</span>
        <input id="coin-search" type="text" placeholder="${T("search")}" aria-label="${T("search")}" autocomplete="off"></div>
      <div id="coin-list">${coins.map(coinRow).join("") || emptyState("💱", T("price"), "")}</div>`;
    el.innerHTML = html;
    const input = $("#coin-search");
    input.addEventListener("input", () => {
      const q = input.value.trim().toLowerCase();
      const filtered = coins.filter((c) => c.name.toLowerCase().includes(q) || c.sym.toLowerCase().includes(q));
      $("#coin-list").innerHTML = filtered.map(coinRow).join("") || emptyState("🔍", T("price"), "");
    });
  }

  function coinRow(c) {
    const icons = { "🪙": "🪙", "⭐": "⭐" };
    return `
      <div class="coin-row">
        <div class="coin-icon">${icons[c.icon] || "🪙"}</div>
        <div class="coin-info">
          <div class="coin-name">${esc(c.name)}</div>
          <div class="coin-sym">${esc(c.sym)}</div>
        </div>
        ${c.sparkline ? sparklineSVG(c.sparkline) : ""}
        <div class="coin-price-col">
          <div class="coin-price">${fmtUsd(c.price)}</div>
          ${changeBadge(c.change)}
        </div>
      </div>`;
  }

  async function loadPrices(force) {
    const el = $("#tab-prices");
    if (pricesCache && !force) { renderPrices(pricesCache); return; }
    try { renderPrices(await request("/api/prices")); } catch (e) { el.innerHTML = emptyState("📉", T("price"), T("err")); }
  }

  /* ── rendering: portfolio ──────────────────────────────────────── */
  function renderPortfolio(d) {
    const el = $("#tab-portfolio");
    if (!d.items || d.items.length === 0) {
      el.innerHTML = emptyState("💼", T("no_holdings"), T("no_holdings_sub"));
      return;
    }
    const totalVal = d.total_value || 0;
    const totalPnl = d.total_pnl || 0;
    const totalPct = d.total_pnl_pct;
    const pnlCls = totalPnl > 0 ? "up" : totalPnl < 0 ? "down" : "flat";
    const colors = ["#3f9ef0", "#6e40c9", "#3fb950", "#e3b341", "#f0883e", "#f85149", "#2dba8f", "#b083f0", "#58a6ff", "#e3b341"];

    let donut = "", legend = "";
    if (d.items.length > 1) {
      const parts = d.items
        .map((it, i) => ({ pct: totalVal ? (it.value / totalVal) * 100 : 0, color: colors[i % colors.length], name: it.name, sym: it.sym }))
        .filter((p) => p.pct > 0.5)
        .sort((a, b) => b.pct - a.pct);
      let acc = 0;
      const segs = parts.map((p) => {
        const s = `${p.color} ${acc.toFixed(1)}deg ${(acc + p.pct * 3.6).toFixed(1)}deg`;
        acc += p.pct * 3.6;
        return s;
      }).join(", ");
      donut = `<div class="donut" style="background:conic-gradient(${segs || "var(--border) 0deg 360deg"})"></div>`;
      legend = `<div class="donut-legend">${parts.map((p) => `
        <div class="legend-row"><span class="legend-dot" style="background:${p.color}"></span>
          <span class="legend-name">${esc(p.name)}</span>
          <span class="legend-pct">${fmtNum(p.pct, 1)}%</span></div>`).join("")}</div>`;
    } else {
      donut = `<div class="donut" style="background:conic-gradient(${colors[0]} 0deg 360deg)"></div>`;
      legend = "";
    }

    el.innerHTML = `
      <div class="hero-card">
        <div class="hero-label">${T("pv_total")}</div>
        <div class="hero-value">${fmtUsd(totalVal)}</div>
        <div class="hero-sub ${pnlCls}">${T("pv_pnl")}: ${totalPnl >= 0 ? "+" : "−"}${fmtUsd(Math.abs(totalPnl))} ${totalPct != null ? `(${totalPct >= 0 ? "+" : "−"}${fmtNum(Math.abs(totalPct), 2)}%)` : ""}</div>
      </div>
      ${d.items.length > 1 ? `<div class="card"><div class="section-title" style="margin:0 0 10px">${T("alloc")}</div><div class="donut-wrap">${donut}${legend}</div></div>` : ""}
      <div class="section-title">${T("holdings")}</div>
      <div id="pf-list">${d.items.map(pfRow).join("")}</div>`;
  }

  function pfRow(it) {
    const pnl = it.pnl;
    const cls = pnl > 0 ? "up" : pnl < 0 ? "down" : "flat";
    const sign = pnl >= 0 ? "+" : "−";
    const pctStr = it.pnl_pct != null ? ` ${sign}${fmtNum(Math.abs(it.pnl_pct), 2)}%` : "";
    return `
      <div class="pf-row">
        <div class="coin-icon">🪙</div>
        <div class="pf-info">
          <div class="pf-sym">${esc(it.sym)}</div>
          <div class="pf-meta">${fmtCoin(it.amount)} ${esc(it.sym)}${it.buy ? " · " + T("price") + " " + fmtUsd(it.buy) : ""}</div>
        </div>
        <div class="pf-val">
          <div class="pf-amount">${fmtUsd(it.value)}</div>
          <div class="pf-pnl ${cls}">${it.pnl == null ? "—" : sign + fmtUsd(Math.abs(it.pnl)) + pctStr}</div>
        </div>
      </div>`;
  }

  async function loadPortfolio(force) {
    const el = $("#tab-portfolio");
    try { renderPortfolio(await request("/api/portfolio")); } catch (e) { el.innerHTML = emptyState("💼", T("err"), ""); }
  }

  /* ── rendering: market ─────────────────────────────────────────── */
  function renderMarket(d) {
    const el = $("#tab-market");
    const g = d.global || {};
    const fng = d.fear_greed || {};
    const mcap = g.total_market_cap_usd, vol = g.total_volume_usd;
    const btcDom = g.market_cap_percentage_btc, ethDom = g.market_cap_percentage_eth;
    const chg = g.market_cap_change_percentage_24h_usd;
    const fngVal = fng.value;
    const fngPct = fngVal != null ? Math.max(0, Math.min(100, fngVal)) : 50;

    let fngBlock = "";
    if (fngVal != null) {
      const fngTxt = fng.value_classification || "";
      fngBlock = `
        <div class="card">
          <div class="section-title" style="margin:0 0 10px">${T("fng")}</div>
          <div class="fng-scale"><div class="fng-marker" style="left:${fngPct}%"></div></div>
          <div class="fng-row">
            <div class="fng-value">${fmtNum(fngVal)} <span style="font-size:13px;color:var(--muted)">${esc(fngTxt)}</span></div>
            <div class="fng-label">0 · 50 · 100</div>
          </div>
        </div>`;
    }

    el.innerHTML = `
      <div class="hero-card">
        <div class="hero-label">${T("mkt_total_mcap")}</div>
        <div class="hero-value">${fmtUsd(mcap, 0)}</div>
        ${chg != null ? `<div class="hero-sub ${chg >= 0 ? "up" : "down"}">${chg >= 0 ? "▲" : "▼"} ${fmtNum(Math.abs(chg), 2)}%</div>` : ""}
      </div>
      <div class="stat-grid">
        <div class="stat"><div class="stat-label">${T("mkt_vol")}</div><div class="stat-value">${fmtUsd(vol, 0)}</div></div>
        <div class="stat"><div class="stat-label">${T("mkt_active")}</div><div class="stat-value">${fmtNum(g.active_cryptocurrencies, 0)}</div></div>
        <div class="stat">
          <div class="stat-label">${T("mkt_btc_dom")}</div>
          <div class="stat-value">${btcDom != null ? fmtNum(btcDom, 1) + "%" : "—"}</div>
          ${btcDom != null ? `<div class="bar-track"><div class="bar-fill" style="width:${btcDom}%;background:var(--gold)"></div></div>` : ""}
        </div>
        <div class="stat">
          <div class="stat-label">${T("mkt_eth_dom")}</div>
          <div class="stat-value">${ethDom != null ? fmtNum(ethDom, 1) + "%" : "—"}</div>
          ${ethDom != null ? `<div class="bar-track"><div class="bar-fill" style="width:${ethDom}%;background:var(--accent-2)"></div></div>` : ""}
        </div>
      </div>
      ${fngBlock}
      <div class="section-title">🔥 ${T("trending")}</div>
      ${(d.trending || []).map((t, i) => `
        <div class="trend-row">
          <div class="trend-rank">${i + 1}</div>
          ${t.thumb ? `<img class="trend-icon" src="${esc(t.thumb)}" alt="" loading="lazy">` : `<div class="trend-icon">🪙</div>`}
          <div class="trend-info">
            <div class="trend-name">${esc(t.name)}</div>
            <div class="trend-sym">${esc(t.symbol)}${t.market_cap_rank ? " · #" + fmtNum(t.market_cap_rank, 0) : ""}</div>
          </div>
          <div class="coin-price">${t.price_btc != null ? fmtCoin(t.price_btc) + " BTC" : ""}</div>
        </div>`).join("") || emptyState("🌍", T("trending"), "")}`;

    if (fngVal != null) requestAnimationFrame(() => {
      const m = $("#tab-market .fng-marker");
      if (m) m.style.left = fngPct + "%";
    });
  }

  async function loadMarket(force) {
    const el = $("#tab-market");
    try { renderMarket(await request("/api/market")); } catch (e) { el.innerHTML = emptyState("🌍", T("err"), ""); }
  }

  /* ── rendering: alerts ─────────────────────────────────────────── */
  function renderAlerts(d) {
    const el = $("#tab-alerts");
    const items = d.items || [];
    if (items.length === 0) { el.innerHTML = emptyState("🔔", T("no_alerts"), T("no_alerts_sub")); return; }
    el.innerHTML = `<div class="section-title" style="margin-top:0">${T("alerts")} · ${fmtNum(items.length, 0)}</div>` +
      items.map((a) => `
        <div class="alert-row">
          <div class="coin-icon">🔔</div>
          <div class="alert-info">
            <div class="alert-head">
              <span class="alert-sym">${esc(a.sym)}</span>
              <span class="alert-tag ${a.direction}">${T(a.direction)}</span>
              ${a.triggered ? `<span class="alert-tag" style="color:var(--gold);background:rgba(227,179,65,.15)">${T("triggered")}</span>` : ""}
            </div>
            <div class="alert-meta">${T("target")}: ${fmtUsd(a.target)} · ${T("current")}: ${fmtUsd(a.price)}</div>
          </div>
          <button class="alert-del" data-del-alert="${a.id}" title="${T("del")}" aria-label="${T("del")}">✕</button>
        </div>`).join("");
    el.querySelectorAll("[data-del-alert]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          await request("/api/alerts/delete?alert_id=" + encodeURIComponent(btn.dataset.delAlert));
          toast(T("del") + " ✓");
          renderAlerts(await request("/api/alerts"));
        } catch (e) { /* toast already shown */ }
      });
    });
  }

  async function loadAlerts(force) {
    const el = $("#tab-alerts");
    try { renderAlerts(await request("/api/alerts")); } catch (e) { el.innerHTML = emptyState("🔔", T("err"), ""); }
  }

  /* ── rendering: wallets ────────────────────────────────────────── */
  function renderWallets(d) {
    const el = $("#tab-wallets");
    const items = d.items || [];
    if (items.length === 0) { el.innerHTML = emptyState("👛", T("no_wallets"), T("no_wallets_sub")); return; }
    el.innerHTML = `
      <div class="hero-card">
        <div class="hero-label">${T("total_trx")}</div>
        <div class="hero-value">${fmtCoin(d.total_trx)} TRX</div>
        <div class="hero-sub">${fmtUsd(d.total_usd)}</div>
      </div>
      <div class="section-title" style="margin-top:0">${T("wallets_title")}</div>
      ${items.map((w) => `
        <div class="wallet-row">
          <div class="wallet-ic">👛</div>
          <div class="wallet-info">
            <div class="wallet-addr" dir="ltr">${esc(w.address)}</div>
            <div class="wallet-meta">${T("current")}: ${fmtUsd(w.balance_usd)}</div>
          </div>
          <div class="wallet-val">
            <div class="wallet-trx">${fmtCoin(w.balance_trx)} TRX</div>
            <div class="wallet-usd">${w.balance_trx == null ? "—" : fmtUsd(w.balance_trx * (d.trx_price || 0))}</div>
          </div>
          <button class="alert-del" data-del-wallet="${esc(w.address)}" title="${T("del")}" aria-label="${T("del")}">✕</button>
        </div>`).join("")}`;
    el.querySelectorAll("[data-del-wallet]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          await request("/api/wallets/delete?address=" + encodeURIComponent(btn.dataset.delWallet));
          toast(T("del") + " ✓");
          renderWallets(await request("/api/wallets"));
        } catch (e) { /* toast already shown */ }
      });
    });
  }

  async function loadWallets(force) {
    const el = $("#tab-wallets");
    try { renderWallets(await request("/api/wallets")); } catch (e) { el.innerHTML = emptyState("👛", T("err"), ""); }
  }

  /* ── shared ────────────────────────────────────────────────────── */
  function emptyState(ic, title, sub) {
    return `<div class="empty"><span class="empty-ic">${ic}</span><div class="empty-title">${esc(title)}</div>${sub ? `<div class="empty-sub">${esc(sub)}</div>` : ""}</div>`;
  }

  /* ── boot ──────────────────────────────────────────────────────── */
  function applyTheme() {
    if (!tg || !tg.themeParams) return;
    const p = tg.themeParams;
    const set = (v, def) => (v ? String(v) : def);
    document.documentElement.style.setProperty("--bg", set(p.bg_color, "#0d1117"));
    document.documentElement.style.setProperty("--surface", set(p.secondary_bg_color, "#161b22"));
    document.documentElement.style.setProperty("--text", set(p.text_color, "#e6edf3"));
    document.documentElement.style.setProperty("--muted", set(p.hint_color, "#8b949e"));
    document.documentElement.style.setProperty("--accent", set(p.button_color, "#3f9ef0"));
    const card = p.secondary_bg_color ? p.secondary_bg_color : "#1c2129";
    document.documentElement.style.setProperty("--card", card);
    document.documentElement.style.setProperty("--card-2", set(p.secondary_bg_color, "#21262e"));
  }

  setLang(lang);
  applyTheme();
  switchTab("prices");
})();
