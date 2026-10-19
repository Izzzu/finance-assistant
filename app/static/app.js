"use strict";
/* Local Accountant UI. No frameworks, no external requests.
   All data from statements is inserted as text nodes (never as HTML). */

// ------------------------------------------------------------------ helpers
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k in el && typeof v !== "string") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : String(c));
  }
  return el;
}

const nf = new Intl.NumberFormat("de-CH", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const money = (v) => (v == null ? "–" : nf.format(v));
const pct = (v) => (v == null ? "–" : `${Math.round(v)}%`);
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
const monthLabel = (ym) => `${MONTHS[+ym.slice(5, 7) - 1]} ${ym.slice(0, 4)}`;
const shortDate = (d) => `${d.slice(8, 10)}.${d.slice(5, 7)}`;

const store = {
  get(k, d) { try { const v = localStorage.getItem("la." + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("la." + k, JSON.stringify(v)); } catch { /* private mode */ } },
};

async function api(path, opts = {}) {
  const o = { method: opts.method || (opts.body !== undefined || opts.form ? "POST" : "GET"), headers: {} };
  if (o.method !== "GET") o.headers["X-Local-Accountant"] = "1";
  if (opts.form) o.body = opts.form;
  else if (opts.body !== undefined) { o.headers["Content-Type"] = "application/json"; o.body = JSON.stringify(opts.body); }
  const r = await fetch(path, o);
  const data = r.headers.get("content-type")?.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.detail) || data || r.statusText);
  return data;
}

// ------------------------------------------------------------------ state
const S = {
  view: "report",
  mode: store.get("mode", "month"),
  month: null, year: null, account: "",
  spread: store.get("spread", false),
  q: "",
  cats: [], status: null,
  expanded: new Set(),
  files: [],          // upload queue: {file, status, result}
  sub: "categories",  // settings sub-view
  openImport: null,   // scanned file whose rows are shown in Settings
};

// ------------------------------------------------------------------ toast
let toastTimer;
function toast(msg, undoToken) {
  const t = $("#toast");
  t.replaceChildren(h("span", {}, msg));
  if (undoToken) {
    t.append(h("button", {
      onclick: async () => { t.classList.add("hidden"); await api("/api/undo", { body: { undo: undoToken } }); await refresh(); toast("Undone"); },
    }, "Undo"));
  }
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), undoToken ? 9000 : 3500);
}

// ------------------------------------------------------------------ status & navigation
async function loadStatus() {
  S.status = await api("/api/status");
  S.cats = await api("/api/categories");
  const b = $("#reviewBadge");
  b.textContent = S.status.to_review;
  b.classList.toggle("hidden", !S.status.to_review);
  const ai = $("#aiStatus");
  ai.classList.toggle("ok", S.status.llm_ok);
  ai.classList.toggle("bad", !S.status.llm_ok);
  ai.querySelector("span").textContent = S.status.llm_ok ? "Local AI ready" : "Local AI not available";
  ai.title = S.status.llm_ok ? "Your statements are read and categorized on this computer" : (S.status.llm_message || "");
  const months = S.status.months;
  if (months.length && (!S.month || !months.includes(S.month))) S.month = months[months.length - 1];
  if (S.month && !S.year) S.year = S.month.slice(0, 4);
  const sel = $("#accountFilter");
  const cur = sel.value;
  sel.replaceChildren(h("option", { value: "" }, "All accounts"), ...S.status.accounts.map((a) => h("option", { value: a }, a)));
  sel.value = S.status.accounts.includes(cur) ? cur : "";
}

function show(view) {
  S.view = view;
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("hidden", v.id !== "view-" + view));
  if (view === "report") renderReport();
  if (view === "settings") renderSettings();
}

async function refresh() {
  await loadStatus();
  if (S.view === "report") await renderReport();
  if (S.view === "settings") await renderSettings();
}

// ------------------------------------------------------------------ category select + dialog
function catSelect(selectedId, { placeholder } = {}) {
  const groups = [
    ["Monthly spending", (c) => c.kind === "expense" && c.period === "monthly"],
    ["Yearly report", (c) => c.kind === "expense" && c.period === "yearly"],
    ["Money in", (c) => c.kind === "income"],
    ["Not spending", (c) => c.kind === "transfer"],
  ];
  const sel = h("select", { "aria-label": "Category" });
  if (placeholder || selectedId == null) sel.append(h("option", { value: "" }, placeholder || "Choose category…"));
  for (const [label, f] of groups) {
    const og = h("optgroup", { label });
    S.cats.filter(f).forEach((c) => og.append(h("option", { value: c.id }, c.name)));
    if (og.children.length) sel.append(og);
  }
  sel.append(h("option", { value: "__new" }, "+ New category…"));
  sel.value = selectedId == null ? "" : String(selectedId);
  sel.dataset.prev = sel.value;
  return sel;
}

/** Handles "+ New category…" in a select. Resolves to a category id or null. */
async function resolveCat(sel) {
  if (sel.value !== "__new") return sel.value ? +sel.value : null;
  const id = await catDialog();
  if (!id) { sel.value = sel.dataset.prev || ""; return null; }
  await loadStatus();
  const fresh = catSelect(id);
  sel.replaceChildren(...fresh.childNodes);
  sel.value = String(id);
  return id;
}

function catDialog(cat) {
  const dlg = $("#catDialog"), form = $("#catForm");
  $("#catDialogTitle").textContent = cat ? "Edit category" : "New category";
  form.name.value = cat ? cat.name : "";
  form.description.value = cat ? cat.description || "" : "";
  form.kind.value = cat ? cat.kind : "expense";
  form.period.value = cat ? cat.period : "monthly";
  dlg.showModal();
  return new Promise((resolve) => {
    dlg.addEventListener("close", async function onClose() {
      dlg.removeEventListener("close", onClose);
      if (dlg.returnValue !== "ok") return resolve(null);
      try {
        const r = await api("/api/categories", { body: {
          id: cat ? cat.id : null, name: form.name.value, description: form.description.value,
          kind: form.kind.value, period: form.period.value } });
        resolve(r.id);
      } catch (e) { toast(e.message); resolve(null); }
    });
  });
}

// ------------------------------------------------------------------ transaction rows
function sourceBadge(t) {
  if (t.status === "user") return h("span", { class: "src", title: "Set by you" }, "👤 you");
  if (t.status === "rule") return h("span", { class: "src", title: "Matched one of your remembered rules" }, "📏 rule");
  if (t.status === "llm") return h("span", { class: "src" + (t.confidence < 0.9 ? " low" : ""), title: "Categorized by the local AI" }, `🤖 ${(t.confidence ?? 0).toFixed(2)}`);
  return h("span", { class: "src low" }, "⚠ review");
}

function amountCell(t) {
  const v = t.amount_chf ?? t.amount;
  const title = t.share !== 1 && t.full_chf != null ? `Your part. Full amount ${money(t.full_chf)}`
    : t.currency !== (S.status?.currency || "CHF") ? `${money(t.amount)} ${t.currency} (${t.fx_source || "no rate"})` : null;
  return h("span", { class: "num " + (v > 0 ? "pos" : "neg"), title }, money(v),
    t.currency !== (S.status?.currency || "CHF") ? h("small", { class: "muted" }, " " + t.currency) : null,
    t.share !== 1 && t.share != null ? h("small", { class: "shared" }, t.share === 0.5 ? " ½" : ` ${Math.round(t.share * 100)}%`) : null);
}

function txnRow(t) {
  const row = h("div", { class: "txn" });
  const sel = catSelect(t.category_id, { placeholder: t.suggestion ? `Suggested: ${t.suggestion}` : null });
  sel.addEventListener("change", async () => {
    const id = await resolveCat(sel);
    if (id) rememberPrompt(row, t, id, sel);
  });
  row.append(
    h("span", { class: "muted" }, shortDate(t.date)),
    h("span", { class: "desc", title: t.description }, t.name ? h("strong", {}, t.name + " ") : null, h("small", {}, t.description), h("small", {}, " · " + t.account),
      t.edited ? h("small", { class: "edited", title: "You corrected this row" }, t.orig_amount != null ? ` · corrected (statement read ${money(t.orig_amount)})` : " · corrected") : null,
      t.share !== 1 ? h("small", { class: "shared" }, ` · ${t.share === 0.5 ? "split ½" : "your share " + Math.round(t.share * 100) + "%"} of ${money(t.full_chf)}`) : null),
    amountCell(t), sel, sourceBadge(t),
    h("span", { class: "row-actions" },
      h("button", { class: "icon-btn split-btn" + (t.share === 0.5 ? " on" : ""), title: t.share === 0.5 ? "Split half — click to count the full amount again" : "Split half (e.g. with your partner): only your half counts",
        "aria-label": "Split half", "aria-pressed": t.share === 0.5 ? "true" : "false", onclick: async () => {
          const next = t.share === 0.5 ? 1 : 0.5;
          await api(`/api/transactions/${t.id}/share`, { body: { share: next } });
          toast(next === 0.5 ? `Split ½ — your part is ${money((t.full_chf ?? t.amount) / 2)}` : "Counting the full amount again", null);
          await refresh();
        } }, "½"),
      h("button", { class: "icon-btn edit-btn", title: "Correct date, description or amount", "aria-label": "Edit transaction", onclick: () => editForm(row, t) }, "✎")));
  return row;
}

/** Inline form to correct a row the statement reader got wrong. */
function editForm(row, t) {
  const existing = row.querySelector(".edit-form");
  if (existing) return existing.remove();
  const date = h("input", { type: "date", value: t.date, "aria-label": "Date" });
  const desc = h("input", { type: "text", value: t.description, "aria-label": "Description" });
  const amount = h("input", { type: "number", step: "0.01", value: String(t.amount), "aria-label": "Amount" });
  const share = h("input", { type: "number", min: "1", max: "100", step: "1", value: String(Math.round((t.share ?? 1) * 100)), "aria-label": "Your share in percent" });
  const after = async (r, msg) => {
    const imp = r.import;
    const check = !imp ? "" : imp.balance_check === "ok" ? ` · ${imp.account}: ✅ balances match`
      : imp.balance_check === "diff" && imp.balance_diff != null ? ` · ${imp.account}: ⚠️ still differs by ${money(imp.balance_diff)}` : "";
    toast(msg + check);
    await refresh();
  };
  const form = h("div", { class: "edit-form" },
    h("label", {}, "Date", date),
    h("label", { class: "grow" }, "Description", desc),
    h("label", {}, `Amount (${t.currency}, minus = money out)`, amount),
    h("label", { class: "share-field" }, "Your share %", share),
    h("div", { class: "edit-actions" },
      h("button", { class: "primary small", onclick: async () => {
        const v = parseFloat(String(amount.value).replace(/['’\s]/g, "").replace(",", "."));
        if (!isFinite(v) || v === 0) return toast("Enter an amount, e.g. -1200.00");
        const p = parseFloat(share.value);
        if (!(p > 0 && p <= 100)) return toast("Your share must be between 1 and 100%");
        try {
          if (Math.round(p) !== Math.round((t.share ?? 1) * 100)) await api(`/api/transactions/${t.id}/share`, { body: { share: p / 100 } });
          await after(await api(`/api/transactions/${t.id}`, { method: "PUT", body: { date: date.value, description: desc.value, amount: v } }), "Saved");
        } catch (e) { toast(e.message); }
      } }, "Save"),
      h("button", { class: "ghost small", onclick: async () => {
        if (!confirm("Delete this row? Use this only for lines that aren't real transactions.")) return;
        try { await after(await api(`/api/transactions/${t.id}`, { method: "DELETE" }), "Row deleted"); } catch (e) { toast(e.message); }
      } }, "Delete row"),
      h("button", { class: "link small", onclick: () => form.remove() }, "Cancel")),
    h("div", { class: "muted small hint-row" }, "Correct what the statement reader got wrong; the file's balance check is re-run when you save. Your share: e.g. 50 for a cost split half, 60 for a 60/40 split — only your part counts in reports."));
  row.append(form);
  amount.focus();
}

async function rememberPrompt(row, t, catId, sel) {
  row.querySelector(".prompt")?.remove();
  const sim = await api("/api/similar", { body: { txn_id: t.id } });
  const input = h("input", { type: "text", value: sim.pattern, "aria-label": "Pattern to remember" });
  const lead = h("span", {});
  const setCount = (n) => lead.textContent = n > 0 ? `Also apply to ${n} other transaction(s) matching` : "Remember transactions matching";
  setCount(sim.count);
  let timer;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => setCount((await api("/api/similar/count", { body: { pattern: input.value } })).count), 300);
  });
  const done = async (remember) => {
    const r = await api("/api/assign", { body: { txn_ids: [t.id], category_id: catId, remember, pattern: input.value } });
    toast(r.not_remembered ? `Updated 1 transaction. “${input.value}” is too generic to remember — use the person's or shop's name.`
      : remember ? `Updated ${r.changed} transaction(s) and remembered “${input.value}”` : "Updated 1 transaction", r.undo);
    await refresh();
  };
  const prompt = h("div", { class: "prompt" },
    lead, input, h("span", {}, "for future scans?"),
    h("button", { class: "primary small", onclick: () => done(true) }, "Apply & remember"),
    h("button", { class: "ghost small", onclick: () => done(false) }, "Only this one"),
    h("button", { class: "link small", onclick: () => { prompt.remove(); sel.value = sel.dataset.prev; } }, "Cancel"));
  row.append(prompt);
  input.focus();
}

// ------------------------------------------------------------------ review card
function reviewCard(groups, period) {
  if (!groups.length) return null;
  const n = groups.reduce((a, g) => a + g.count, 0);
  const body = h("div", { class: "card-body" });
  for (const g of groups) body.append(reviewRow(g));
  const hasSuggestions = groups.some((g) => g.suggestion_id);
  return h("div", { class: "card review" },
    h("div", { class: "card-head" }, h("h2", {}, `⚠ To review (${n})`),
      h("span", { class: "muted small" }, "The local AI wasn't sure about these. Pick a category; tick remember to teach it."),
      h("div", { class: "right" }, hasSuggestions ? h("button", { class: "ghost small", onclick: async () => {
        const r = await api("/api/review/accept-all", { body: { period, account: S.account || null } });
        toast(`Accepted ${r.changed} suggestion(s)`, r.undo); await refresh();
      } }, "Accept all suggestions") : null)),
    body);
}

function reviewRow(g) {
  const sel = catSelect(g.suggestion_id, { placeholder: "Choose category…" });
  const pattern = h("input", { type: "text", value: g.pattern, "aria-label": "Pattern to remember" });
  const remember = h("input", { type: "checkbox", checked: true });
  remember.addEventListener("change", () => pattern.classList.toggle("hidden", !remember.checked));
  sel.addEventListener("change", () => resolveCat(sel));
  const ok = h("button", { class: "ok-btn", title: "Confirm", onclick: async () => {
    const id = sel.value && sel.value !== "__new" ? +sel.value : await resolveCat(sel);
    if (!id) return toast("Choose a category first");
    const r = await api("/api/assign", { body: { txn_ids: g.txn_ids, category_id: id, remember: remember.checked, pattern: pattern.value } });
    toast(`Updated ${r.changed} transaction(s)` + (r.not_remembered ? ` · “${pattern.value}” is too generic to remember` : remember.checked ? ` · remembered “${pattern.value}”` : ""), r.undo);
    await refresh();
  } }, "✓");
  return h("div", { class: "rg" },
    h("span", { class: "who", title: g.label }, g.label),
    h("span", { class: "muted" }, `${g.count}×`),
    h("span", { class: "num" }, money(g.total)),
    h("span", {}, sel, g.suggestion_id && g.confidence != null ? h("div", { class: "conf" }, `AI suggestion · confidence ${g.confidence.toFixed(2)}`) : null),
    ok,
    h("label", { class: "remember" }, remember, "Remember", pattern),
    h("div", { class: "samples" }, g.samples.slice(0, 3).map((s) =>
      h("div", {}, `${shortDate(s.date)} · ${s.account} · ${money(s.amount)} ${s.currency} · ${s.description}`))));
}

// ------------------------------------------------------------------ report rendering
/** Bars + amount + % + vs. avg for a list of category lines; click a line to see its transactions. */
function categoryLines(lines, key, { barClass = "", reviewName = "To review" } = {}) {
  const cur = S.status.currency;
  const max = Math.max(1, ...lines.map((c) => c.amount));
  const body = h("div", {}, h("div", { class: "cat-head" }, h("span", {}, "Category"), h("span", {}), h("span", { class: "num" }, cur), h("span", { class: "num" }, "% total"), h("span", { class: "num" }, "vs. avg"), h("span", {})));
  for (const c of lines) {
    const id = key + ":" + c.category;
    const open = S.expanded.has(id);
    const bar = h("div", { class: "bar " + barClass + (c.category === reviewName ? " review" : c.spread ? " spread" : "") });
    bar.style.width = `${Math.max(1, (100 * c.amount) / max)}%`;
    const up = barClass === "income" ? "delta-down" : "delta-up", down = barClass === "income" ? "delta-up" : "delta-down";
    const delta = c.vs_avg == null ? h("span", { class: "num muted avg", title: "Not enough history yet" }, "–")
      : h("span", { class: "num avg " + (c.vs_avg > 5 ? up : c.vs_avg < -5 ? down : "muted"), title: `12-month average ${money(c.avg)}` }, (c.vs_avg > 0 ? "+" : "") + pct(c.vs_avg));
    body.append(h("div", { class: "cat-row", onclick: () => { open ? S.expanded.delete(id) : S.expanded.add(id); renderReport(); } },
      h("span", { class: "cat-name" }, c.category), h("div", { class: "bar-track" }, bar),
      h("span", { class: "num" }, money(c.amount)), h("span", { class: "num" }, pct(c.share)), delta,
      h("span", { class: "caret" }, c.transactions.length ? (open ? "⌄" : "›") : "")));
    if (open && c.transactions.length) body.append(h("div", { class: "txns" }, c.transactions.map(txnRow)));
  }
  return body;
}

function kpi(label, value, sub, cls) {
  return h("div", { class: "kpi " + (cls || "") }, h("div", { class: "label" }, label), h("div", { class: "value" }, value), sub ? h("div", { class: "sub" }, sub) : null);
}

function card(title, body, right) {
  return h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, title), right ? h("div", { class: "right" }, right) : null), h("div", { class: "card-body" }, body));
}

function periodNav() {
  const months = S.status.months, years = [...new Set(months.map((m) => m.slice(0, 4)))];
  let label = "All time", prev = null, next = null;
  if (S.mode === "month" && S.month) {
    const i = months.indexOf(S.month);
    label = monthLabel(S.month); prev = months[i - 1]; next = months[i + 1];
  } else if (S.mode === "year" && S.year) {
    const i = years.indexOf(S.year);
    label = S.year; prev = years[i - 1]; next = years[i + 1];
  }
  $("#periodLabel").textContent = label;
  $("#prevPeriod").disabled = !prev;
  $("#nextPeriod").disabled = !next;
  $("#prevPeriod").onclick = () => { if (S.mode === "month") S.month = prev; else S.year = prev; renderReport(); };
  $("#nextPeriod").onclick = () => { if (S.mode === "month") S.month = next; else S.year = next; renderReport(); };
  $$(".seg button").forEach((b) => b.classList.toggle("active", b.dataset.mode === S.mode));
}

async function renderReport() {
  const body = $("#reportBody");
  if (!S.status) return;
  periodNav();
  if (!S.status.transactions) {
    body.replaceChildren(h("div", { class: "empty" }, h("h2", {}, "No statements yet"),
      h("p", {}, "Upload your PostFinance, Swisscard or Wise PDFs to see where your money goes."),
      h("button", { class: "primary", onclick: () => show("upload") }, "Upload statements")));
    return;
  }
  const acc = S.account ? `&account=${encodeURIComponent(S.account)}` : "";
  try {
    if (S.q) return renderSearch(body);
    if (S.mode === "month") return await renderMonth(body, acc);
    if (S.mode === "year") return await renderYear(body, acc);
    return await renderAll(body, acc);
  } catch (e) {
    body.replaceChildren(h("div", { class: "empty" }, "Could not load the report: " + e.message));
  }
}

async function renderMonth(body, acc) {
  const [r, groups] = await Promise.all([
    api(`/api/report/month/${S.month}?spread=${S.spread}${acc}`),
    api(`/api/review?period=${S.month}${acc}`)]);
  const cur = S.status.currency;
  const spreadToggle = h("label", { class: "toggle" }, h("input", { type: "checkbox", checked: S.spread, onchange: (e) => { S.spread = e.target.checked; store.set("spread", S.spread); renderReport(); } }), "Spread yearly costs over 12 months");
  const kpis = h("div", { class: "kpis" },
    kpi("Income", money(r.income + r.unreviewed_income), r.unreviewed_income ? `incl. ${money(r.unreviewed_income)} to review` : cur),
    kpi("Monthly costs", money(r.monthly_costs), r.spread ? `${money(r.cost_of_living)} incl. yearly share` : cur),
    kpi("Yearly costs paid", money(r.yearly_paid), "shown separately"),
    kpi("Net", money(r.net), r.savings_rate != null ? `saved ${pct(r.savings_rate)} of income` : null, r.net < 0 ? "warn" : ""),
    r.to_review ? kpi("To review", String(r.to_review), "transactions", "warn") : null);

  const catBody = categoryLines(r.categories, "cost");
  if (!r.categories.length) catBody.append(h("p", { class: "muted" }, "No monthly costs in this period."));
  const incBody = categoryLines(r.income_lines, "income", { barClass: "income", reviewName: "To review (money in)" });
  if (!r.income_lines.length) incBody.append(h("p", { class: "muted" }, "No income in this period."));
  const avgNote = r.avg_months ? `vs. avg = compared with your average of the previous ${r.avg_months} month(s)` : "vs. avg appears once there is an earlier month to compare with";

  const parts = [kpis, reviewCard(groups, S.month),
    card("Monthly costs by category", [catBody, h("p", { class: "muted small" }, avgNote)], spreadToggle),
    card("Income", incBody, h("span", { class: "muted small" }, `Total ${money(r.income + r.unreviewed_income)}`))];
  if (r.yearly.length) parts.push(card("Yearly costs paid this month", h("div", { class: "txns" }, r.yearly.map(txnRow)),
    h("button", { class: "link", onclick: () => { S.mode = "year"; S.year = S.month.slice(0, 4); store.set("mode", S.mode); renderReport(); } }, "→ open yearly report")));
  if (r.transfers.length) {
    const open = S.expanded.has("__tr");
    parts.push(card("Transfers between your accounts",
      open ? h("div", { class: "txns" }, r.transfers.map(txnRow)) : h("p", { class: "muted" }, `${r.transfers.length} transaction(s): card bills, top-ups, conversions. Not counted as spending or income.`),
      h("button", { class: "link", onclick: () => { open ? S.expanded.delete("__tr") : S.expanded.add("__tr"); renderReport(); } }, open ? "Hide" : "Show")));
  }
  parts.push(h("div", { class: "two-col" },
    card("Top merchants", r.top_merchants.map((m) => h("div", { class: "list-row" }, h("span", { title: m.name }, m.name), h("span", { class: "num" }, money(m.amount))))),
    card("Largest expenses", r.largest.map((t) => h("div", { class: "list-row" }, h("span", { title: t.description }, `${shortDate(t.date)} · ${t.name}`), h("span", { class: "num" }, money(-t.amount_chf)))))));
  parts.push(notesCard());
  parts.push(footer(r.without_rate));
  body.replaceChildren(...parts.filter(Boolean));
}

function notesCard() {
  const out = h("div", { class: "notes muted" }, "A short review of this month, written by the local AI from the totals above.");
  const btn = h("button", { class: "ghost small", onclick: async () => {
    btn.disabled = true; out.textContent = "Thinking… (runs on this computer, may take ~20 s)";
    try { out.textContent = (await api("/api/notes", { body: { month: S.month, account: S.account || null } })).text; out.classList.remove("muted"); }
    catch (e) { out.textContent = e.message; }
    btn.disabled = false;
  } }, "Write accountant's notes");
  return card("Accountant's notes 🤖", out, btn);
}

function footer(withoutRate) {
  return h("p", { class: "muted small" },
    withoutRate ? `⚠ ${withoutRate} transaction(s) have no exchange rate yet and are not included (Settings → AI & currency). ` : "",
    `Amounts in ${S.status.currency}. Exchange rates: European Central Bank euro reference rates; CHF cross rates calculated by Local Accountant.`);
}

async function renderYear(body, acc) {
  const [r, groups] = await Promise.all([api(`/api/report/year/${S.year}?x=1${acc}`), api(`/api/review?period=${S.year}${acc}`)]);
  const kpis = h("div", { class: "kpis" },
    kpi("Income", money(r.income), `${r.active_months} month(s) with data`),
    kpi("Monthly costs", money(r.monthly_costs), r.active_months ? `avg ${money(r.monthly_costs / r.active_months)} / month` : null),
    kpi("Yearly costs", money(r.yearly_total), r.yearly_vs_last != null ? `${r.yearly_vs_last > 0 ? "+" : ""}${pct(r.yearly_vs_last)} vs. last year` : "no data for last year"),
    kpi("Net", money(r.net), r.savings_rate != null ? `saved ${pct(r.savings_rate)} of income` : null, r.net < 0 ? "warn" : ""),
    kpi("Set aside / month", money(r.set_aside), "to cover yearly bills"));

  const yc = h("div", {});
  for (const c of r.yearly_costs) {
    yc.append(h("div", { class: "list-row" }, h("strong", {}, c.category), h("span", { class: "num" }, money(c.total))));
    yc.append(h("div", { class: "txns" }, c.transactions.map(txnRow)));
  }
  if (!r.yearly_costs.length) yc.append(h("p", { class: "muted" }, "No yearly costs recorded in this year yet."));

  const mlabels = r.months.map((m) => MONTHS[+m.slice(5) - 1].slice(0, 3));
  const table = h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "Category"), mlabels.map((m) => h("th", {}, m)), h("th", {}, "Total"), h("th", {}, "Avg / month"))),
    h("tbody", {},
      r.categories.map((c) => h("tr", {}, h("td", {}, c), r.months.map((m) => h("td", { class: r.matrix[m][c] ? "" : "dim" }, r.matrix[m][c] ? money(r.matrix[m][c]) : "·")),
        h("td", {}, money(r.category_totals[c])), h("td", {}, money(r.category_avg[c])))),
      h("tr", { class: "total" }, h("td", {}, "Monthly costs"), r.months.map((m) => h("td", {}, r.per_month[m].has_data ? money(r.per_month[m].monthly_costs) : "·")), h("td", {}, money(r.monthly_costs)), h("td", {}, r.active_months ? money(r.monthly_costs / r.active_months) : "–")),
      h("tr", {}, h("td", {}, "Yearly costs"), r.months.map((m) => h("td", { class: "dim" }, r.per_month[m].yearly ? money(r.per_month[m].yearly) : "·")), h("td", {}, money(r.yearly_total)), h("td", {}, "")),
      h("tr", {}, h("td", {}, "Income"), r.months.map((m) => h("td", { class: "dim" }, r.per_month[m].has_data ? money(r.per_month[m].income) : "·")), h("td", {}, money(r.income)), h("td", {}, "")),
      h("tr", { class: "total" }, h("td", {}, "Net"), r.months.map((m) => h("td", {}, r.per_month[m].has_data ? money(r.per_month[m].net) : "·")), h("td", {}, money(r.net)), h("td", {}, ""))));

  const incTable = h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "Income"), mlabels.map((m) => h("th", {}, m)), h("th", {}, "Total"), h("th", {}, "Avg / month"))),
    h("tbody", {},
      r.income_categories.map((c) => h("tr", {}, h("td", {}, c), r.months.map((m) => h("td", { class: r.income_matrix[m][c] ? "" : "dim" }, r.income_matrix[m][c] ? money(r.income_matrix[m][c]) : "·")),
        h("td", {}, money(r.income_totals[c])), h("td", {}, money(r.income_avg[c])))),
      h("tr", { class: "total" }, h("td", {}, "Total income"), r.months.map((m) => h("td", {}, r.per_month[m].has_data ? money(r.per_month[m].income) : "·")), h("td", {}, money(r.income)), h("td", {}, r.active_months ? money(r.income / r.active_months) : "–"))));

  body.replaceChildren(...[kpis, reviewCard(groups, S.year),
    card("Income over the year", r.income_categories.length ? h("div", { class: "table-wrap" }, incTable) : h("p", { class: "muted" }, "No income recorded in this year yet.")),
    card("Yearly costs", yc),
    card("Monthly costs over the year", h("div", { class: "table-wrap" }, table)),
    askCard(), footer(0)].filter(Boolean));
}

function askCard() {
  const input = h("input", { type: "text", placeholder: "e.g. How much did I spend on eating out this year?", "aria-label": "Question" });
  const out = h("div", { class: "notes" });
  const go = async () => {
    if (!input.value.trim()) return;
    out.textContent = "Thinking… (runs on this computer)";
    try { out.textContent = (await api("/api/ask", { body: { question: input.value, account: S.account || null } })).text; }
    catch (e) { out.textContent = e.message; }
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  return card("Ask your accountant 🤖", [h("div", { class: "ask-row" }, input, h("button", { class: "primary", onclick: go }, "Ask")), out]);
}

async function renderAll(body, acc) {
  const [r, groups] = await Promise.all([api(`/api/report/all?x=1${acc}`), api(`/api/review?x=1${acc}`)]);
  const cats = [...new Set(r.years.flatMap((y) => Object.keys(y.categories)))];
  const table = h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, ""), r.years.map((y) => h("th", {}, y.year)))),
    h("tbody", {},
      h("tr", {}, h("td", {}, "Income"), r.years.map((y) => h("td", {}, money(y.income)))),
      h("tr", { class: "total" }, h("td", {}, "Monthly costs"), r.years.map((y) => h("td", {}, money(y.monthly_costs)))),
      cats.map((c) => h("tr", {}, h("td", { class: "dim" }, "  " + c), r.years.map((y) => h("td", { class: "dim" }, y.categories[c] ? money(y.categories[c]) : "·")))),
      h("tr", { class: "total" }, h("td", {}, "Yearly costs"), r.years.map((y) => h("td", {}, money(y.yearly_costs)))),
      h("tr", { class: "total" }, h("td", {}, "Net"), r.years.map((y) => h("td", {}, money(y.net)))),
      h("tr", {}, h("td", {}, "Savings rate"), r.years.map((y) => h("td", {}, pct(y.savings_rate))))));
  body.replaceChildren(...[reviewCard(groups, null), card("All time", h("div", { class: "table-wrap" }, table)), askCard(), footer(0)].filter(Boolean));
}

async function renderSearch(body) {
  const rows = await api(`/api/search?q=${encodeURIComponent(S.q)}`);
  body.replaceChildren(card(`Search: “${S.q}” — ${rows.length} result(s)`, rows.length ? h("div", { class: "txns" }, rows.map(txnRow)) : h("p", { class: "muted" }, "Nothing found.")));
}

// ------------------------------------------------------------------ upload & scan
function renderFiles() {
  const list = $("#fileList");
  list.replaceChildren(...S.files.map((f, i) => {
    const r = f.result || {};
    const check = r.balance_check === "ok" ? h("span", { class: "pill ok" }, "✅ balances match")
      : r.balance_check === "diff" ? h("span", { class: "pill warn" }, `⚠️ balances differ${r.balance_diff != null ? " by " + money(r.balance_diff) : ""}`)
      : r.balance_check === "none" ? h("span", { class: "pill" }, "no balance to check") : null;
    const statusPill = f.status === "waiting" ? h("button", { class: "link small", onclick: () => { S.files.splice(i, 1); renderFiles(); } }, "Remove")
      : f.status === "done" ? check
      : f.status === "error" ? h("span", { class: "pill bad" }, "Error")
      : h("span", { class: "pill" }, f.status === "reading" ? (f.result?.detail || "Reading…") : f.status);
    return h("div", { class: "file" },
      h("span", { class: "name" }, f.file.name), statusPill,
      f.status === "done" ? h("div", { class: "meta" },
        `${r.account} · ${r.period_from ? shortDate(r.period_from) + "." + r.period_from.slice(0, 4) : ""} – ${r.period_to ? shortDate(r.period_to) + "." + r.period_to.slice(0, 4) : ""} · ${r.added} new transaction(s)` + (r.duplicates ? ` · ${r.duplicates} already imported` : ""),
        r.import_id && r.balance_check === "diff" ? h("button", { class: "link small", onclick: () => { S.sub = "files"; S.openImport = r.import_id; show("settings"); } }, "Check rows") : null,
        r.import_id ? h("button", { class: "link small", onclick: async () => { await api(`/api/imports/${r.import_id}`, { method: "DELETE" }); f.status = "removed"; f.result = {}; toast("File removed from your data"); await loadStatus(); renderFiles(); } }, "Undo this file") : null)
        : h("div", { class: "meta" }, `${(f.file.size / 1024).toFixed(0)} KB`),
      (r.warnings || []).map((w) => h("div", { class: "warn" }, "⚠ " + w)),
      f.error ? h("div", { class: "err" }, f.error) : null);
  }));
  $("#scanBtn").disabled = !S.files.some((f) => f.status === "waiting");
}

function addFiles(fileList) {
  for (const file of fileList) {
    if (!/\.(pdf|csv|txt)$/i.test(file.name)) { toast(`${file.name}: only PDF or CSV`); continue; }
    S.files.push({ file, status: "waiting" });
  }
  renderFiles();
}

async function scan() {
  const pending = S.files.filter((f) => f.status === "waiting");
  if (!pending.length) return;
  const form = new FormData();
  pending.forEach((f) => { form.append("files", f.file, f.file.name); f.status = "queued"; });
  $("#scanBtn").disabled = true;
  renderFiles();
  let jobId;
  try { jobId = (await api("/api/scan", { form })).job_id; }
  catch (e) { pending.forEach((f) => (f.status = "waiting")); renderFiles(); return toast(e.message); }
  const stage = $("#scanStage"), bar = $("#scanProgress"), fill = $("#scanProgressFill");
  bar.classList.remove("hidden");
  for (;;) {
    const j = await api(`/api/jobs/${jobId}`);
    j.files.forEach((jf, i) => { pending[i].status = jf.status; pending[i].result = jf; pending[i].error = jf.error; });
    renderFiles();
    const p = j.progress;
    if (j.stage === "categorizing") {
      stage.textContent = `Categorizing with the local AI… ${p.total ? `${p.done}/${p.total} merchants` : ""}`;
      fill.style.width = p.total ? `${(100 * p.done) / p.total}%` : "5%";
    } else {
      stage.textContent = j.stage === "converting" ? "Converting currencies…" : "Reading statements…";
      fill.style.width = "3%";
    }
    if (j.done) {
      bar.classList.add("hidden");
      const s = j.summary || {};
      stage.textContent = j.error ? "Couldn't finish: " + j.error : s.llm_error ? "Done — local AI was not reachable, so new items are marked To review." : "Done.";
      await loadStatus();
      const ends = pending.map((f) => f.result && f.result.period_to).filter(Boolean).sort();
      if (ends.length) { S.month = ends[ends.length - 1].slice(0, 7); S.year = S.month.slice(0, 4); S.mode = "month"; }
      if (pending.some((f) => f.status === "done")) {
        toast("Scan complete — opening your report");
        setTimeout(() => show("report"), 700);
      }
      return;
    }
    await new Promise((r) => setTimeout(r, 700));
  }
}

// ------------------------------------------------------------------ settings
async function renderSettings() {
  $$(".subtabs button").forEach((b) => b.classList.toggle("active", b.dataset.sub === S.sub));
  const body = $("#settingsBody");
  const fn = { categories: setCategories, rules: setRules, files: setFiles, ai: setAI, privacy: setPrivacy }[S.sub];
  body.replaceChildren(await fn());
}

async function setCategories() {
  const kindLabel = { expense: "Spending", income: "Income", transfer: "Not spending" };
  const rows = S.cats.map((c) => h("div", { class: "set-row" },
    h("div", {}, h("strong", {}, c.name), " ", h("span", { class: "pill" }, kindLabel[c.kind] + (c.kind === "expense" ? (c.period === "yearly" ? " · Yearly report" : " · Monthly report") : ""))),
    h("div", { class: "desc" }, c.description || ""),
    h("div", {},
      h("button", { class: "ghost small", onclick: async () => { if (await catDialog(c)) { await loadStatus(); renderSettings(); } } }, "Edit"), " ",
      h("button", { class: "ghost small", onclick: async () => {
        if (!confirm(`Delete category “${c.name}”?`)) return;
        try { await api(`/api/categories/${c.id}`, { method: "DELETE" }); await loadStatus(); renderSettings(); } catch (e) { toast(e.message); }
      } }, "Delete"))));
  return card("Categories", [h("p", { class: "muted" }, "The description is what the local AI reads to decide. “Yearly report” categories are kept out of the monthly report."), ...rows],
    h("button", { class: "primary small", onclick: async () => { if (await catDialog()) { await loadStatus(); renderSettings(); } } }, "+ Add category"));
}

async function setRules() {
  const rules = await api("/api/rules");
  const filter = h("input", { type: "search", placeholder: "Filter…" });
  const list = h("div", {});
  const draw = () => list.replaceChildren(...rules.filter((r) => !filter.value || (r.pattern + r.category).toLowerCase().includes(filter.value.toLowerCase()))
    .map((r) => h("div", { class: "set-row" }, h("code", {}, r.pattern), h("span", {}, "→ " + r.category),
      h("button", { class: "ghost small", onclick: async () => { await api(`/api/rules/${r.id}`, { method: "DELETE" }); rules.splice(rules.indexOf(r), 1); draw(); toast("Rule deleted"); } }, "Delete"))));
  filter.addEventListener("input", draw);
  draw();
  return card(`Remembered rules (${rules.length})`, [h("p", { class: "muted" }, "Created when you tick “remember”. A rule matches whole words in a transaction description and always wins over the AI. Your manual choices are never overwritten."), filter, list]);
}

async function setFiles() {
  const imps = await api("/api/imports");
  const rows = [];
  for (const i of imps) rows.push(...await fileRow(i));
  return card("Scanned files", rows.length ? [h("p", { class: "muted" }, "If a file shows ⚠️, open “Check rows” and correct the row that was misread (✎). The check re-runs as you fix it."), ...rows] : h("p", { class: "muted" }, "No files scanned yet."));
}

async function fileRow(i) {
  const open = S.openImport === i.id;
  const out = [h("div", { class: "set-row wide" },
    h("div", {}, h("strong", {}, i.filename), h("div", { class: "desc" }, `${i.account} · ${i.period_from || ""} – ${i.period_to || ""} · ${i.added} transaction(s) · scanned ${i.imported_at.replace("T", " ")}`)),
    h("div", {}, i.balance_check === "ok" ? h("span", { class: "pill ok" }, "✅ balances match") : i.balance_check === "diff" ? h("span", { class: "pill warn" }, i.balance_diff != null ? `⚠️ differs by ${money(i.balance_diff)}` : "⚠️ balances differ") : h("span", { class: "pill" }, "not checked"),
      i.warnings.map((w) => h("div", { class: "desc" }, "⚠ " + w))),
    h("div", {},
      h("button", { class: (i.balance_check === "diff" ? "primary" : "ghost") + " small", onclick: () => { S.openImport = open ? null : i.id; renderSettings(); } }, open ? "Hide rows" : "Check rows"), " ",
      h("button", { class: "ghost small", onclick: async () => {
        if (!confirm(`Remove ${i.filename} and its ${i.added} transaction(s)?`)) return;
        await api(`/api/imports/${i.id}`, { method: "DELETE" }); await loadStatus(); renderSettings(); toast("File removed");
      } }, "Remove")))];
  if (open) {
    const d = await api(`/api/imports/${i.id}/transactions`);
    out.push(h("div", { class: "txns" }, d.transactions.map(txnRow)));
  }
  return out;
}

async function setAI() {
  const s = await api("/api/settings");
  const models = S.status.models.length ? S.status.models : [s.llm_model];
  const model = h("select", {}, models.map((m) => h("option", { value: m }, m)));
  model.value = s.llm_model;
  const parser = h("select", {}, models.map((m) => h("option", { value: m }, m)));
  parser.value = s.parser_model || s.llm_model;
  const thr = h("input", { type: "number", min: "0.5", max: "0.99", step: "0.05", value: s.confidence_threshold });
  const url = h("input", { type: "text", value: s.llm_url });
  const fxdl = h("input", { type: "checkbox", checked: s.fx_download === "1" });
  const dbl = h("input", { type: "checkbox", checked: s.double_check !== "0" });
  const manual = h("input", { type: "text", value: Object.entries(JSON.parse(s.fx_manual || "{}")).map(([k, v]) => `${k}=${v}`).join(", "), placeholder: "EUR=0.94, USD=0.80" });
  const save = async () => {
    const fxm = {};
    manual.value.split(",").map((x) => x.trim()).filter(Boolean).forEach((p) => { const [k, v] = p.split("="); if (k && v) fxm[k.trim()] = parseFloat(v); });
    try {
      await api("/api/settings", { body: { llm_model: model.value, parser_model: parser.value, confidence_threshold: parseFloat(thr.value), llm_url: url.value, fx_download: fxdl.checked, double_check: dbl.checked, fx_manual: fxm } });
      toast("Settings saved"); await refresh();
    } catch (e) { toast(e.message); }
  };
  return card("AI & currency", [h("div", { class: "form-grid" },
    h("label", {}, "Ask me when AI confidence is below"), thr,
    h("span", {}), h("div", { class: "hint" }, "Higher = more questions, fewer wrong guesses. Default 0.8."),
    h("label", {}, "Double-check AI answers"), h("label", { class: "toggle" }, dbl, "Ask twice; if the answers differ, ask me instead of guessing"),
    h("span", {}), h("div", { class: "hint" }, "About twice as slow on first scans, but far fewer wrong guesses. Recommended."),
    h("label", {}, "Download ECB exchange rates"), h("label", { class: "toggle" }, fxdl, "Public file, no personal data sent"),
    h("label", {}, "Manual rates (CHF per unit)"), manual,
    h("span", {}), h("div", { class: "hint" }, "Used when ECB rates are off or missing.")),
    h("details", { class: "advanced" }, h("summary", {}, "Advanced"),
      h("div", { class: "form-grid" },
        h("label", {}, "AI for categories & notes"), model,
        h("span", {}), h("div", { class: "hint" }, "The AIs available on this computer."),
        h("label", {}, "AI for reading statements"), parser,
        h("span", {}), h("div", { class: "hint" }, "Smaller is faster, bigger is more careful."),
        h("label", {}, "Local AI address"), url,
        h("span", {}), h("div", { class: "hint" }, "Must be on this computer; other addresses are refused."))),
    h("p", {}, h("button", { class: "primary", onclick: save }, "Save"), " ",
      h("button", { class: "ghost", onclick: async () => { const r = await api("/api/recategorize", { body: {} }); toast("Asking the local AI again for unanswered items…"); pollJob(r.job_id); } }, "Re-run AI on To review items"))]);
}

async function pollJob(id) {
  for (;;) { const j = await api(`/api/jobs/${id}`); if (j.done) { await refresh(); return toast("Done"); } await new Promise((r) => setTimeout(r, 1000)); }
}

async function setPrivacy() {
  return card("Privacy", [
    h("p", {}, "🔒 Everything runs on this computer:"),
    h("ul", {},
      h("li", {}, "Your transactions are stored in the app's folder on this computer, readable only by you."),
      h("li", {}, "Uploaded PDFs are read in memory and not kept."),
      h("li", {}, "Reading statements, categorizing, notes and answers use an AI that runs on this computer. AIs elsewhere are refused."),
      h("li", {}, "The only internet request is the public ECB exchange-rate file (no data about you is sent). You can switch it off under AI & currency."),
      h("li", {}, "This page loads nothing from the internet.")),
    h("p", { class: "muted" }, "Tip: turn on full-disk encryption (FileVault / BitLocker) and keep the app folder out of cloud-synced folders."),
    h("p", { class: "muted" }, "Exchange rates: European Central Bank euro foreign exchange reference rates; CHF cross rates calculated by Local Accountant.")]);
}

// ------------------------------------------------------------------ wiring
function wire() {
  $$(".tab").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));
  $$(".seg button").forEach((b) => b.addEventListener("click", () => { S.mode = b.dataset.mode; store.set("mode", S.mode); renderReport(); }));
  $$(".subtabs button").forEach((b) => b.addEventListener("click", () => { S.sub = b.dataset.sub; renderSettings(); }));
  $("#accountFilter").addEventListener("change", (e) => { S.account = e.target.value; renderReport(); });
  let t;
  $("#search").addEventListener("input", (e) => { clearTimeout(t); t = setTimeout(() => { S.q = e.target.value.trim(); renderReport(); }, 250); });
  const dz = $("#dropzone");
  ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("over"); }));
  dz.addEventListener("drop", (e) => addFiles(e.dataTransfer.files));
  dz.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") $("#fileInput").click(); });
  $("#fileInput").addEventListener("change", (e) => { addFiles(e.target.files); e.target.value = ""; });
  $("#scanBtn").addEventListener("click", scan);
}

(async function main() {
  wire();
  try { await loadStatus(); } catch (e) { $("#reportBody").textContent = "Cannot reach the app server: " + e.message; return; }
  show(S.status.transactions ? "report" : "upload");
})();
