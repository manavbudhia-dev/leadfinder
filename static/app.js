const $ = (id) => document.getElementById(id);

const STATUS_LABEL = {
  valid: "✅ Verified",
  risky: "⚠️ Risky (check)",
  not_found: "❌ Not found",
};
const STATUS_COLORS = { valid: "#22c55e", risky: "#f59e0b", not_found: "#ef4444" };
const KEY_ENVS = ["REOON_API_KEY", "HUNTER_API_KEY", "PROSPEO_API_KEY", "APOLLO_API_KEY"];
const PROVIDERS = ["hunter", "prospeo", "apollo"];

let csvFile = null;
let currentJobId = null;
let jobTimer = null;
let currentJob = null;

function setMsg(el, text, kind) {
  el.textContent = text || "";
  el.className = "msg" + (kind ? " " + kind : "");
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (res.status === 401) {
    location.href = "/login";
    throw new Error("Login zaruri hai.");
  }
  const body = await res.json();
  if (!res.ok) throw new Error(body.error || `Request fail (${res.status})`);
  return body;
}

const postJson = (path, data) => api(path, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(data || {}),
});

// ---------- tables ----------
function renderTable(container, rows, columns, opts = {}) {
  container.innerHTML = "";
  if (!rows.length) {
    container.textContent = "Nothing here yet.";
    return;
  }
  const wrap = document.createElement("div");
  wrap.className = "table-wrap";
  const table = document.createElement("table");
  const thead = document.createElement("tr");
  const hasActions = opts.actionLabel || (opts.actions && opts.actions.length);
  columns.concat(hasActions ? ["actions"] : []).forEach((c) => {
    const th = document.createElement("th");
    th.textContent = c;
    thead.appendChild(th);
  });
  table.appendChild(thead);
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    columns.forEach((c) => {
      const td = document.createElement("td");
      if (opts.customCells && opts.customCells[c]) {
        opts.customCells[c](row, td);
      } else {
        const val = row[c] == null ? "" : String(row[c]);
        if (c === opts.badgeCol && STATUS_LABEL[val]) {
          const b = document.createElement("span");
          b.className = "badge " + val;
          b.textContent = STATUS_LABEL[val];
          td.appendChild(b);
        } else {
          td.textContent = val;
        }
      }
      tr.appendChild(td);
    });
    if (hasActions) {
      const td = document.createElement("td");
      const actions = opts.actions || [{ label: opts.actionLabel, onClick: opts.onAction }];
      actions.forEach((a) => {
        const btn = document.createElement("button");
        btn.textContent = a.label;
        btn.addEventListener("click", () => a.onClick(row, btn));
        td.appendChild(btn);
      });
      tr.appendChild(td);
    }
    table.appendChild(tr);
  });
  wrap.appendChild(table);
  container.appendChild(wrap);
}

// ---------- settings ----------
async function loadSettings() {
  const s = await api("/api/settings");
  KEY_ENVS.forEach((env) => {
    const input = $("key-" + env);
    input.value = "";
    input.placeholder = s.keys[env] ? "•••••• saved. Type to replace." : "Not set";
  });
  PROVIDERS.forEach((p) => { $("lim-" + p).value = s.limits[p]; });
  paintSettings(s);
  $("flag-pattern").checked = s.flags.pattern_guess;
  $("flag-push").checked = s.flags.push_valid;
  $("webhook-url").value = "";
  $("webhook-url").placeholder = s.webhook_set ? "•••••• saved" : "https://hooks.zapier.com/...";
  $("logout-btn").classList.toggle("hidden", !s.auth);
}

$("save-settings").addEventListener("click", async () => {
  const keys = {};
  KEY_ENVS.forEach((env) => {
    const v = $("key-" + env).value.trim();
    if (v || $("key-" + env).placeholder === "Not set") keys[env] = v;
  });
  const limits = {};
  PROVIDERS.forEach((p) => { limits[p] = $("lim-" + p).value; });
  const payload = {
    keys, limits,
    flags: { pattern_guess: $("flag-pattern").checked, push_valid: $("flag-push").checked },
  };
  if ($("webhook-url").value.trim() || $("webhook-url").placeholder === "https://hooks.zapier.com/...") {
    payload.webhook_url = $("webhook-url").value.trim();
  }
  try {
    await postJson("/api/settings", payload);
    setMsg($("settings-msg"), "Settings saved.", "ok");
    await loadSettings();
    loadAlerts();
  } catch (e) {
    setMsg($("settings-msg"), e.message, "err");
  }
});

$("logout-btn").addEventListener("click", async () => {
  await fetch("/api/logout", { method: "POST" });
  location.href = "/login";
});

// ---------- tabs ----------
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === btn.dataset.tab));
    document.body.dataset.tab = btn.dataset.tab;
    document.querySelectorAll(".panel").forEach((p) =>
      p.classList.toggle("active", p.id === "tab-" + btn.dataset.tab));
    if (btn.dataset.tab === "single") loadRecent();
    if (btn.dataset.tab === "dash") loadDashboard();
    if (btn.dataset.tab === "csv") loadJobsList();
    if (btn.dataset.tab === "auto") loadAutomation();
    if (btn.dataset.goto) setTimeout(() => $(btn.dataset.goto).scrollIntoView({ behavior: "smooth" }), 150);
  });
});

function openTab(name) {
  document.querySelector(`[data-tab=${name}]`).click();
}

// ---------- quota alerts ----------
async function loadAlerts() {
  try {
    const quota = await api("/api/quota");
    paintQuota(quota);
    const box = $("quota-alerts");
    box.innerHTML = "";
    quota.forEach((q) => {
      if (!q.exhausted && !q.low) return;
      const div = document.createElement("div");
      div.className = "alert " + (q.exhausted ? "err" : "warn");
      div.textContent = q.exhausted
        ? `${cap(q.provider)} credits are used up (${q.used}/${q.monthly_limit}). Resets ${q.resets_on}.`
        : `${cap(q.provider)} credits are running low: ${q.remaining} left (${q.used}/${q.monthly_limit}).`;
      box.appendChild(div);
    });
  } catch (e) { /* alerts are best-effort */ }
  refreshNotifs();
}

// ---------- single lead ----------
$("find-btn").addEventListener("click", async () => {
  const payload = {
    first_name: $("first").value,
    last_name: $("last").value,
    domain: $("domain").value,
    linkedin_url: $("linkedin").value.trim(),
  };
  const btn = $("find-btn");
  btn.disabled = true;
  setMsg($("single-msg"), "Dhundh raha hoon aur verify kar raha hoon...", "warn");
  $("single-result").classList.add("hidden");
  $("single-log-wrap").classList.add("hidden");
  try {
    const r = await postJson("/api/find", payload);
    setMsg($("single-msg"), "");
    const card = $("single-result");
    card.className = "card " + r.status;
    card.textContent = r.email
      ? `${STATUS_LABEL[r.status]}: ${r.email} (source: ${r.source})`
      : STATUS_LABEL.not_found;
    card.classList.remove("hidden");
    $("single-log").textContent = r.log || "-";
    $("single-log-wrap").classList.remove("hidden");
    loadAlerts();
    loadRecent();
  } catch (e) {
    setMsg($("single-msg"), e.message, "err");
  } finally {
    btn.disabled = false;
  }
});

// ---------- CSV + column mapping + jobs ----------
let csvHeader = null;
let csvRawRows = null;
let mappedRows = null;

const HEADER_SYNONYMS = {
  first_name: ["first_name", "firstname", "first", "fname", "given_name"],
  last_name: ["last_name", "lastname", "last", "lname", "surname", "family_name"],
  domain: ["domain", "company_domain", "website", "company_website", "url", "company_url", "web"],
};
const normHeader = (h) => h.trim().toLowerCase().replace(/[\s-]+/g, "_");

function autoMap(header) {
  const normed = header.map(normHeader);
  const map = {};
  for (const target of ["first_name", "last_name", "domain"]) {
    const idx = normed.findIndex((h) => h === target || HEADER_SYNONYMS[target].includes(h));
    map[target] = idx >= 0 ? idx : null;
  }
  return map;
}

function applyMapping(map) {
  mappedRows = csvRawRows.map((cells) => {
    const row = {
      first_name: cells[map.first_name] ?? "",
      last_name: cells[map.last_name] ?? "",
      domain: cells[map.domain] ?? "",
    };
    csvHeader.forEach((h, i) => {
      const key = normHeader(h);
      if (!(key in row) && ![map.first_name, map.last_name, map.domain].includes(i)) row[key] = cells[i] ?? "";
    });
    return row;
  });
  $("csv-mapping").classList.add("hidden");
  setMsg($("csv-msg"), `${mappedRows.length} leads mile. Preview:`, "ok");
  renderTable($("csv-preview"), mappedRows.slice(0, 5), Object.keys(mappedRows[0] || {}));
  $("csv-preview").classList.remove("hidden");
  if (mappedRows.length) $("batch-btn").classList.remove("hidden");
}

function showMappingPanel(map) {
  ["first", "last", "domain"].forEach((field) => {
    const sel = $("map-" + field);
    sel.innerHTML = "";
    const none = document.createElement("option");
    none.value = "";
    none.textContent = "-- choose --";
    sel.appendChild(none);
    csvHeader.forEach((h, i) => {
      const opt = document.createElement("option");
      opt.value = i;
      opt.textContent = h;
      sel.appendChild(opt);
    });
    const target = field === "first" ? "first_name" : field === "last" ? "last_name" : "domain";
    if (map[target] != null) sel.value = String(map[target]);
  });
  $("csv-mapping").classList.remove("hidden");
  setMsg($("csv-msg"), "Headers alag hain — columns map karo:", "warn");
}

$("map-apply").addEventListener("click", () => {
  const map = {
    first_name: $("map-first").value === "" ? null : Number($("map-first").value),
    last_name: $("map-last").value === "" ? null : Number($("map-last").value),
    domain: $("map-domain").value === "" ? null : Number($("map-domain").value),
  };
  if (map.first_name == null || map.last_name == null || map.domain == null) {
    setMsg($("csv-msg"), "Teeno columns choose karo.", "err");
    return;
  }
  applyMapping(map);
});

function parseCsvLine(line) {
  const out = [];
  let cur = "", inQ = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (inQ) {
      if (ch === '"' && line[i + 1] === '"') { cur += '"'; i++; }
      else if (ch === '"') inQ = false;
      else cur += ch;
    } else if (ch === '"') inQ = true;
    else if (ch === ",") { out.push(cur); cur = ""; }
    else cur += ch;
  }
  out.push(cur);
  return out;
}

function rowsToCsv(rows) {
  const cols = [];
  rows.forEach((r) => Object.keys(r).forEach((k) => { if (!cols.includes(k)) cols.push(k); }));
  const esc = (v) => {
    const s = v == null ? "" : String(v);
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  return [cols.join(",")].concat(rows.map((r) => cols.map((c) => esc(r[c])).join(","))).join("\n");
}

$("csv-file").addEventListener("change", async () => {
  csvFile = $("csv-file").files[0] || null;
  csvHeader = csvRawRows = mappedRows = null;
  $("batch-btn").classList.add("hidden");
  $("csv-preview").classList.add("hidden");
  $("csv-mapping").classList.add("hidden");
  setMsg($("csv-msg"), "");
  if (!csvFile) return;
  try {
    const text = await csvFile.text();
    const lines = text.split(/\r?\n/).filter((l) => l.trim());
    csvHeader = parseCsvLine(lines[0] || "");
    csvRawRows = lines.slice(1).map(parseCsvLine);
    if (!csvRawRows.length) {
      setMsg($("csv-msg"), "The CSV is empty.", "err");
      return;
    }
    const map = autoMap(csvHeader);
    if (map.first_name != null && map.last_name != null && map.domain != null) {
      applyMapping(map);
    } else {
      showMappingPanel(map);
    }
  } catch (e) {
    setMsg($("csv-msg"), "CSV padhi nahi ja saki: " + e.message, "err");
  }
});

$("batch-btn").addEventListener("click", async () => {
  if (!csvFile || !mappedRows) return;
  const btn = $("batch-btn");
  btn.disabled = true;
  try {
    const form = new FormData();
    const normalized = new File([rowsToCsv(mappedRows)], csvFile.name, { type: "text/csv" });
    form.append("file", normalized);
    const r = await api("/api/jobs", { method: "POST", body: form });
    setMsg($("csv-msg"), `Job shuru ho gaya (${r.total} leads) — background mein chalega.`, "ok");
    openJob(r.job_id);
    loadJobsList();
  } catch (e) {
    setMsg($("csv-msg"), e.message, "err");
  } finally {
    btn.disabled = false;
  }
});

function openJob(jobId) {
  currentJobId = jobId;
  if (jobTimer) clearInterval(jobTimer);
  $("job-panel").classList.remove("hidden");
  pollJob();
  jobTimer = setInterval(pollJob, 1500);
}

async function pollJob() {
  if (!currentJobId) return;
  let job;
  try {
    job = await api("/api/jobs/" + currentJobId);
  } catch (e) {
    setMsg($("job-status"), e.message, "err");
    return;
  }
  currentJob = job;
  $("job-title").textContent = `Job ${job.id} (${job.kind}) — ${job.filename || ""}`;
  const pct = job.total ? (job.done / job.total) * 100 : 0;
  $("bar-fill").style.width = pct.toFixed(1) + "%";
  $("bar-text").textContent = `${job.done}/${job.total} leads process ho chuke`;

  const running = job.status === "running" || job.status === "queued";
  if (running) {
    setMsg($("job-status"), "⏳ Chal raha hai...", "warn");
  } else if (job.status === "paused") {
    setMsg($("job-status"), "⏸️ Paused: " + (job.error || ""), "err");
  } else {
    const c = { valid: 0, risky: 0, not_found: 0 };
    job.rows.forEach((r) => { if (r.state === "done" && c[r.status] != null) c[r.status]++; });
    const failed = job.rows.filter((r) => r.state === "failed").length;
    setMsg($("job-status"),
      `Ho gaya: ${c.valid} sahi, ${c.risky} risky, ${c.not_found} nahi mile` +
      (failed ? `, ${failed} failed (retry karo)` : ""), failed ? "warn" : "ok");
  }
  $("resume-btn").classList.toggle("hidden", running);
  $("download-btn").classList.toggle("hidden", !job.rows.some((r) => r.state === "done"));

  const display = job.rows.map((r) => ({
    ...r,
    status: r.state === "pending" ? "⏳ pending" : r.state === "failed" ? "💥 failed" : r.status,
  }));
  renderTable($("batch-table"), display,
    ["idx", "first_name", "last_name", "domain", "email", "status", "source"], { badgeCol: "status" });

  if (!running) {
    clearInterval(jobTimer);
    jobTimer = null;
    loadJobsList();
    loadAlerts();
  }
}

$("resume-btn").addEventListener("click", async () => {
  if (!currentJobId) return;
  try {
    await postJson(`/api/jobs/${currentJobId}/resume`, { include_not_found: $("retry-notfound").checked });
    openJob(currentJobId);
  } catch (e) {
    setMsg($("job-status"), e.message, "err");
  }
});

$("download-btn").addEventListener("click", () => {
  if (!currentJob) return;
  const rows = currentJob.rows.filter((r) => r.state === "done");
  if (!rows.length) return;
  const cols = [];
  rows.forEach((r) => Object.keys(r).forEach((k) => {
    if (!["idx", "email_in", "extra", "state", "log", "job_id"].includes(k) && !cols.includes(k)) cols.push(k);
  }));
  const esc = (v) => {
    const s = v == null ? "" : String(v);
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  const csv = [cols.join(",")].concat(rows.map((r) => cols.map((c) => esc(r[c])).join(","))).join("\n");
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = `results_${currentJob.id}.csv`;
  a.click();
  URL.revokeObjectURL(url);
});

async function loadJobsList() {
  try {
    const list = await api("/api/jobs");
    const rows = list.map((j) => ({
      id: j.id, kind: j.kind, status: j.status,
      progress: `${j.done}/${j.total}`,
      bana: j.created_at, file: j.filename || "",
    }));
    renderTable($("jobs-list"), rows, ["id", "kind", "file", "status", "progress", "bana"], {
      actionLabel: "Open",
      onAction: (row) => { openTab("csv"); openJob(row.id); },
    });
  } catch (e) {
    $("jobs-list").textContent = e.message;
  }
}

// ---------- dashboard ----------
const esc = (v) => String(v == null ? "" : v).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const PVH = (p) => p === "hunter" ? '<span class="pv hu"><svg viewBox="0 0 24 24"><use href="#i-hu"/></svg></span>'
  : p === "apollo" ? '<span class="pv ap"><svg viewBox="0 0 24 24"><use href="#i-ap"/></svg></span>'
  : p === "prospeo" ? '<span class="pv pr">P</span>' : "";
const cap = (p) => p.charAt(0).toUpperCase() + p.slice(1);
const fmtDay = (d) => d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const niceMax = (m) => [5, 10, 20, 40, 80, 100, 200, 400, 800, 1000, 2000, 5000].find((x) => x >= m) || Math.ceil(m / 1000) * 1000;
const smooth = (pts) => pts.reduce((o, p, i) => i ? o + `C${(pts[i - 1][0] + p[0]) / 2},${pts[i - 1][1]} ${(pts[i - 1][0] + p[0]) / 2},${p[1]} ${p}` : `M${p}`, "");
const grid = (w, h, max, L) => [0, 1, 2, 3, 4].map((i) => `<path d="M${L} ${10 + i * (h / 4)}H${w}" stroke="#1a2250"/><text x="2" y="${14 + i * (h / 4)}" fill="#8e99c6" font-size="11">${Math.round(max - i * max / 4)}</text>`).join("");
const dayLabels = (days, w, L, y) => [0, 1, 2, 3, 4, 5, 6].map((i) => {
  const d = days[Math.round(i * (days.length - 1) / 6)]; if (!d) return "";
  return `<text x="${L + i * (w - L - 30) / 6}" y="${y}" fill="#8e99c6" font-size="11">${MON[+d.slice(5, 7) - 1]} ${d.slice(8)}</text>`; }).join("");
const rangeDays = () => { const n = +$("rng").value || 30, out = []; for (let i = n - 1; i >= 0; i--) { const d = new Date(); d.setDate(d.getDate() - i); out.push(fmtDay(d)); } return out; };
let dashCache = null;

function drawQuota(rows) {
  const max = niceMax(Math.max(1, ...rows.map((q) => q.monthly_limit))), H = 136, bw = 90;
  $("chart-quota").innerHTML = `<svg viewBox="0 0 520 190">${grid(400, H, max, 40)}${rows.map((q, i) => {
    const x = 64 + i * 112, th = q.monthly_limit / max * H, uh = Math.min(q.used, q.monthly_limit) / max * H;
    return `<rect x="${x}" y="${10 + H - th}" width="${bw}" height="${th}" fill="#2a2f78"/><rect x="${x}" y="${10 + H - uh}" width="${bw}" height="${uh}" fill="#7c5cff"/><text x="${x + 20}" y="178" fill="#cfd5f5" font-size="12">${cap(q.provider)}</text>`; }).join("")}
    <g font-size="12" fill="#cfd5f5"><rect x="420" y="40" width="11" height="11" fill="#2a2f78"/><text x="438" y="50">Remaining</text><rect x="420" y="62" width="11" height="11" fill="#7c5cff"/><text x="438" y="72">Used</text></g></svg>`;
}
function drawStatus(stats) {
  const days = rangeDays(), by = {};
  stats.daily.forEach((r) => { (by[r.found_at] = by[r.found_at] || {})[r.status] = r.c; });
  const get = (f) => days.map((d) => f(by[d] || {}));
  const S = [["#8b5cf6", get((o) => (o.valid || 0) + (o.risky || 0) + (o.not_found || 0))], ["#22c55e", get((o) => o.valid || 0)], ["#ef4444", get((o) => o.not_found || 0)]];
  const max = niceMax(Math.max(1, ...S[0][1])), H = 136, W = 530, n = days.length;
  const path = (v) => smooth(v.map((x, i) => [30 + (n > 1 ? i * W / (n - 1) : 0), 10 + H - x / max * H]));
  $("chart-status").innerHTML = `<svg viewBox="0 0 560 190">${grid(560, H, max, 30)}${dayLabels(days, 560, 30, 186)}<g fill="none" stroke-width="2">${S.map(([c, v]) => `<path d="${path(v)}" stroke="${c}"/>`).join("")}</g></svg>`;
}
function drawDaily(stats) {
  const days = rangeDays(), tot = {};
  stats.daily.forEach((r) => { tot[r.found_at] = (tot[r.found_at] || 0) + r.c; });
  const v = days.map((d) => tot[d] || 0), max = niceMax(Math.max(1, ...v)), H = 112, n = days.length, step = 480 / n, bw = Math.max(2, step * 0.6);
  $("chart-daily").innerHTML = `<svg viewBox="0 0 520 150">${grid(520, H, max, 30)}${v.map((x, i) => `<rect x="${38 + i * step}" y="${10 + H - x / max * H}" width="${bw}" height="${x / max * H}" rx="2" fill="#6d4df5"/>`).join("")}${dayLabels(days, 520, 34, 146)}</svg>`;
}
function renderQuotaTable(quota) {
  const rows = quota.filter((q) => PROVIDERS.includes(q.provider));
  $("quota-table").innerHTML = `<div class="tw"><table><tr><th>Provider</th><th>Used</th><th>Monthly Limit</th><th>Remaining</th><th>Usage %</th><th>Resets On</th><th>Status</th></tr>${rows.map((q) => {
    const pct = q.monthly_limit ? Math.round(q.used / q.monthly_limit * 100) : 0, col = q.exhausted ? "#ef4444" : q.low ? "#f59e0b" : "#22c55e";
    const st = q.exhausted ? ["fl", "Exhausted"] : q.low ? ["p", "Low"] : ["a", "Active"];
    return `<tr><td><div class="nmc">${PVH(q.provider)}${cap(q.provider)}</div></td><td>${q.used}</td><td>${q.monthly_limit}</td><td>${q.remaining}</td><td><div style="display:flex;gap:12px;align-items:center"><b>${pct}%</b><div class="bar" style="width:160px;margin:0"><i style="width:${Math.min(100, pct)}%;background:${col}"></i></div></div></td><td>${esc(q.resets_on)}</td><td><span class="st ${st[0]}">● ${st[1]}</span></td></tr>`; }).join("")}</table></div>`;
}
function drawDashboard() {
  if (!dashCache) return;
  document.querySelectorAll(".rl").forEach((e) => { e.textContent = $("rng").value; });
  const { quota, stats } = dashCache, q3 = quota.filter((q) => PROVIDERS.includes(q.provider));
  drawQuota(q3); drawStatus(stats); drawDaily(stats); paintStats(quota, stats);
}
$("rng").addEventListener("change", drawDashboard);

async function loadDashboard() {
  try {
    const [quota, stats] = await Promise.all([api("/api/quota"), api("/api/stats")]);
    dashCache = { quota, stats };
    renderQuotaTable(quota);
    drawDashboard();
    loadHistory();
  } catch (e) {
    $("quota-table").textContent = e.message;
  }
}

// ---------- history ----------
function histQuery() {
  const params = new URLSearchParams();
  if ($("hist-q").value.trim()) params.set("q", $("hist-q").value.trim());
  if ($("hist-status").value) params.set("status", $("hist-status").value);
  if ($("hist-pipeline").value) params.set("pipeline", $("hist-pipeline").value);
  return params.toString();
}

const PIPELINE_OPTIONS = ["", "contacted", "replied", "interested", "closed"];

async function saveMeta(row) {
  try {
    await postJson("/api/leads/meta", {
      first_name: row.first_name, last_name: row.last_name, domain: row.domain,
      pipeline: row.pipeline || "", tags: row.tags || "", notes: row.notes || "",
    });
    setMsg($("hist-msg"), "Settings saved.", "ok");
  } catch (e) {
    setMsg($("hist-msg"), e.message, "err");
  }
}

function metaCell(row, field, td, kind) {
  let el;
  if (kind === "select") {
    el = document.createElement("select");
    PIPELINE_OPTIONS.forEach((v) => {
      const opt = document.createElement("option");
      opt.value = v;
      opt.textContent = v === "" ? "new" : v;
      el.appendChild(opt);
    });
    el.value = row[field] || "";
  } else {
    el = document.createElement("input");
    el.value = row[field] || "";
    el.placeholder = field;
    el.size = field === "notes" ? 20 : 10;
  }
  el.addEventListener("change", () => {
    row[field] = el.value;
    saveMeta(row);
  });
  td.appendChild(el);
}

let histSeq = 0;

async function loadHistory() {
  const seq = ++histSeq;
  try {
    const rows = await api("/api/history" + (histQuery() ? "?" + histQuery() : ""));
    if (seq !== histSeq) return;  // purana response naye filter ko overwrite na kare
    renderTable($("history-table"), rows,
      ["first_name", "last_name", "domain", "email", "status", "pipeline", "tags", "notes", "source", "found_at"], {
        badgeCol: "status",
        customCells: {
          pipeline: (row, td) => metaCell(row, "pipeline", td, "select"),
          tags: (row, td) => metaCell(row, "tags", td, "input"),
          notes: (row, td) => metaCell(row, "notes", td, "input"),
        },
        actionLabel: "🔁 Re-verify",
        onAction: async (row, btn) => {
          if (!row.email) {
            setMsg($("hist-msg"), "Is lead ke paas email nahi hai — pehle find karo.", "err");
            return;
          }
          btn.disabled = true;
          btn.textContent = "...";
          try {
            const r = await postJson("/api/reverify", row);
            setMsg($("hist-msg"), `${row.first_name} ${row.last_name}: ${r.log}`, r.status === "valid" ? "ok" : "warn");
            loadHistory();
          } catch (e) {
            setMsg($("hist-msg"), e.message, "err");
            btn.disabled = false;
            btn.textContent = "🔁 Re-verify";
          }
        },
      });
  } catch (e) {
    if (seq === histSeq) $("history-table").textContent = e.message;
  }
}

let histDebounce = null;
$("hist-q").addEventListener("input", () => {
  clearTimeout(histDebounce);
  histDebounce = setTimeout(loadHistory, 300);
});
$("hist-status").addEventListener("change", loadHistory);
$("hist-pipeline").addEventListener("change", loadHistory);

$("hist-export").addEventListener("click", () => {
  window.location.href = "/api/history/export" + (histQuery() ? "?" + histQuery() : "");
});

$("hist-reverify").addEventListener("click", async () => {
  try {
    const r = await postJson("/api/reverify-bulk", { status: "risky" });
    setMsg($("hist-msg"), `Re-verify job shuru: ${r.total} risky leads. CSV tab mein dekho.`, "ok");
    openTab("csv");
    openJob(r.job_id);
    loadJobsList();
  } catch (e) {
    setMsg($("hist-msg"), e.message, "err");
  }
});

$("hist-push").addEventListener("click", async () => {
  try {
    const r = await postJson("/api/push", {});
    setMsg($("hist-msg"), `Push ho gaya: ${r.pushed}/${r.total} leads` + (r.failed ? ` (${r.failed} fail)` : ""), r.failed ? "warn" : "ok");
  } catch (e) {
    setMsg($("hist-msg"), e.message, "err");
  }
});

// ---------- automation: schedules, outreach, tokens ----------
async function loadAutomation() {
  loadSchedules();
  loadTokens();
  try {
    const s = await api("/api/settings");
    $("oc-provider").value = s.outreach.provider;
    $("oc-campaign").value = s.outreach.campaign;
    $("oc-key").value = "";
    $("oc-key").placeholder = s.outreach.key_set ? "•••••• saved" : "Enter API key";
    $("oc-enabled").classList.toggle("off", !s.outreach.enabled);
    markPlat();
  } catch (e) { /* settings msg sidebar mein dikhega */ }
}

async function loadSchedules() {
  try {
    const rows = await api("/api/schedules");
    renderTable($("sch-list"), rows,
      ["name", "csv_url", "interval_hours", "sheet_id", "enabled", "last_run"], {
        customCells: {
          enabled: (row, td) => {
            const cb = document.createElement("input");
            cb.type = "checkbox";
            cb.checked = !!row.enabled;
            cb.addEventListener("change", async () => {
              await postJson("/api/schedules/" + row.id, { enabled: cb.checked });
              loadSchedules();
            });
            td.appendChild(cb);
          },
        },
        actions: [
          { label: "▶️ Run now", onClick: async (row) => {
            try {
              const r = await postJson(`/api/schedules/${row.id}/run`, {});
              setMsg($("sch-msg"), "Job shuru ho gaya — CSV tab mein dekho.", "ok");
              openTab("csv");
              openJob(r.job_id);
            } catch (e) { setMsg($("sch-msg"), e.message, "err"); }
          } },
          { label: "🗑️", onClick: async (row) => {
            await api("/api/schedules/" + row.id, { method: "DELETE" });
            loadSchedules();
          } },
        ],
      });
  } catch (e) {
    $("sch-list").textContent = e.message;
  }
}

$("sch-add").addEventListener("click", async () => {
  try {
    await postJson("/api/schedules", {
      name: $("sch-name").value,
      csv_url: $("sch-url").value.trim(),
      interval_hours: $("sch-interval").value,
      sheet_id: $("sch-sheet").value.trim(),
    });
    setMsg($("sch-msg"), "Schedule add ho gaya ✅", "ok");
    $("sch-name").value = $("sch-url").value = $("sch-sheet").value = "";
    loadSchedules();
  } catch (e) {
    setMsg($("sch-msg"), e.message, "err");
  }
});

$("oc-save").addEventListener("click", async () => {
  const payload = { outreach: { provider: $("oc-provider").value, campaign: $("oc-campaign").value.trim(), enabled: !$("oc-enabled").classList.contains("off") } };
  if ($("oc-key").value.trim()) payload.outreach.api_key = $("oc-key").value.trim();
  try {
    await postJson("/api/settings", payload);
    setMsg($("oc-msg"), "Outreach settings save ✅", "ok");
    loadAutomation();
  } catch (e) {
    setMsg($("oc-msg"), e.message, "err");
  }
});

$("oc-push").addEventListener("click", async () => {
  try {
    const r = await postJson("/api/outreach/push", {});
    setMsg($("oc-msg"), r.info, "ok");
  } catch (e) {
    setMsg($("oc-msg"), e.message, "err");
  }
});

async function loadTokens() {
  try {
    const rows = await api("/api/tokens");
    renderTable($("tok-list"), rows, ["name", "created_at", "last_used"], {
      actionLabel: "🗑️ Revoke",
      onAction: async (row) => {
        await api("/api/tokens/" + row.id, { method: "DELETE" });
        loadTokens();
      },
    });
  } catch (e) {
    $("tok-list").textContent = e.message;
  }
}

$("tok-create").addEventListener("click", async () => {
  try {
    const r = await postJson("/api/tokens", { name: $("tok-name").value.trim() });
    $("tok-show").textContent = r.token;
    $("tok-show").classList.remove("hidden");
    setMsg($("tok-msg"), "Token copy kar lo — dobara nahi dikhega!", "warn");
    $("tok-name").value = "";
    loadTokens();
  } catch (e) {
    setMsg($("tok-msg"), e.message, "err");
  }
});

loadSettings().catch((e) => setMsg($("settings-msg"), e.message, "err"));
loadAlerts();

// ---------- new design helpers ----------
const LIM_COLORS = { hunter: "#2f6bff", prospeo: "#a855f7", apollo: "#22c55e" };
function paintQuota(quota) {
  let days = null;
  quota.forEach((q) => {
    document.querySelectorAll(`[data-su="${q.provider}"]`).forEach((e) => { e.textContent = `${q.used} / ${q.monthly_limit}`; });
    document.querySelectorAll(`[data-sb="${q.provider}"]`).forEach((e) => { e.style.width = Math.min(100, q.monthly_limit ? q.used / q.monthly_limit * 100 : 0) + "%"; });
    if (q.resets_on) { const d = Math.max(0, Math.ceil((new Date(q.resets_on) - new Date()) / 864e5)); days = days == null ? d : Math.min(days, d); }
  });
  if (days != null) document.querySelectorAll("[data-reset]").forEach((e) => { e.textContent = days; });
}
function paintSettings(s) {
  let active = 0;
  PROVIDERS.forEach((p) => {
    const on = !!s.keys[p.toUpperCase() + "_API_KEY"];
    if (on) active++;
    $("rk-" + p).innerHTML = `<i class="dt${on ? "" : " off"}"></i>${on ? "Configured" : "Not set"}`;
    $("cs-" + p).innerHTML = `<i class="dt${on ? "" : " off"}"></i>${on ? "Connected" : "Not set"}`;
    $("rl-" + p).textContent = `${s.limits[p]} / month`;
  });
  PROVIDERS.forEach((p) => { const on = !!s.keys[p.toUpperCase() + "_API_KEY"]; document.querySelectorAll(`[data-sp="${p}"]`).forEach((e) => e.classList.toggle("off", !on)); });
  $("a-toggle").classList.toggle("off", !s.flags.push_valid);
  $("a-valid").checked = s.flags.push_only_valid;
  $("a-webhook").value = "";
  $("a-webhook").placeholder = s.webhook_set ? "•••••• (set)" : "https://hooks.zapier.com/...";
  $("src-active").textContent = `${active} source${active === 1 ? "" : "s"} active`;
  $("st-src").textContent = active;
}
function paintStats(quota, stats) {
  const sc = stats.status_counts || {}, pc = stats.pipeline_counts || {};
  const total = Object.values(sc).reduce((a, b) => a + b, 0), valid = sc.valid || 0;
  const rows = quota.filter((q) => PROVIDERS.includes(q.provider));
  const used = rows.reduce((a, q) => a + q.used, 0), lim = rows.reduce((a, q) => a + q.monthly_limit, 0);
  $("st-total").textContent = total; $("st-valid").textContent = valid;
  $("st-rate").textContent = `${total ? Math.round((valid / total) * 100) : 0}% success rate`;
  $("st-used").textContent = used; $("st-lim").textContent = lim;
  $("st-bar").style.width = (lim ? Math.min(100, (used / lim) * 100) : 0) + "%";
  $("pp-found").textContent = total; $("pp-valid").textContent = valid;
  $("pp-sent").textContent = ["contacted", "replied", "interested", "closed"].reduce((a, k) => a + (pc[k] || 0), 0);
  $("pp-replied").textContent = pc.replied || 0;
}
const dz = $("dropzone");
["dragover", "dragenter"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
dz.addEventListener("drop", (e) => {
  if (!e.dataTransfer.files.length) return;
  $("csv-file").files = e.dataTransfer.files;
  $("csv-file").dispatchEvent(new Event("change"));
});
document.querySelector(".nav-leads").addEventListener("click", () => {
  document.querySelector('nav.m [data-tab="dash"]').click();
  setTimeout(() => $("history-table").scrollIntoView({ behavior: "smooth" }), 200);
});

// ---------- recent leads (Find Lead screen) ----------
let recentRows = [], recentTimer = null;
const AV = ["#a855f7", "#ec4899", "#3b82f6", "#22c55e", "#f59e0b", "#06b6d4"];
function rel(ts) {
  if (!ts) return "";
  if (ts.length <= 10) return ts;
  const m = Math.max(0, Math.round((Date.now() - new Date(ts)) / 60000));
  if (m < 1) return "just now"; if (m < 60) return `${m} min ago`;
  if (m < 1440) return `${Math.floor(m / 60)} hour${m >= 120 ? "s" : ""} ago`;
  return ts.slice(0, 10);
}
async function loadRecent() {
  try {
    recentRows = (await api("/api/history?q=" + encodeURIComponent($("rl-q").value.trim()))).filter((r) => r.email || r.status);
    const cur = $("rl-src").value, srcs = [...new Set(recentRows.map((r) => r.source).filter(Boolean))];
    $("rl-src").innerHTML = '<option value="">All Sources</option>' + srcs.map((x) => `<option value="${esc(x)}">${esc(cap(x))}</option>`).join("");
    $("rl-src").value = srcs.includes(cur) ? cur : "";
    renderRecent();
  } catch (e) { $("rl-table").innerHTML = `<div class="rl-empty">${esc(e.message)}</div>`; }
}
function renderRecent() {
  const src = $("rl-src").value, rows = recentRows.filter((r) => !src || r.source === src).slice(0, 8);
  if (!rows.length) { $("rl-table").innerHTML = '<div class="rl-empty">No leads yet — find your first email above.</div>'; return; }
  const ST = { valid: ["v", "Verified"], risky: ["p", "Pending"], not_found: ["fl", "Not found"] };
  $("rl-table").innerHTML = `<table><tr><th><input type="checkbox" id="rl-all" aria-label="Select all"></th><th>Name</th><th>Company</th><th>Email</th><th>Source</th><th>Status</th><th>Added</th></tr>${rows.map((r, i) => {
    const nm = `${r.first_name} ${r.last_name}`.trim(), ini = ((r.first_name[0] || "") + (r.last_name[0] || "")).toUpperCase(), st = ST[r.status] || ["fl", r.status];
    const comp = r.domain.split(".")[0];
    return `<tr><td><input type="checkbox" class="rl-ck" data-i="${i}"></td><td><div class="nmc"><span class="ini" style="background:${AV[(nm.length + i) % AV.length]}">${esc(ini)}</span>${esc(nm)}</div></td><td><div class="co"><span class="cob" style="background:${AV[(comp.length * 3) % AV.length]}">${esc(comp[0] || "?").toUpperCase()}</span>${esc(cap(comp))}</div></td><td>${esc(r.email) || "—"} ${r.email ? `<button type="button" class="cp-b" data-copy="${esc(r.email)}" aria-label="Copy email"><svg class="i" viewBox="0 0 24 24"><use href="#i-copy"/></svg></button>` : ""}</td><td>${r.source ? `<span class="src">${PVH(r.source) || ""}${esc(cap(r.source))}</span>` : ""}</td><td><span class="st ${st[0]}">● ${st[1]}</span></td><td>${esc(rel(r.found_at))}</td></tr>`; }).join("")}</table>`;
  $("rl-table").dataset.shown = JSON.stringify(rows.map((r) => recentRows.indexOf(r)));
}
$("rl-table").addEventListener("click", (e) => {
  const b = e.target.closest("[data-copy]");
  if (b) { navigator.clipboard && navigator.clipboard.writeText(b.dataset.copy); b.style.color = "#4ade80"; setTimeout(() => { b.style.color = ""; }, 900); }
  if (e.target.id === "rl-all") document.querySelectorAll(".rl-ck").forEach((c) => { c.checked = e.target.checked; });
});
$("rl-q").addEventListener("input", () => { clearTimeout(recentTimer); recentTimer = setTimeout(loadRecent, 250); });
$("rl-src").addEventListener("change", renderRecent);
$("rl-export").addEventListener("click", () => {
  const shown = JSON.parse($("rl-table").dataset.shown || "[]"), picked = [...document.querySelectorAll(".rl-ck:checked")].map((c) => recentRows[shown[+c.dataset.i]]);
  if (!picked.length) { location.href = "/api/history/export?q=" + encodeURIComponent($("rl-q").value.trim()); return; }
  const f = ["first_name", "last_name", "domain", "email", "status", "source", "found_at"];
  const csv = [f.join(",")].concat(picked.map((r) => f.map((k) => `"${String(r[k] ?? "").replace(/"/g, '""')}"`).join(","))).join("\n");
  const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" })); a.download = "selected_leads.csv"; a.click();
});

// ---------- automation card (Find Lead screen) ----------
async function saveFlag(flags, msgEl) {
  try { await postJson("/api/settings", { flags }); await loadSettings(); } catch (e) { setMsg(msgEl, e.message, "err"); }
}
$("a-toggle").addEventListener("click", async () => {
  const on = $("a-toggle").classList.contains("off");
  await saveFlag({ push_valid: on }, $("a-msg"));
  setMsg($("a-msg"), on ? "Auto-push is on." : "Auto-push is off.", "ok");
});
$("a-valid").addEventListener("change", () => saveFlag({ push_only_valid: $("a-valid").checked }, $("a-msg")));
async function saveWebhook() {
  const v = $("a-webhook").value.trim();
  if (v) { await postJson("/api/settings", { webhook_url: v }); await loadSettings(); }
}
$("a-webhook").addEventListener("change", () => saveWebhook().then(() => setMsg($("a-msg"), "Webhook saved ✅", "ok")).catch((e) => setMsg($("a-msg"), e.message, "err")));
$("a-test").addEventListener("click", async () => {
  setMsg($("a-msg"), "Sending test...", "warn");
  try { await saveWebhook(); const r = await postJson("/api/webhook/test"); setMsg($("a-msg"), `Webhook responded: ${r.info} ✅`, "ok"); }
  catch (e) { setMsg($("a-msg"), e.message.includes("HTTP") || e.message ? e.message : "Webhook test failed", "err"); }
});

// ---------- outreach toggle + platform picker ----------
$("oc-enabled").addEventListener("click", async () => {
  const on = $("oc-enabled").classList.contains("off");
  try { await postJson("/api/settings", { outreach: { enabled: on } }); $("oc-enabled").classList.toggle("off", !on); setMsg($("oc-msg"), on ? "Outreach enabled." : "Outreach disabled.", "ok"); }
  catch (e) { setMsg($("oc-msg"), e.message, "err"); }
});
function markPlat() { document.querySelectorAll(".plat").forEach((p) => p.classList.toggle("sel", p.dataset.prov === $("oc-provider").value)); }
document.querySelectorAll(".plat").forEach((p) => {
  const go = () => { if (p.dataset.prov === "api") { $("tok-name").scrollIntoView({ behavior: "smooth", block: "center" }); $("tok-name").focus(); } else { $("oc-provider").value = p.dataset.prov; markPlat(); } };
  p.addEventListener("click", go); p.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
});
$("oc-provider").addEventListener("change", markPlat);

// ---------- tutorial ----------
const TUT = [
  ["search", "Find a single email", "Enter a first name, last name and company domain (or a LinkedIn URL) and press Find Email. LeadFinder tries free pattern guesses first, then Hunter, Prospeo and Apollo, and verifies the result.", "single"],
  ["file", "Bulk search with a CSV", "Upload a CSV with first_name, last_name and domain. The job runs on the server in the background, so you can close the tab and come back later.", "csv"],
  ["dash", "Track credits and results", "The Dashboard shows credits used per provider, how many leads were found, verified or failed, and lets you search, export and re-verify your leads.", "dash"],
  ["bolt", "Automate it", "Import from a Google Sheet on a schedule, push valid leads to a webhook, Instantly, Smartlead or Lemlist, and create API tokens for your own scripts.", "auto"],
  ["key", "Add your API keys first", "Open API Keys in the sidebar and add your Reoon key (required for verification) plus at least one finder key, or turn on the free pattern-guess fallback.", null],
];
let tutI = 0;
function showTut() {
  const [ic, t, p, tab] = TUT[tutI];
  $("tut-body").innerHTML = `<div class="tsi"><svg class="i" viewBox="0 0 24 24"><use href="#i-${ic}"/></svg></div><h3>${tutI + 1}. ${esc(t)}</h3><p>${esc(p)}</p>`;
  $("tut-dots").innerHTML = TUT.map((_, i) => `<i class="${i === tutI ? "on" : ""}"></i>`).join("");
  $("tut-prev").style.visibility = tutI ? "visible" : "hidden";
  $("tut-next").textContent = tutI === TUT.length - 1 ? "Done" : "Next";
  $("tut").dataset.tab = tab || "";
}
document.querySelectorAll(".tut").forEach((b) => b.addEventListener("click", () => { tutI = 0; showTut(); $("tut").classList.remove("hidden"); $("tut-next").focus(); }));
$("tut-x").addEventListener("click", () => $("tut").classList.add("hidden"));
$("tut-prev").addEventListener("click", () => { tutI--; showTut(); });
$("tut-next").addEventListener("click", () => {
  if (tutI < TUT.length - 1) { tutI++; showTut(); return; }
  $("tut").classList.add("hidden");
  if (!TUT[tutI][3]) openTab("keys");
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("tut").classList.add("hidden"); });
$("gsearch").addEventListener("keydown", (e) => { if (e.key === "Enter" && $("gsearch").value.trim()) { $("rl-q").value = $("gsearch").value.trim(); document.querySelector('nav.m [data-tab="single"]').click(); } });
document.addEventListener("keydown", (e) => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); $("gsearch").focus(); } });
loadRecent();


// ---------- menus, notifications, sign out ----------
const LS_READ = "lf_notif_read";
let notifs = [], lfAuth = false;
const readSet = () => { try { return new Set(JSON.parse(localStorage.getItem(LS_READ) || "[]")); } catch (e) { return new Set(); } };
const saveRead = (s) => { try { localStorage.setItem(LS_READ, JSON.stringify([...s].slice(-200))); } catch (e) { /* storage blocked */ } };
function closeMenus() {
  ["notif", "umenu"].forEach((id) => { $(id).hidden = true; });
  $("bell").setAttribute("aria-expanded", "false"); $("usr").setAttribute("aria-expanded", "false");
}
function toggleMenu(id, btn) {
  const open = $(id).hidden;
  closeMenus();
  $(id).hidden = !open;
  btn.setAttribute("aria-expanded", String(open));
  if (open && id === "notif") renderNotifs();
}
function paintBell() {
  const unread = notifs.filter((n) => !readSet().has(n.id)).length;
  $("bell-n").textContent = unread > 9 ? "9+" : unread;
  $("bell-n").classList.toggle("hidden", !unread);
  $("bell").setAttribute("aria-label", unread ? `Notifications, ${unread} unread` : "Notifications");
}
function renderNotifs() {
  const rd = readSet(), box = $("notif-list");
  if (!notifs.length) { box.innerHTML = '<div class="nz">You’re all caught up.</div>'; return; }
  box.innerHTML = notifs.map((n, i) => `<button type="button" class="ni ${rd.has(n.id) ? "" : "u"}" data-i="${i}"><span class="lv ${n.lvl}"></span><span><b>${esc(n.t)}</b><span class="d">${esc(n.d)}</span></span></button>`).join("");
  box.querySelectorAll(".ni").forEach((b) => b.addEventListener("click", () => {
    const n = notifs[+b.dataset.i], s = readSet(); s.add(n.id); saveRead(s); paintBell(); closeMenus();
    if (n.go === "settings") openTab("keys");
    if (n.go === "job") { openTab("csv"); openJob(n.job); }
  }));
}
function paintUserMenu() { $("so").hidden = !lfAuth; $("so-note").hidden = lfAuth; }
async function refreshNotifs() {
  const [s, q, jobs] = (await Promise.allSettled([api("/api/settings"), api("/api/quota"), api("/api/jobs")])).map((r) => (r.status === "fulfilled" ? r.value : null));
  const list = [];
  if (s) {
    lfAuth = !!s.auth; paintUserMenu();
    if (!s.auth) list.push({ id: "auth-off", lvl: "warn", t: "Sign-in is turned off", d: "Anyone with this link can use the app. Set APP_PASSWORD on your server to require a password." });
    if (!s.keys.REOON_API_KEY) list.push({ id: "no-reoon", lvl: "warn", t: "Add your Reoon key", d: "Emails can’t be verified until it’s set.", go: "settings" });
    if (!PROVIDERS.some((p) => s.keys[p.toUpperCase() + "_API_KEY"])) list.push({ id: "no-source", lvl: "ok", t: "No data source connected", d: "Add a Hunter, Prospeo or Apollo key to widen your search.", go: "settings" });
  }
  (q || []).forEach((x) => {
    if (x.exhausted) list.push({ id: `q-${x.provider}-out-${x.resets_on}`, lvl: "err", t: `${cap(x.provider)} credits used up`, d: `${x.used}/${x.monthly_limit} used. Resets ${x.resets_on}.`, go: "settings" });
    else if (x.low) list.push({ id: `q-${x.provider}-low-${x.resets_on}`, lvl: "warn", t: `${cap(x.provider)} credits running low`, d: `${x.remaining} left of ${x.monthly_limit}.`, go: "settings" });
  });
  (jobs || []).slice(0, 5).forEach((x) => {
    const name = x.filename || x.id;
    if (x.status === "done") list.push({ id: `j-${x.id}-done`, lvl: "ok", t: "CSV job finished", d: `${name}: ${x.done}/${x.total} rows processed.`, go: "job", job: x.id });
    else if (x.status === "paused" || x.status === "error") list.push({ id: `j-${x.id}-${x.status}`, lvl: "err", t: x.status === "paused" ? "CSV job paused" : "CSV job failed", d: `${name}${x.error ? ": " + x.error : ""}`, go: "job", job: x.id });
  });
  notifs = list; paintBell();
  if (!$("notif").hidden) renderNotifs();
}
$("bell").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu("notif", $("bell")); });
$("usr").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu("umenu", $("usr")); });
document.addEventListener("click", (e) => { if (!e.target.closest(".menu")) closeMenus(); });
$("notif-all").addEventListener("click", () => { const s = readSet(); notifs.forEach((n) => s.add(n.id)); saveRead(s); paintBell(); renderNotifs(); });
$("u-set").addEventListener("click", () => { closeMenus(); openTab("keys"); });
$("so").addEventListener("click", async () => { try { await fetch("/api/logout", { method: "POST" }); } finally { location.href = "/login"; } });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenus(); });
