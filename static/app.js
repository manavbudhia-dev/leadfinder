const $ = (id) => document.getElementById(id);

const STATUS_LABEL = {
  valid: "✅ Sahi email",
  risky: "⚠️ Risky (check karo)",
  not_found: "❌ Nahi mila",
};
const STATUS_COLORS = { valid: "#16a34a", risky: "#f59e0b", not_found: "#dc2626" };
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
    container.textContent = "Abhi kuch nahi hai.";
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
    input.placeholder = s.keys[env] ? "•••••• (set hai, badalna ho to hi bharo)" : "khali hai";
  });
  PROVIDERS.forEach((p) => { $("lim-" + p).value = s.limits[p]; });
  $("flag-pattern").checked = s.flags.pattern_guess;
  $("flag-push").checked = s.flags.push_valid;
  $("webhook-url").value = "";
  $("webhook-url").placeholder = s.webhook_set ? "•••••• (set hai)" : "https://hooks.zapier.com/...";
  $("logout-btn").classList.toggle("hidden", !s.auth);
}

$("save-settings").addEventListener("click", async () => {
  const keys = {};
  KEY_ENVS.forEach((env) => {
    const v = $("key-" + env).value.trim();
    if (v || $("key-" + env).placeholder === "khali hai") keys[env] = v;
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
    setMsg($("settings-msg"), "Save ho gaya ✅", "ok");
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
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".panel").forEach((p) =>
      p.classList.toggle("active", p.id === "tab-" + btn.dataset.tab));
    if (btn.dataset.tab === "dash") loadDashboard();
    if (btn.dataset.tab === "csv") loadJobsList();
    if (btn.dataset.tab === "auto") loadAutomation();
  });
});

function openTab(name) {
  document.querySelector(`[data-tab=${name}]`).click();
}

// ---------- quota alerts ----------
async function loadAlerts() {
  try {
    const quota = await api("/api/quota");
    const box = $("quota-alerts");
    box.innerHTML = "";
    quota.forEach((q) => {
      if (!q.exhausted && !q.low) return;
      const div = document.createElement("div");
      div.className = "alert " + (q.exhausted ? "err" : "warn");
      div.textContent = q.exhausted
        ? `⛔ ${q.provider}: credits khatam (${q.used}/${q.monthly_limit}). Reset: ${q.resets_on}`
        : `⚠️ ${q.provider}: credits kam hain — sirf ${q.remaining} bache (${q.used}/${q.monthly_limit})`;
      box.appendChild(div);
    });
  } catch (e) { /* alerts best-effort hain */ }
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
      setMsg($("csv-msg"), "CSV khali hai.", "err");
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
const PLOT_LAYOUT = { height: 260, margin: { t: 40, b: 40, l: 45, r: 15 }, paper_bgcolor: "transparent" };
const PLOT_CONFIG = { responsive: true, displayModeBar: false };

async function loadDashboard() {
  try {
    const [quota, stats] = await Promise.all([api("/api/quota"), api("/api/stats")]);
    renderTable($("quota-table"), quota, ["provider", "used", "monthly_limit", "remaining", "resets_on"]);

    Plotly.newPlot("chart-quota", [
      { x: quota.map((q) => q.provider), y: quota.map((q) => q.used), name: "Used", type: "bar", marker: { color: "#4f6ef7" } },
      { x: quota.map((q) => q.provider), y: quota.map((q) => q.remaining), name: "Bacha", type: "bar", marker: { color: "#c7d2fe" } },
    ], { ...PLOT_LAYOUT, title: "Credits usage", barmode: "stack" }, PLOT_CONFIG);

    const sc = stats.status_counts;
    const labels = ["valid", "risky", "not_found"].filter((s) => sc[s]);
    Plotly.newPlot("chart-status", [{
      labels: labels.map((s) => STATUS_LABEL[s]),
      values: labels.map((s) => sc[s]),
      type: "pie", hole: 0.55,
      marker: { colors: labels.map((s) => STATUS_COLORS[s]) },
    }], { ...PLOT_LAYOUT, title: "Leads status" }, PLOT_CONFIG);

    const pc = stats.pipeline_counts || {};
    const pLabels = ["", "contacted", "replied", "interested", "closed"].filter((p) => pc[p]);
    Plotly.newPlot("chart-pipeline", [{
      x: pLabels.map((p) => p === "" ? "new" : p),
      y: pLabels.map((p) => pc[p]),
      type: "bar", marker: { color: "#8b5cf6" },
    }], { ...PLOT_LAYOUT, title: "CRM pipeline" }, PLOT_CONFIG);

    const days = [...new Set(stats.daily.map((d) => d.found_at))].sort().slice(-30);
    const series = ["valid", "risky", "not_found"].map((s) => ({
      x: days,
      y: days.map((d) => stats.daily.filter((r) => r.found_at === d && r.status === s)
        .reduce((a, r) => a + r.c, 0)),
      name: STATUS_LABEL[s], type: "bar", marker: { color: STATUS_COLORS[s] },
    }));
    Plotly.newPlot("chart-daily", series,
      { ...PLOT_LAYOUT, title: "Daily leads (last 30 din)", barmode: "stack" }, PLOT_CONFIG);

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
    setMsg($("hist-msg"), "Save ho gaya ✅", "ok");
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
    $("oc-key").placeholder = s.outreach.key_set ? "•••••• (set hai)" : "API key";
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
  const payload = { outreach: { provider: $("oc-provider").value, campaign: $("oc-campaign").value.trim() } };
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