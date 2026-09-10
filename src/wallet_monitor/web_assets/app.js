/* Wallet Monitor dashboard. Vanilla JS, no build step, no CDN. */

const $ = (sel) => document.querySelector(sel);
const state = { chains: [], activeTab: "signals", timer: null, chartData: [] };

async function api(path, options) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json();
  if (data && data.error) throw new Error(data.error);
  return data;
}

/* ------------------------------------------------------------- formatting */

function ago(ts, now) {
  const seconds = Math.max(0, (now || Math.floor(Date.now() / 1000)) - ts);
  if (seconds < 90) return `${seconds}s ago`;
  const minutes = seconds / 60;
  if (minutes < 90) return `${Math.round(minutes)}m ago`;
  const hours = minutes / 60;
  if (hours < 48) return `${hours.toFixed(1)}h ago`;
  return `${(hours / 24).toFixed(1)}d ago`;
}

function money(value) {
  if (!value) return "";
  return "$" + Math.round(value).toLocaleString();
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function empty(container, message) {
  container.replaceChildren(el("p", "empty", message));
}

/* ------------------------------------------------------------------ state */

async function refreshState() {
  const data = await api("/api/state");
  state.chains = data.chains;

  $("#store-path").textContent = data.store_path;
  $("#kpi-wallets").textContent = data.stats.wallets.toLocaleString();
  $("#kpi-wallets-note").textContent = `across ${data.coverage.length} chain${
    data.coverage.length === 1 ? "" : "s"}`;
  $("#kpi-mints").textContent = data.stats.mints.toLocaleString();
  $("#kpi-mints-note").textContent = `${data.stats.contracts.toLocaleString()} distinct contracts`;

  const last = data.last_scan;
  $("#kpi-scan").textContent = last ? ago(last.at, data.now) : "never";
  $("#kpi-scan-note").textContent = last
    ? `${last.new_mints} new mints, ${last.new_signals} new signals`
    : "run a scan to start";

  const minWallets = $("#f-min-wallets");
  if (!minWallets.value) minWallets.value = data.rules.min_wallets;
  const windowSelect = $("#f-window");
  if (!windowSelect.dataset.ready) {
    windowSelect.value = String(data.rules.window_minutes);
    windowSelect.dataset.ready = "1";
  }

  fillChainSelects(data.coverage);
  renderCredentials(data.credentials);
  renderSources(data.notion_sources);
  renderCoverage(data.coverage);
  renderScanStatus(data.scan, data.last_scan);
  return data;
}

function fillChainSelects(coverage) {
  const filter = $("#f-chain");
  const chosen = filter.value;
  filter.replaceChildren(new Option("All chains", ""));
  coverage.forEach((row) => {
    filter.append(new Option(`${row.chain} (${row.wallets})`, row.chain));
  });
  filter.value = chosen;

  const adder = $("#add-chain");
  if (adder.dataset.ready) return;
  state.chains.forEach((c) => adder.append(new Option(c.name, c.key)));
  adder.dataset.ready = "1";
}

function renderScanStatus(scan, last) {
  const bar = $("#scan-status");
  const button = $("#scan-btn");
  button.disabled = scan.running;
  button.textContent = scan.running ? "Scanning…" : "Run scan";

  if (scan.running) {
    bar.hidden = false;
    bar.removeAttribute("data-tone");
    bar.textContent = "Scan in progress. Wallets are queried one at a time, so a large watchlist takes a while.";
    return;
  }
  const errors = (last && last.errors) || scan.errors || [];
  if (errors.length) {
    bar.hidden = false;
    bar.dataset.tone = "critical";
    bar.textContent = `Last scan reported ${errors.length} error${
      errors.length === 1 ? "" : "s"}: ${errors[0]}`;
    return;
  }
  if (scan.finished_at && scan.summary) {
    bar.hidden = false;
    bar.dataset.tone = "good";
    bar.textContent = scan.summary;
    return;
  }
  bar.hidden = true;
}

function renderCredentials(creds) {
  const required = [
    ["alchemy", "ALCHEMY_API_KEY", "EVM mints, one key for every network"],
    ["etherscan", "ETHERSCAN_API_KEY", "EVM fallback and HyperEVM"],
    ["helius", "HELIUS_API_KEY", "Solana mints and first buys"],
    ["notion", "NOTION_TOKEN", "importing the watchlist from Notion"],
    ["discord", "DISCORD_WEBHOOK_URL", "optional alert sink"],
    ["telegram", "TELEGRAM_BOT_TOKEN + CHAT_ID", "optional alert sink"],
  ];
  const list = $("#cred-list");
  list.replaceChildren(
    ...required.map(([key, name, why]) => {
      const optional = key === "discord" || key === "telegram";
      const set = creds[key];
      const row = el("li");
      row.append(el("span", `dot dot-${set ? "good" : optional ? "warning" : "critical"}`));
      row.append(el("b", null, name));
      row.append(el("span", "status-note", set ? "set" : optional ? "not set (optional)" : "missing"));
      row.append(el("span", "status-note", `— ${why}`));
      return row;
    })
  );
}

function renderSources(sources) {
  const list = $("#source-list");
  if (!sources.length) {
    list.replaceChildren(el("li", "status-note", "No Notion databases configured."));
    return;
  }
  list.replaceChildren(
    ...sources.map((source) => {
      const row = el("li");
      const link = el("a", null, source.name);
      link.href = source.url;
      link.target = "_blank";
      link.rel = "noreferrer";
      row.append(link);
      return row;
    })
  );
}

function renderCoverage(coverage) {
  const body = $("#coverage-table tbody");
  if (!coverage.length) {
    const row = el("tr");
    const cell = el("td", null, "No wallets yet.");
    cell.colSpan = 3;
    row.append(cell);
    body.replaceChildren(row);
    return;
  }
  body.replaceChildren(
    ...coverage.map((row) => {
      const tr = el("tr");
      tr.append(el("td", null, row.chain));
      tr.append(el("td", "num", String(row.wallets)));
      tr.append(el("td", null, row.provider || "no provider — set a key"));
      return tr;
    })
  );
}

/* ---------------------------------------------------------------- signals */

async function loadSignals() {
  const params = new URLSearchParams({
    hours: $("#f-hours").value,
    min_wallets: $("#f-min-wallets").value || "0",
    window_minutes: $("#f-window").value,
  });
  if ($("#f-chain").value) params.set("chain", $("#f-chain").value);

  const data = await api(`/api/signals?${params}`);
  const list = $("#signal-list");
  $("#kpi-signals").textContent = String(data.signals.length);
  $("#kpi-signals-note").textContent =
    `${data.rules.min_wallets}+ wallets within ${data.rules.window_minutes}m`;

  if (!data.signals.length) {
    empty(list, "Nothing clustered yet. Widen the window, lower the wallet threshold, or run a scan.");
    return;
  }
  const top = data.signals[0].score || 1;
  list.replaceChildren(...data.signals.map((s) => signalCard(s, top, data.now)));
}

function signalCard(signal, topScore, now) {
  const card = el("article", "signal");

  const score = el("div", "score");
  score.append(el("div", "score-value", signal.score.toFixed(1)));
  score.append(el("div", "score-label", "score"));
  const meter = el("div", "meter");
  const fill = el("span");
  fill.style.width = `${Math.max(6, (signal.score / topScore) * 100)}%`;
  meter.append(fill);
  score.append(meter);
  card.append(score);

  const body = el("div");
  const head = el("div", "signal-head");
  head.append(el("span", "signal-name", signal.collection_name || "Unnamed collection"));
  head.append(el("span", "badge", signal.chain_name));
  head.append(el("span", "badge", `${signal.wallet_count} wallets`));
  body.append(head);

  const meta = el("p", "signal-meta");
  meta.append(el("b", null, String(signal.mint_count)));
  meta.append(document.createTextNode(" mints clustered in "));
  meta.append(el("b", null, `${signal.window_minutes}m`));
  meta.append(document.createTextNode(", first seen "));
  meta.append(el("b", null, ago(signal.first_mint_at, now)));
  meta.append(document.createTextNode(", latest "));
  meta.append(el("b", null, ago(signal.last_mint_at, now)));
  body.append(meta);

  const links = el("div", "signal-links");
  if (signal.token_url) {
    const a = el("a", null, "Contract");
    a.href = signal.token_url;
    a.target = "_blank";
    a.rel = "noreferrer";
    links.append(a);
  }
  if (signal.tx_url) {
    const a = el("a", null, "Sample mint tx");
    a.href = signal.tx_url;
    a.target = "_blank";
    a.rel = "noreferrer";
    links.append(a);
  }
  links.append(el("span", "status-note", signal.contract));
  body.append(links);

  const chips = el("div", "chips");
  signal.wallets.forEach((wallet) => {
    const chip = el("span", "chip");
    const link = el("a", null, wallet.short);
    link.href = wallet.explorer;
    link.target = "_blank";
    link.rel = "noreferrer";
    chip.append(link);
    if (wallet.rank) chip.append(el("b", null, `#${wallet.rank}`));
    if (wallet.pnl_usd) chip.append(el("b", null, money(wallet.pnl_usd)));
    if (wallet.tag) chip.append(document.createTextNode(wallet.tag));
    chips.append(chip);
  });
  body.append(chips);
  card.append(body);
  return card;
}

/* --------------------------------------------------------------- activity */

async function loadActivity() {
  const data = await api("/api/activity?hours=48");
  state.chartData = data.hourly;
  drawChart();

  const body = $("#mint-table tbody");
  if (!data.mints.length) {
    const row = el("tr");
    const cell = el("td", null, "No mints recorded yet.");
    cell.colSpan = 6;
    row.append(cell);
    body.replaceChildren(row);
    return;
  }
  body.replaceChildren(
    ...data.mints.map((mint) => {
      const tr = el("tr");
      tr.append(el("td", null, ago(mint.timestamp, data.now)));
      tr.append(el("td", null, mint.chain));
      const wallet = el("td", "mono");
      wallet.append(document.createTextNode(mint.wallet_short));
      if (mint.wallet_tag) wallet.append(el("span", "status-note", ` ${mint.wallet_tag}`));
      tr.append(wallet);
      tr.append(el("td", null, mint.collection_name || "—"));
      tr.append(el("td", null, mint.token_standard));
      const links = el("td");
      if (mint.token_url) {
        const a = el("a", null, "contract");
        a.href = mint.token_url;
        a.target = "_blank";
        a.rel = "noreferrer";
        links.append(a);
      }
      tr.append(links);
      return tr;
    })
  );
}

const SVG_NS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs) {
  const node = document.createElementNS(SVG_NS, tag);
  Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, String(v)));
  return node;
}

/* A column anchored to the baseline with its data-end rounded. */
function barPath(x, y, w, h, r) {
  const radius = Math.min(r, w / 2, h);
  return `M${x},${y + h} L${x},${y + radius} Q${x},${y} ${x + radius},${y} ` +
         `L${x + w - radius},${y} Q${x + w},${y} ${x + w},${y + radius} L${x + w},${y + h} Z`;
}

function drawChart() {
  const host = $("#activity-chart");
  const buckets = state.chartData;
  if (!buckets.length) {
    empty(host, "No mint activity recorded in the last 48 hours.");
    return;
  }

  const width = Math.max(320, host.clientWidth || 720);
  const height = 190;
  const pad = { top: 12, right: 8, bottom: 24, left: 34 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const max = Math.max(...buckets.map((b) => b.count), 1);
  const step = plotW / buckets.length;
  const barW = Math.max(2, step - 2); // 2px surface gap between adjacent bars

  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width, height,
    role: "img", "aria-label": "Mints per hour over the last 48 hours" });

  [0, 0.5, 1].forEach((fraction) => {
    const y = pad.top + plotH - fraction * plotH;
    svg.append(svgEl("line", { class: fraction === 0 ? "baseline" : "grid-line",
      x1: pad.left, x2: width - pad.right, y1: y, y2: y }));
    const label = svgEl("text", { class: "axis-text", x: pad.left - 7, y: y + 3, "text-anchor": "end" });
    label.textContent = Math.round(fraction * max);
    svg.append(label);
  });

  buckets.forEach((bucket, index) => {
    const h = (bucket.count / max) * plotH;
    const x = pad.left + index * step;
    const y = pad.top + plotH - h;
    const bar = svgEl("path", { class: "bar", d: barPath(x, y, barW, Math.max(h, 1), 4) });
    const when = new Date(bucket.bucket * 1000);
    bar.addEventListener("mousemove", (event) =>
      showTooltip(event, `<b>${bucket.count}</b> mint${bucket.count === 1 ? "" : "s"}<br>` +
        when.toLocaleString([], { month: "short", day: "numeric", hour: "numeric" })));
    bar.addEventListener("mouseleave", hideTooltip);
    svg.append(bar);

    if (index % Math.ceil(buckets.length / 6) === 0) {
      const tick = svgEl("text", { class: "axis-text", x: x + barW / 2,
        y: height - 8, "text-anchor": "middle" });
      tick.textContent = when.toLocaleTimeString([], { hour: "numeric" });
      svg.append(tick);
    }
  });

  host.replaceChildren(svg);
}

function showTooltip(event, html) {
  const tip = $("#tooltip");
  tip.innerHTML = html;
  tip.hidden = false;
  tip.style.left = `${Math.min(event.clientX + 14, window.innerWidth - tip.offsetWidth - 12)}px`;
  tip.style.top = `${event.clientY - tip.offsetHeight - 10}px`;
}

function hideTooltip() {
  $("#tooltip").hidden = true;
}

/* -------------------------------------------------------------- watchlist */

async function loadWallets() {
  const data = await api("/api/wallets");
  const body = $("#wallet-table tbody");
  if (!data.wallets.length) {
    const row = el("tr");
    const cell = el("td", null, "No wallets yet. Paste a Notion column above, or import from Notion in Setup.");
    cell.colSpan = 7;
    row.append(cell);
    body.replaceChildren(row);
    return;
  }
  body.replaceChildren(
    ...data.wallets.map((wallet) => {
      const tr = el("tr");
      tr.append(el("td", null, wallet.chain));
      const address = el("td", "mono");
      const link = el("a", null, wallet.address);
      link.href = wallet.explorer;
      link.target = "_blank";
      link.rel = "noreferrer";
      address.append(link);
      tr.append(address);
      tr.append(el("td", "num", wallet.rank ? `#${wallet.rank}` : "—"));
      tr.append(el("td", "num", wallet.pnl_usd ? money(wallet.pnl_usd) : "—"));
      tr.append(el("td", null, wallet.tag || "—"));
      tr.append(el("td", null, wallet.source || "—"));
      const actions = el("td");
      const remove = el("button", "link-btn", "remove");
      remove.addEventListener("click", async () => {
        await api("/api/wallets/delete", {
          method: "POST",
          body: JSON.stringify({ chain: wallet.chain, address: wallet.address }),
        });
        await Promise.all([loadWallets(), refreshState()]);
      });
      actions.append(remove);
      tr.append(actions);
      return tr;
    })
  );
}

/* ------------------------------------------------------------------ wiring */

function note(selector, message, ok) {
  const node = $(selector);
  node.hidden = false;
  node.textContent = message;
  node.style.color = ok ? "" : "var(--critical)";
}

function switchTab(name) {
  state.activeTab = name;
  document.querySelectorAll(".tab").forEach((tab) =>
    tab.classList.toggle("is-active", tab.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((panel) =>
    panel.classList.toggle("is-active", panel.id === `panel-${name}`));
  if (name === "activity") loadActivity();
  if (name === "watchlist") loadWallets();
  if (name === "signals") loadSignals();
}

function bind() {
  document.querySelectorAll(".tab").forEach((tab) =>
    tab.addEventListener("click", () => switchTab(tab.dataset.tab)));

  ["#f-hours", "#f-min-wallets", "#f-window", "#f-chain"].forEach((sel) =>
    $(sel).addEventListener("change", loadSignals));

  $("#scan-btn").addEventListener("click", async () => {
    $("#scan-btn").disabled = true;
    await api("/api/scan", { method: "POST", body: JSON.stringify({}) });
    pollScan();
  });

  $("#add-btn").addEventListener("click", async () => {
    const text = $("#paste-box").value.trim();
    if (!text) return note("#add-note", "Paste some addresses first.", false);
    try {
      const data = await api("/api/wallets", {
        method: "POST",
        body: JSON.stringify({
          text,
          chain: $("#add-chain").value,
          tag: $("#add-tag").value,
          source: "web",
        }),
      });
      if (!data.added) return note("#add-note", "No addresses found in that text.", false);
      note("#add-note", `Added ${data.added} wallet${data.added === 1 ? "" : "s"}.`, true);
      $("#paste-box").value = "";
      await Promise.all([loadWallets(), refreshState()]);
    } catch (err) {
      note("#add-note", err.message, false);
    }
  });

  $("#sync-btn").addEventListener("click", async () => {
    $("#sync-btn").disabled = true;
    note("#sync-note", "Importing…", true);
    try {
      const data = await api("/api/sync", { method: "POST", body: JSON.stringify({}) });
      note("#sync-note", `${data.imported} wallets imported. ${data.notes.join(" · ")}`, true);
      await Promise.all([loadWallets(), refreshState()]);
    } catch (err) {
      note("#sync-note", err.message, false);
    } finally {
      $("#sync-btn").disabled = false;
    }
  });

  $("#auto-refresh").addEventListener("change", scheduleRefresh);
  window.addEventListener("resize", () => {
    if (state.activeTab === "activity") drawChart();
  });
}

async function pollScan() {
  const data = await api("/api/scan");
  renderScanStatus(data.scan, null);
  if (data.scan.running) {
    setTimeout(pollScan, 2000);
    return;
  }
  await refreshState();
  await loadSignals();
  if (state.activeTab === "activity") loadActivity();
}

function scheduleRefresh() {
  clearInterval(state.timer);
  if (!$("#auto-refresh").checked) return;
  state.timer = setInterval(async () => {
    await refreshState();
    if (state.activeTab === "signals") loadSignals();
    if (state.activeTab === "activity") loadActivity();
  }, 30000);
}

async function boot() {
  bind();
  await refreshState();
  await loadSignals();
  scheduleRefresh();
}

boot().catch((err) => {
  document.body.prepend(
    Object.assign(el("p", "scan-status", `Could not reach the server: ${err.message}`), {
      hidden: false,
    })
  );
});
