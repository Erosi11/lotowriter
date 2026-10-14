// LotoWriter web app (no build step). Hash routes:
//   #/                 procedure list          #/new            new procedure
//   #/rev/ID/TAB       procedure (points | details | print | copy)
//   #/rev/ID/add       add isolation point     #/rev/ID/point/PID  edit isolation point
//   #/admin            users, sites, departments
//   #/password         change password         #/sync           upload status / problems
import * as data from "./data.js";
import { editMarkup, photoFigure } from "./annotate.js";

const $app = document.getElementById("app");
const $nav = document.getElementById("nav");
const state = { me: null, config: null, energy: {}, dirty: false, autoCheckoutTried: new Set() };

const COMMON_DEVICES = ["Breaker lockout", "Disconnect lockout", "Ball valve cover", "Gate valve cover",
  "Plug cover", "Pneumatic lockout", "Hasp", "Group lockbox", "Chain / cable lockout", "Safety blocks"];
const STATUS_TEXT = { draft: "Draft", ready_for_review: "Ready for review", printed_for_review: "Printed for review",
  returned: "Returned with markups", approved: "Approved", active: "Active", superseded: "Superseded" };
const OP_TEXT = { "revision.update": "Details", "point.add": "New point", "point.update": "Point edit",
  "point.delete": "Point deleted", "point.reorder": "Point order", "point.check": "Point checked",
  "photo.add": "Photo", "photo.delete": "Photo removed", "photo.move": "Photo order", "photo.annotate": "Photo markup" };

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => { /* app still works online */ });
}

// ------------------------------------------------------------------ helpers

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "style") Object.assign(e.style, v);
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "value") e.value = v;
    else if (k === "checked") e.checked = !!v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return e;
}

// Like parent.append(), but skips null/false (used for optional sections).
function append(parent, ...children) {
  parent.append(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));
  return parent;
}

function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.remove("show"), 2600);
}

function handleAuth(err) {
  if (err.status === 401) { state.me = null; go("#/login"); }
  if (err.status === 403 && /change your password/i.test(err.message)) go("#/password");
}

async function api(method, path, body) {
  try {
    return await data.api(method, path, body);
  } catch (err) {
    if (!path.endsWith("/login")) handleAuth(err);
    throw err;
  }
}

// Apply a change: on this device's working copy if checked out here, otherwise directly on the server.
async function change(rid, op, blob) {
  try {
    return await data.change(rid, op, blob);
  } catch (err) {
    handleAuth(err);
    throw err;
  }
}

function go(hash) {
  if (location.hash === hash) route(); else location.hash = hash;
}

function errorBox(err) {
  const lines = String(err.message || err).split("\n");
  const box = el("div", { class: "notice error", role: "alert" }, el("strong", {}, lines[0]));
  if (lines.length > 1) append(box, el("ul", {}, lines.slice(1).map((l) => el("li", {}, l.replace(/^- /, "")))));
  if (err.status === 409) append(box, el("div", { class: "row" },
    el("button", { class: "small", onclick: () => { state.dirty = false; route(); } }, "Reload")));
  return box;
}

function showError(container, err) {
  container.querySelectorAll(".notice.error").forEach((n) => n.remove());
  const box = errorBox(err);
  container.prepend(box);
  box.scrollIntoView({ behavior: "smooth", block: "center" });
}

function field(label, input, hint) {
  const id = input.id || `f_${Math.random().toString(36).slice(2)}`;
  input.id = id;
  return el("div", {}, el("label", { for: id }, label, hint ? el("span", { class: "hint" }, ` — ${hint}`) : null), input);
}

const text = (name, value = "", attrs = {}) => el("input", { type: "text", name, value, ...attrs });
const area = (name, value = "", attrs = {}) => el("textarea", { name, ...attrs }, value);
const linesOf = (v) => v.split("\n").map((s) => s.trim()).filter(Boolean);
const editor = () => state.me && state.me.role !== "read_only";
const canEdit = (rev) => rev.editable && (!rev.checkout || rev.checkout.mine) && !data.status.lost[rev.id];

function markDirty(form) {
  form.addEventListener("input", () => { state.dirty = true; });
  form.addEventListener("change", () => { state.dirty = true; });
}

function energyStyle(et) {
  if (!et) return {};
  const s = { backgroundColor: `#${et.fill}`, color: `#${et.text_color}` };
  if (et.pattern === "thinDiagStripe") {
    s.backgroundImage = `repeating-linear-gradient(135deg, transparent 0 6px, #${et.pattern_color} 6px 8px)`;
  } else if (et.pattern === "diagCross") {
    s.backgroundImage = `repeating-linear-gradient(45deg, transparent 0 6px, #ffffff 6px 7px),` +
      `repeating-linear-gradient(-45deg, transparent 0 6px, #ffffff 6px 7px)`;
  }
  return s;
}

function statusBadge(status) {
  return el("span", { class: `badge ${status}` }, STATUS_TEXT[status] || status);
}

function render(...nodes) {
  $app.replaceChildren(...nodes.flat(Infinity).filter((n) => n !== null && n !== undefined && n !== false));
  $app.focus({ preventScroll: true });
  window.scrollTo(0, 0);
}

async function photoSrc(ph) {
  if (ph.file_id) return `/api/photo-files/${ph.file_id}`;
  return (await data.photoBlobUrl(ph.id)) || "";
}

// ------------------------------------------------------------------ top bar & sync indicator

const $pill = el("a", { href: "#/sync", class: "sync-pill", "aria-live": "polite" });

function drawPill(s) {
  let cls = "ok", label = "All saved";
  if (s.problems) { cls = "bad"; label = `${s.problems} problem${s.problems > 1 ? "s" : ""}`; }
  else if (Object.keys(s.lost).length) { cls = "bad"; label = "Upload blocked"; }
  else if (!s.online) { cls = "warn"; label = s.pending ? `Offline · ${s.pending} waiting` : "Offline"; }
  else if (s.syncing) { cls = "busy"; label = "Uploading…"; }
  else if (s.pending) { cls = "warn"; label = `${s.pending} waiting`; }
  $pill.className = `sync-pill ${cls}`;
  $pill.textContent = label;
  $pill.title = s.lastSync ? `Last upload ${s.lastSync.toLocaleTimeString()}` : "";
}
data.onStatus(drawPill);

async function signOut() {
  const s = data.status;
  if (s.pending) {
    if (!s.online) { alert("Changes on this device haven't uploaded yet. Connect to the network before signing out."); return; }
    await data.syncNow();
    if (data.status.pending && !confirm(`${data.status.pending} change(s) could not be uploaded and will be lost. Sign out anyway?`)) return;
  }
  await api("POST", "/api/logout").catch(() => {});
  await data.forgetSession();
  state.me = null;
  go("#/login");
}

function setNav() {
  $nav.replaceChildren();
  if (!state.me) return;
  if (state.me.must_change_password) {
    $nav.append(el("button", { onclick: signOut }, "Sign out"));
    return;
  }
  append($nav, $pill, el("a", { href: "#/dashboard" }, "Dashboard"), el("a", { href: "#/" }, "Procedures"),
    state.me.role === "admin" ? el("a", { href: "#/admin" }, "Admin") : null,
    el("a", { href: "#/password", title: "Change password", class: "who" }, state.me.display_name),
    el("button", { onclick: signOut }, "Sign out"));
}

async function loadConfig() {
  state.config = await data.getConfig();
  state.energy = Object.fromEntries(state.config.energy_types.map((t) => [t.code, t]));
  data.setEnergyTypes(state.energy);
}

// ------------------------------------------------------------------ router

window.addEventListener("beforeunload", (e) => { if (state.dirty) { e.preventDefault(); e.returnValue = ""; } });

let lastHash = location.hash;
window.addEventListener("hashchange", () => {
  if (state.dirty && !confirm("You have unsaved changes. Leave without saving?")) {
    history.replaceState(null, "", lastHash);
    return;
  }
  state.dirty = false;
  route();
});

async function route() {
  lastHash = location.hash;
  const hash = location.hash || "#/";
  const parts = hash.slice(2).split("?")[0].split("/").filter(Boolean);
  try {
    if (!state.me && parts[0] !== "login") {
      try { state.me = await data.getMe(); } catch { return viewLogin(); }
    }
    if (parts[0] === "login") return viewLogin();
    if (state.me.must_change_password || parts[0] === "password") return viewPassword();
    if (!state.config) await loadConfig();
    setNav();
    if (parts.length === 0) return await viewList();
    if (parts[0] === "new") return await viewNew();
    if (parts[0] === "admin") return await viewAdmin();
    if (parts[0] === "sync") return await viewSync();
    if (parts[0] === "dashboard") return await viewDashboard();
    if (parts[0] === "rev") {
      const rid = Number(parts[1]);
      if (parts[2] === "add") return await viewAddPoint(rid);
      if (parts[2] === "point") return await viewPoint(rid, /^\d+$/.test(parts[3]) ? Number(parts[3]) : parts[3]);
      return await viewRevision(rid, parts[2] || "points");
    }
    render(el("p", {}, "Page not found. ", el("a", { href: "#/" }, "Go to procedures")));
  } catch (err) {
    if (err.status === 401) return;
    render(errorBox(err), el("p", {}, el("a", { href: "#/" }, "Back to procedures")));
  }
}

// ------------------------------------------------------------------ sign in / password

function viewLogin() {
  setNav();
  const form = el("form", { class: "card login" },
    el("h1", {}, "Sign in"),
    field("User name", text("username", "", { autocomplete: "username", autocapitalize: "none", required: true })),
    field("Password", el("input", { type: "password", name: "password", autocomplete: "current-password", required: true })),
    el("div", { class: "row", style: { marginTop: "16px" } }, el("button", { class: "primary grow", type: "submit" }, "Sign in")));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      state.me = await api("POST", "/api/login", { username: form.username.value, password: form.password.value });
      go(state.me.must_change_password ? "#/password" : "#/");
    } catch (err) { showError(form, err); }
  });
  render(form);
  form.username.focus();
}

function viewPassword() {
  setNav();
  const forced = state.me.must_change_password;
  const form = el("form", { class: "card login" },
    el("h1", {}, forced ? "Set a new password" : "Change password"),
    forced ? el("div", { class: "notice warn" }, "You must change your password before continuing.") : null,
    field("Current password", el("input", { type: "password", name: "current", autocomplete: "current-password", required: true })),
    field("New password", el("input", { type: "password", name: "new", autocomplete: "new-password", required: true, minlength: 8 }),
      "at least 8 characters"),
    field("Repeat new password", el("input", { type: "password", name: "again", autocomplete: "new-password", required: true })),
    el("div", { class: "row", style: { marginTop: "16px" } }, el("button", { class: "primary grow", type: "submit" }, "Save password")));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (form.new.value !== form.again.value) return showError(form, new Error("The new passwords don't match."));
    try {
      await api("POST", "/api/me/password", { current: form.current.value, new: form.new.value });
      state.me = await data.getMe();
      toast("Password changed");
      go("#/");
    } catch (err) { showError(form, err); }
  });
  render(form);
}

// ------------------------------------------------------------------ procedure list

async function viewList() {
  const search = el("input", { type: "search", placeholder: "Search asset #, machine, OEM, model…" });
  const dept = el("select", {}, el("option", { value: "" }, "All departments"),
    state.config.departments.map((d) => el("option", { value: d.id }, `${d.code} – ${d.name}`)));
  const status = el("select", {}, el("option", { value: "" }, "All statuses"),
    Object.entries(STATUS_TEXT).map(([k, v]) => el("option", { value: k }, v)));
  const results = el("div");

  async function load() {
    const qs = new URLSearchParams({ q: search.value });
    if (dept.value) qs.set("department_id", dept.value);
    if (status.value) qs.set("status", status.value);
    const { rows, offline } = await data.listProcedures(qs);
    results.replaceChildren();
    append(results,
      offline ? el("div", { class: "notice warn" }, el("strong", {}, "Offline. "),
        "Showing the procedures saved on this device. Everything else needs a connection.") : null,
      rows.length ? null : el("p", { class: "muted" },
        offline ? "No procedures are saved on this device." :
          state.me.role === "read_only" ? "No active procedures found." : "No procedures yet."),
      rows.map((r) => el("a", { class: "card link", href: `#/rev/${r.revision_id}` },
        el("div", { class: "spread" },
          el("strong", {}, r.procedure_number),
          el("div", { class: "row" },
            r.flag_reason ? el("span", { class: "badge needs" }, "Flagged") : null,
            r.active_rev_no !== null && r.active_rev_no !== undefined && r.status !== "active"
              ? el("span", { class: "badge active" }, `In use: V${String(r.active_rev_no).padStart(2, "0")}`) : null,
            statusBadge(r.status))),
        el("div", {}, `${r.asset_number} – ${r.machine_type}`,
          el("span", { class: "muted" }, `  ·  ${r.dept_name}`)),
        el("div", { class: "small muted" }, `${r.point_count} isolation point${r.point_count === 1 ? "" : "s"}`,
          r.needs_check ? el("span", { class: "badge needs", style: { marginLeft: "8px" } },
            `${r.needs_check} to check`) : null))));
  }
  let timer;
  search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(load, 250); });
  dept.addEventListener("change", load);
  status.addEventListener("change", load);

  render(
    el("div", { class: "spread" }, el("h1", {}, "Procedures"),
      editor() ? el("a", { class: "button primary", href: "#/new" }, "+ New procedure") : null),
    el("div", { class: "two" }, search, el("div", { class: "row" }, el("div", { class: "grow" }, dept),
      el("div", { class: "grow" }, status))),
    results);
  await load();
}

// ------------------------------------------------------------------ new procedure

function departmentSelect(name, value) {
  const s = el("select", { name, required: true }, el("option", { value: "" }, "Choose department…"),
    state.config.departments.map((d) => el("option", { value: d.id }, `${d.code} – ${d.name}`)));
  if (value) s.value = value;
  return s;
}

function assetFields(a = {}) {
  return [
    el("div", { class: "two" },
      field("Asset #", text("asset_number", a.asset_number || "", { required: true, autocapitalize: "characters" })),
      field("Machine type", text("machine_type", a.machine_type || "", { required: true }), "printed on the form")),
    el("div", { class: "two" },
      field("Department", departmentSelect("department_id", a.department_id)),
      field("Location", text("location", a.location || ""))),
    el("div", { class: "two" },
      field("OEM", text("oem", a.oem || ""), "not printed"),
      field("Model", text("model", a.model || ""), "not printed")),
    field("Serial #", text("serial", a.serial || ""), "not printed"),
  ];
}

const assetValues = (form) => Object.fromEntries(
  ["asset_number", "machine_type", "department_id", "location", "oem", "model", "serial"]
    .map((k) => [k, form[k].value]));

async function viewNew() {
  const free = await api("GET", "/api/assets?without_procedure=1");
  const existing = el("select", { name: "asset_id" }, el("option", { value: "" }, "— Enter a new machine below —"),
    free.map((a) => el("option", { value: a.id }, `${a.asset_number} – ${a.machine_type} (${a.dept_code})`)));
  const newBox = el("div", {}, assetFields());
  const form = el("form", {},
    el("h1", {}, "New procedure"),
    el("p", { class: "muted" }, "Each machine has one procedure. The procedure number (SITE-DEPT-ASSET) is fixed when it is created."),
    free.length ? field("Machine already in the system", existing) : null,
    newBox,
    el("div", { class: "row", style: { marginTop: "16px" } },
      el("button", { class: "primary", type: "submit" }, "Create procedure"),
      el("a", { class: "button", href: "#/" }, "Cancel")));
  existing.addEventListener("change", () => {
    newBox.style.display = existing.value ? "none" : "";
    newBox.querySelectorAll("input,select").forEach((i) => { i.disabled = !!existing.value; });
  });
  const pre = new URLSearchParams(location.hash.split("?")[1] || "").get("asset");
  if (pre && free.some((a) => String(a.id) === pre)) {
    existing.value = pre;
    existing.dispatchEvent(new Event("change"));
  }
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const body = existing.value ? { asset_id: Number(existing.value) } : { asset: assetValues(form) };
      const r = await api("POST", "/api/procedures", body);
      state.dirty = false;
      toast("Procedure created");
      go(`#/rev/${r.revision_id}/points`);
    } catch (err) { showError(form, err); }
  });
  markDirty(form);
  render(form);
}

// ------------------------------------------------------------------ procedure view

function checkoutBanner(rev) {
  const lost = data.status.lost[rev.id];
  if (lost) {
    return el("div", { class: "notice error" },
      el("strong", {}, "Changes on this device can't be uploaded. "), lost,
      el("div", { class: "row", style: { marginTop: "8px" } },
        el("a", { class: "button small", href: "#/sync" }, "See the changes"),
        el("button", { class: "small danger", onclick: async () => {
          if (!confirm("Discard this device's unsent changes to this procedure? This can't be undone.")) return;
          await data.discardLocal(rev.id);
          route();
        } }, "Discard my changes")));
  }
  const c = rev.checkout;
  if (c && c.mine) {
    return el("div", { class: "notice ok small" },
      el("strong", {}, "On this device: "), "works offline; changes upload automatically.",
      el("div", { class: "row", style: { marginTop: "8px" } },
        el("button", { class: "small", onclick: async () => {
          try { await data.release(rev.id); toast("Released"); route(); } catch (err) { showError($app, err); }
        } }, "Done editing – release")));
  }
  if (c && !c.mine) {
    const own = c.user_id === state.me.id;
    return el("div", { class: "notice warn" },
      el("strong", {}, "Checked out "), `to ${c.user_name} on ${c.device_name} (since ${c.at} UTC). Editing here is blocked until it's released.`,
      el("div", { class: "row", style: { marginTop: "8px" } },
        own ? el("button", { class: "small", onclick: async () => {
          if (!confirm(`Take this procedure over on this device? Any changes on the ${c.device_name} that haven't uploaded yet will NOT be saved.`)) return;
          try { await data.checkout(rev.id, true); toast("Now on this device"); route(); } catch (err) { showError($app, err); }
        } }, "Take over on this device") : null,
        !own && state.me.role === "admin" ? el("button", { class: "small", onclick: async () => {
          if (!confirm(`Release ${c.user_name}'s checkout? Their changes that haven't uploaded will NOT be saved.`)) return;
          try { await data.release(rev.id, true); toast("Released"); route(); } catch (err) { showError($app, err); }
        } }, "Release (admin)") : null));
  }
  if (canEdit(rev) && data.status.online) {
    return el("div", { class: "row small muted" },
      el("button", { class: "small", onclick: async () => {
        try { await data.checkout(rev.id); toast("Available offline on this device"); route(); } catch (err) { showError($app, err); }
      } }, "Make available offline"), "for areas without Wi-Fi");
  }
  return null;
}

function revHeader(rev, tab) {
  const tabs = editor()
    ? [["points", "Isolation points"], ["details", "Details"], ["print", "Review & print"], ["copy", "Copy to similar"], ["history", "History"]]
    : [["points", "Isolation points"], ["print", "Print"]];
  return [
    el("div", { class: "spread" },
      el("div", {}, el("h1", { style: { marginBottom: "2px" } }, rev.procedure_number),
        el("div", {}, `${rev.asset_number} – ${rev.machine_type}`, el("span", { class: "muted" }, `  ·  ${rev.dept_name}`))),
      statusBadge(rev.status)),
    rev.copied_from ? el("div", { class: "small muted" }, `Copied from ${rev.copied_from}`) : null,
    rev.status !== "active" && rev.active_revision_id && rev.active_revision_id !== rev.id
      ? el("div", { class: "notice small" }, "The procedure in use is ",
        el("a", { href: `#/rev/${rev.active_revision_id}` }, (rev.history.find((h) => h.id === rev.active_revision_id) || {}).procedure_number || "the Active revision"), ".")
      : null,
    rev.status === "active" && rev.open_revision_id
      ? el("div", { class: "notice small" }, "A new revision is in progress: ",
        el("a", { href: `#/rev/${rev.open_revision_id}` }, (rev.history.find((h) => h.id === rev.open_revision_id) || {}).procedure_number || "open it"), ".")
      : null,
    rev.flag ? el("div", { class: "notice warn small" }, el("strong", {}, "Flagged for revision: "), rev.flag.reason,
      ` (${rev.flag.by || "?"}, ${rev.flag.at} UTC)`) : null,
    rev.source_newer && editor() ? el("div", { class: "notice warn small" },
      el("strong", {}, "The original was revised: "), `${rev.source_newer}. Check whether the same change applies here.`,
      el("div", { class: "row", style: { marginTop: "6px" } },
        el("button", { class: "small", onclick: () => workflowAction(rev, "ack_source_change", {}, $app, "Noted") }, "Reviewed – no change needed"))) : null,
    checkoutBanner(rev),
    el("nav", { class: "tabs" }, tabs.map(([k, label]) =>
      el("a", { href: `#/rev/${rev.id}/${k}`, class: k === tab ? "active" : "" }, label))),
  ];
}

async function viewRevision(rid, tab) {
  let rev = await data.loadRevision(rid);
  // Phones/tablets go to the plant floor: keep the draft on the device automatically.
  if (editor() && device().touch && canEdit(rev) && !rev.checkout && data.status.online && !state.autoCheckoutTried.has(rid)) {
    state.autoCheckoutTried.add(rid);
    try { rev = await data.checkout(rid); toast("Saved on this device for offline use"); } catch { /* someone else has it */ }
  }
  const views = { points: tabPoints, details: tabDetails, print: tabPrint, copy: tabCopy, history: tabHistory };
  const body = await (views[tab] || tabPoints)(rev);
  render(...revHeader(rev, tab), body);
}

const device = () => data.device;

function pointCard(rev, p, i) {
  const et = state.energy[p.energy_type];
  const editable = canEdit(rev);
  const move = async (dir) => {
    const ids = rev.points.map((x) => x.id);
    const j = i + dir;
    [ids[i], ids[j]] = [ids[j], ids[i]];
    try { await change(rev.id, { type: "point.reorder", ids, version: rev.version }); route(); }
    catch (err) { showError($app, err); }
  };
  return el("div", { class: "card point" },
    el("div", { class: "swatch", style: energyStyle(et) }, p.label),
    el(editable ? "a" : "div", editable ? { href: `#/rev/${rev.id}/point/${p.id}` } : {},
      el("div", { class: "title" }, `${i + 1}. ${et ? et.name : p.energy_type} – ${p.magnitude || "(magnitude?)"}`),
      el("div", { class: "small" }, p.device || el("span", { class: "muted" }, "Isolation device not entered")),
      el("div", { class: "small muted" },
        p.photos.length ? `${p.photos.length} photo${p.photos.length > 1 ? "s" : ""}` :
          (p.photo_missing_reason ? "Photo to be added" : "No photo"),
        `  ·  ${p.locks} lock${p.locks === 1 ? "" : "s"}`,
        p.copy_status === "needs_check" ? el("span", { class: "badge needs", style: { marginLeft: "8px" } }, "Check at machine") : null)),
    editable ? el("div", { class: "order" },
      el("button", { class: "small icon", title: "Move up", "aria-label": `Move ${p.label} up`, disabled: i === 0, onclick: () => move(-1) }, "▲"),
      el("button", { class: "small icon", title: "Move down", "aria-label": `Move ${p.label} down`, disabled: i === rev.points.length - 1, onclick: () => move(1) }, "▼"))
      : el("span"));
}

async function tabPoints(rev) {
  const needs = rev.points.filter((p) => p.copy_status === "needs_check").length;
  return el("div", {},
    needs ? el("div", { class: "notice warn" },
      el("strong", {}, `${needs} copied point${needs > 1 ? "s" : ""} still to check at this machine. `),
      "Open each point, retake or confirm its photo, and check the details.") : null,
    el("p", { class: "muted small" }, "List points in the order they will be shut down and isolated."),
    rev.points.length ? rev.points.map((p, i) => pointCard(rev, p, i)) : el("p", { class: "muted" }, "No isolation points yet."),
    canEdit(rev) ? el("div", { class: "sticky-actions" }, el("div", { class: "inner" },
      el("a", { class: "button primary", href: `#/rev/${rev.id}/add` }, "+ Add isolation point"))) : null);
}

// ------------------------------------------------------------------ details tab

async function tabDetails(rev) {
  const container = el("div");
  if (!canEdit(rev)) return el("div", { class: "notice" }, "This revision can't be edited here.");

  // Machine (asset) section — server only.
  const assetForm = el("form", { class: "card" }, el("h2", { style: { marginTop: 0 } }, "Machine"),
    el("p", { class: "small muted" }, "Machine/Equipment prints as “Asset # – Machine type”. Changing these does not change the procedure number. Needs a connection."),
    assetFields(rev), el("div", { class: "row", style: { marginTop: "12px" } }, el("button", { type: "submit" }, "Save machine")));
  assetForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("PUT", `/api/assets/${rev.asset_id}`, assetValues(assetForm));
      if (rev.checkout && rev.checkout.mine) await data.checkout(rev.id);  // refresh device copy
      state.dirty = false; toast("Machine saved"); route();
    } catch (err) { showError(assetForm, err); }
  });
  markDirty(assetForm);

  const commonChecks = COMMON_DEVICES.map((d) => el("label", { class: "check" },
    el("input", { type: "checkbox", name: "dev", value: d, checked: rev.lockout_devices.includes(d) }), d));
  const otherDevices = rev.lockout_devices.filter((d) => !COMMON_DEVICES.includes(d));
  const manual = el("input", { type: "checkbox", name: "review_date_manual", checked: !!rev.review_date_manual });
  const reviewInput = el("input", { type: "date", name: "review_date", value: rev.review_date || "", disabled: !rev.review_date_manual });
  manual.addEventListener("change", () => { reviewInput.disabled = !manual.checked; });

  const form = el("form", {},
    el("h2", {}, "Description"),
    field("Tasks covered", area("tasks_covered", rev.tasks_covered.join("\n")), "one per line"),
    el("h2", {}, "1. Notify affected employees"),
    field("Affected positions / roles", area("affected_employees", rev.affected_employees.join("\n")), "one per line"),
    el("h2", {}, "2. Shutdown"),
    field("Shutdown procedure reference", area("shutdown_procedure", rev.shutdown_procedure)),
    el("h2", {}, "3. Isolate all energy sources"),
    field("Text for this box", area("isolate_text", rev.isolate_text, {
      placeholder: "Leave blank to print: “Isolate the N energy isolation points in the order listed in the table below.”" })),
    el("h2", {}, "4. Apply lockout/tagout devices"),
    el("div", { class: "two" }, commonChecks),
    field("Other devices", area("other_devices", otherDevices.join("\n")), "one per line"),
    field("Group lockout considerations", area("group_lockout", rev.group_lockout)),
    el("h2", {}, "5. Release or restrain stored energy"),
    field("Text for this box", area("stored_energy", rev.stored_energy, {
      placeholder: "Leave blank to refer to each isolation point’s “Stored energy” step." })),
    el("h2", {}, "6. Verify isolation"),
    field("Text for this box", area("verify_isolation", rev.verify_isolation, {
      placeholder: "Leave blank to refer to each isolation point’s “Verify zero energy” step." })),
    el("h2", {}, "7. Perform servicing or maintenance"),
    field("Notes (optional)", area("servicing_notes", rev.servicing_notes)),
    el("h2", {}, "8. Release from lockout"),
    field("Restart steps", area("restart_steps", rev.restart_steps.join("\n")), "one step per line, printed numbered"),
    el("h2", {}, "# of locks required"),
    el("p", {}, `Calculated from isolation points: `, el("strong", {}, rev.locks_calculated)),
    el("div", { class: "two" },
      field("Override (optional)", el("input", { type: "number", min: 0, name: "locks_required_override",
        value: rev.locks_required_override ?? "" }), "leave blank to use the calculated number"),
      field("Reason for override", text("locks_override_reason", rev.locks_override_reason))),
    el("h2", {}, "Footer"),
    el("div", { class: "two" },
      field("Document effective date", el("input", { type: "date", name: "effective_date", value: rev.effective_date || "" })),
      field("Document review date", reviewInput, "defaults to 1 year after the effective date")),
    el("label", { class: "check" }, manual, "Set the review date manually"),
    field("Document owner", text("document_owner", rev.document_owner)),
    el("div", { class: "sticky-actions" }, el("div", { class: "inner" },
      el("button", { class: "primary", type: "submit" }, "Save details"))));

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const devices = [...form.querySelectorAll("input[name=dev]:checked")].map((c) => c.value)
      .concat(linesOf(form.other_devices.value));
    const fields = {
      tasks_covered: linesOf(form.tasks_covered.value),
      affected_employees: linesOf(form.affected_employees.value),
      shutdown_procedure: form.shutdown_procedure.value,
      isolate_text: form.isolate_text.value,
      lockout_devices: devices,
      group_lockout: form.group_lockout.value,
      stored_energy: form.stored_energy.value,
      verify_isolation: form.verify_isolation.value,
      servicing_notes: form.servicing_notes.value,
      restart_steps: linesOf(form.restart_steps.value),
      locks_required_override: form.locks_required_override.value === "" ? null : Number(form.locks_required_override.value),
      locks_override_reason: form.locks_override_reason.value,
      effective_date: form.effective_date.value || null,
      review_date_manual: manual.checked,
      document_owner: form.document_owner.value,
    };
    if (manual.checked) fields.review_date = reviewInput.value || null;
    if (fields.locks_required_override !== null && !(Number.isInteger(fields.locks_required_override) && fields.locks_required_override >= 0)) {
      return showError(form, new Error("# of locks must be a whole number."));
    }
    try {
      await change(rev.id, { type: "revision.update", fields, version: rev.version });
      state.dirty = false;
      toast("Details saved");
      route();
    } catch (err) { showError(form, err); }
  });
  markDirty(form);
  container.append(assetForm, form);
  return container;
}

// ------------------------------------------------------------------ check & print tab

async function needsServer(rev) {
  // Upload this device's changes first so the server checks / prints the latest version.
  if (!data.status.online) {
    try { await data.api("GET", "/api/health"); } catch { /* still offline */ }
  }
  if (!data.status.online) {
    return el("div", { class: "notice warn" }, el("strong", {}, "Offline. "),
      "Checking and printing need a connection to the server. Your changes are saved on this device.");
  }
  if ((await data.pendingOps(rev.id)).length) {
    await data.syncNow();
    if ((await data.pendingOps(rev.id)).length) {
      return el("div", { class: "notice warn" }, "Some changes haven't uploaded yet. ", el("a", { href: "#/sync" }, "See upload status"));
    }
  }
  return null;
}

const today = () => new Date().toISOString().slice(0, 10);

async function workflowAction(rev, action, body, box, okMessage) {
  try {
    const res = await api("POST", `/api/revisions/${rev.id}/workflow`, { action, ...body });
    state.dirty = false;
    const warn = res.result && res.result.warnings && res.result.warnings.find((w) => /not created/.test(w));
    toast(warn ? `${okMessage} (${warn})` : okMessage);
    if (res.revision && res.revision.id !== rev.id) go(`#/rev/${res.revision.id}/points`);
    else route();
  } catch (err) { showError(box, err); }
}

function actionForm(title, intro, fields, submitLabel, onSubmit, { danger = false } = {}) {
  const form = el("form", { class: "card" }, el("h3", { style: { marginTop: 0 } }, title),
    intro ? el("p", { class: "small muted" }, intro) : null, fields,
    el("div", { class: "row", style: { marginTop: "12px" } },
      el("button", { class: danger ? "danger" : "primary", type: "submit" }, submitLabel)));
  form.addEventListener("submit", (e) => { e.preventDefault(); onSubmit(form); });
  return form;
}

function documentsTable(rev) {
  return rev.documents.length ? el("table", { class: "list" },
    el("tr", {}, el("th", {}, "Created"), el("th", {}, "Status when printed"), el("th", {}, "Files")),
    rev.documents.map((d) => el("tr", {},
      el("td", {}, `${d.created_at} UTC`, el("div", { class: "small muted" }, d.created_by || "")),
      el("td", {}, STATUS_TEXT[d.status_at_print] || d.status_at_print),
      el("td", {}, el("div", { class: "row" },
        d.has_pdf ? el("a", { class: "button small", href: `/api/documents/${d.id}/pdf`, target: "_blank", rel: "noopener" }, "PDF") : null,
        el("a", { class: "button small", href: `/api/documents/${d.id}/docx` }, "Word"))))))
    : el("p", { class: "muted" }, "None yet.");
}

function scansSection(rev) {
  const kind = el("select", { name: "kind" },
    el("option", { value: "markup" }, "Marked-up review copy"),
    el("option", { value: "approval" }, "Signed approval copy"),
    el("option", { value: "other" }, "Other"));
  if (["printed_for_review", "approved", "active"].includes(rev.status)) kind.value = "approval";
  const file = el("input", { type: "file", name: "file", accept: "application/pdf,image/*", required: true });
  const form = el("form", { class: "card" }, el("h3", { style: { marginTop: 0 } }, "Upload a scan"),
    el("p", { class: "small muted" }, "Keep the paper record with this revision: a PDF scan or a photo of each page."),
    el("div", { class: "two" }, field("What is it", kind), field("File", file)),
    field("Note (optional)", text("note", "")),
    el("div", { class: "row", style: { marginTop: "12px" } }, el("button", { type: "submit" }, "Upload scan")));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!file.files.length) return;
    const fd = new FormData();
    fd.append("kind", kind.value);
    fd.append("note", form.note.value);
    fd.append("file", file.files[0], file.files[0].name);
    try { await api("POST", `/api/revisions/${rev.id}/scans`, fd); toast("Scan uploaded"); route(); }
    catch (err) { showError(form, err); }
  });
  return el("div", {},
    el("h2", {}, "Scans of the paper copies"),
    rev.scans.length ? el("table", { class: "list" },
      el("tr", {}, el("th", {}, "Uploaded"), el("th", {}, "What"), el("th", {}, "")),
      rev.scans.map((s) => el("tr", {},
        el("td", {}, `${s.created_at} UTC`, el("div", { class: "small muted" }, s.uploaded_by || "")),
        el("td", {}, s.kind_label, s.note ? el("div", { class: "small muted" }, s.note) : null),
        el("td", {}, el("a", { class: "button small", href: `/api/scans/${s.id}`, target: "_blank", rel: "noopener" }, "Open")))))
      : el("p", { class: "muted" }, "None yet."),
    form);
}

async function tabPrint(rev) {
  const blocked = await needsServer(rev);
  if (blocked) return blocked;
  rev = await data.loadRevision(rev.id);
  const box = el("div");

  if (!editor()) {  // Read-only: the Active document only
    append(box, el("h2", {}, "Current procedure"), documentsTable({ documents: rev.documents.filter((d) => d.status_at_print === "active") }));
    return box;
  }

  const genBtn = (label, primary = false) => {
    const b = el("button", { class: primary ? "primary" : "" }, label);
    b.addEventListener("click", async () => {
      b.disabled = true;
      b.textContent = "Creating… (this can take a few seconds)";
      try {
        const doc = await api("POST", `/api/revisions/${rev.id}/generate`, { pdf: true });
        toast(doc.warnings.some((w) => w.startsWith("PDF not created")) ? "Word file created; PDF conversion failed" : "Document created");
        route();
      } catch (err) { showError(box, err); b.disabled = false; }
    });
    return b;
  };

  const s = rev.status;
  if (["draft", "returned", "ready_for_review"].includes(s)) {
    const check = await api("GET", `/api/revisions/${rev.id}/check`);
    append(box,
      s === "returned" ? el("div", { class: "notice warn" }, el("strong", {}, "Returned with markups: "), rev.returned_note,
        el("div", { class: "small" }, "Make the changes, then mark it ready for review again.")) : null,
      check.blocking.length
        ? el("div", { class: "notice error" }, el("strong", {}, "Must be fixed before Ready for Review:"),
          el("ul", {}, check.blocking.map((m) => el("li", {}, m))))
        : el("div", { class: "notice ok" }, el("strong", {}, "Complete. "), "Nothing blocks this procedure from review."),
      check.warnings.length
        ? el("div", { class: "notice warn" }, el("strong", {}, "Please double-check:"),
          el("ul", {}, check.warnings.map((m) => el("li", {}, m))))
        : null,
      el("p", { class: "small muted" }, "This check only confirms that information has been entered. Trained personnel must review the printed procedure for accuracy."));
    const actions = el("div", { class: "row" });
    if (s === "ready_for_review") {
      append(actions,
        el("button", { class: "primary", onclick: (e) => {
          if (!confirm("Print the review copy? After this the procedure can't be edited until the reviewers return it.")) return;
          e.target.disabled = true; e.target.textContent = "Printing… (this can take a few seconds)";
          workflowAction(rev, "print_for_review", {}, box, "Review copy created");
        } }, "Print for review"),
        canEdit(rev) ? el("button", { onclick: async () => {
          try { await api("POST", `/api/revisions/${rev.id}/status`, { status: "draft" }); route(); }
          catch (err) { showError(box, err); }
        } }, "Back to draft") : null);
    } else {
      append(actions, genBtn("Create draft PDF (working copy)"),
        canEdit(rev) ? el("button", { class: "primary", onclick: async () => {
          try {
            await api("POST", `/api/revisions/${rev.id}/status`, { status: "ready_for_review" });
            await data.loadRevision(rev.id);  // the server releases this device's checkout
            toast("Marked ready for review"); route();
          } catch (err) { showError(box, err); }
        } }, "Mark ready for review") : null);
    }
    append(box, actions);
  }

  if (s === "printed_for_review") {
    append(box,
      el("div", { class: "notice" }, el("strong", {}, "With the reviewers. "),
        `Review copy printed ${rev.printed_for_review_at} UTC. The procedure is locked until the reviewers’ decision is recorded here.`),
      el("div", { class: "row" }, genBtn("Print the review copy again")),
      actionForm("Record approval",
        "Enter who approved the printed copy and when. Upload a scan of the signed copy below.",
        [el("div", { class: "two" },
          field("Approved by", text("approved_by", "", { required: true }), "reviewer name(s)"),
          field("Approval date", el("input", { type: "date", name: "approved_date", value: today(), max: today(), required: true }))),
        field("Note (optional)", text("note", ""))],
        "Record approval",
        (f) => workflowAction(rev, "approve", { approved_by: f.approved_by.value, approved_date: f.approved_date.value, note: f.note.value }, f, "Approval recorded")),
      actionForm("Returned with markups",
        "The reviewers want changes. Summarize them (and upload a scan of the marked-up copy). The procedure becomes editable again.",
        [field("What needs to change", area("note", "", { required: true, rows: 3 }))],
        "Return for changes",
        (f) => workflowAction(rev, "return", { note: f.note.value }, f, "Returned for changes"), { danger: true }));
  }

  if (s === "approved") {
    const hasApprovalScan = rev.scans.some((x) => x.kind === "approval");
    append(box,
      el("div", { class: "notice ok" }, el("strong", {}, "Approved "),
        `by ${rev.approved_by} on ${rev.approved_date}`, rev.approval_recorded_by ? ` (recorded by ${rev.approval_recorded_by})` : "", ".",
        rev.approval_note ? el("div", { class: "small" }, rev.approval_note) : null),
      hasApprovalScan ? null : el("div", { class: "notice warn" }, "No scan of the signed approval copy has been uploaded yet. It is strongly recommended before activating."),
      actionForm("Activate",
        rev.history.some((h) => h.status === "active")
          ? "Makes this revision the procedure in use and prints the final copy (no DRAFT watermark). The current Active revision becomes Superseded; replace posted copies."
          : "Makes this revision the procedure in use and prints the final copy (no DRAFT watermark).",
        [field("Effective date", el("input", { type: "date", name: "effective_date", value: rev.effective_date || today(), required: true }),
          "printed in the footer; review date defaults to 1 year later")],
        "Activate",
        (f) => {
          if (!confirm("Activate this revision? It becomes the procedure in use.")) return;
          workflowAction(rev, "activate", { effective_date: f.effective_date.value }, f, "Activated");
        }),
      el("div", { class: "row" }, el("button", { class: "small", onclick: () => {
        if (confirm("Withdraw the recorded approval (back to Printed for review)?")) workflowAction(rev, "withdraw_approval", {}, box, "Approval withdrawn");
      } }, "Withdraw approval (recorded by mistake)")));
  }

  if (s === "active") {
    append(box,
      el("div", { class: "notice ok" }, el("strong", {}, "Active. "),
        `Effective ${rev.effective_date}. Approved by ${rev.approved_by || "?"} on ${rev.approved_date || "?"}. `,
        rev.next_review_due ? el("strong", {}, `Next review due ${rev.next_review_due}.`) : null),
      el("div", { class: "row" }, genBtn("Print the final copy again", true)),
      rev.open_revision_id && rev.open_revision_id !== rev.id
        ? el("div", { class: "notice" }, "A new revision is in progress: ", el("a", { href: `#/rev/${rev.open_revision_id}/points` }, "open it"), ".")
        : actionForm("Start a new revision",
          `Creates V${String(rev.rev_no + 1).padStart(2, "0")} as a draft copy of this one. This revision stays Active until the new one is approved and activated.`,
          [field("Reason for the change", area("reason", rev.flag ? rev.flag.reason : "", { required: true, rows: 2 }), "e.g. machine modified, reviewer findings")],
          "Start new revision",
          (f) => workflowAction(rev, "new_revision", { reason: f.reason.value }, f, "New revision started")),
      actionForm("Record periodic review (no changes)",
        "The annual inspection of this procedure found nothing to change. Moves the next due date to one year after this review. If anything must change, start a new revision instead.",
        [el("div", { class: "two" },
          field("Review date", el("input", { type: "date", name: "inspected_date", value: today(), max: today(), required: true })),
          field("Reviewed by", text("inspector", "", { required: true }))),
        field("Note (optional)", text("note", ""))],
        "Record periodic review",
        (f) => workflowAction(rev, "periodic_review", { inspected_date: f.inspected_date.value, inspector: f.inspector.value, note: f.note.value }, f, "Periodic review recorded")),
      rev.flag
        ? el("div", { class: "row" }, el("button", { onclick: () => workflowAction(rev, "clear_flag", {}, box, "Flag cleared") }, "Clear the revision flag"))
        : actionForm("Flag for revision",
          "The machine or its energy sources changed (guard added, new air drop…). Shows on the dashboard until a new revision is activated.",
          [field("What changed", text("reason", "", { required: true }))],
          "Flag",
          (f) => workflowAction(rev, "flag", { reason: f.reason.value }, f, "Flagged for revision"), { danger: true }));
  }

  if (s === "superseded") {
    append(box, el("div", { class: "notice" }, el("strong", {}, "Superseded "), `on ${rev.superseded_at} UTC. `,
      rev.active_revision_id ? el("a", { href: `#/rev/${rev.active_revision_id}` }, "Go to the Active revision") : null));
  }

  append(box, el("h2", {}, "Created documents"), documentsTable(rev), scansSection(rev));
  return box;
}

// ------------------------------------------------------------------ history tab

function eventText(e) {
  let d = e.detail;
  try { d = JSON.parse(e.detail); } catch { /* plain text */ }
  const st = (x) => STATUS_TEXT[x] || x;
  switch (e.action) {
    case "status":
      if (Array.isArray(d)) return `Status: ${st(d[0])} → ${st(d[1])}`;
      return `Status → ${st(d.to)}${d.approved_by ? ` (approved by ${d.approved_by}, ${d.approved_date})` : ""}${d.note ? `: ${d.note}` : ""}${d.effective_date ? `, effective ${d.effective_date}` : ""}`;
    case "generate": return "Document printed";
    case "scan": return `Scan uploaded (${d.kind})`;
    case "create": return d.reason ? `Revision started: ${d.reason}` : "Created";
    case "checkout": return `Checked out to ${d || "a device"}`;
    case "checkout_take_over": return `Taken over from ${d.from_device || "another device"}`;
    case "release": return d.forced ? "Checkout released (forced)" : "Checkout released";
    case "flag": return `Flagged for revision: ${d}`;
    case "unflag": return "Revision flag cleared";
    case "periodic_review": return `Periodic review by ${d.inspector} (${d.date})`;
    case "copied_from": return `Copied from ${d.source}`;
    case "source_change_reviewed": return "Source revision reviewed – no change needed";
    case "update": return "Details edited";
    case "reorder_points": return "Isolation points reordered";
    default: return e.action.replace(/_/g, " ");
  }
}

async function tabHistory(rev) {
  if (!data.status.online) return el("div", { class: "notice warn" }, "History needs a connection.");
  rev = await data.loadRevision(rev.id);
  return el("div", {},
    el("h2", {}, "Revisions"),
    el("table", { class: "list" },
      el("tr", {}, el("th", {}, "Revision"), el("th", {}, "Status"), el("th", {}, "Effective"), el("th", {}, "Approved"), el("th", {}, "Reason")),
      rev.history.map((h) => el("tr", {},
        el("td", {}, h.id === rev.id ? el("strong", {}, h.procedure_number) : el("a", { href: `#/rev/${h.id}/history` }, h.procedure_number)),
        el("td", {}, statusBadge(h.status)),
        el("td", {}, h.effective_date || ""),
        el("td", { class: "small" }, h.approved_by ? `${h.approved_by}, ${h.approved_date}` : ""),
        el("td", { class: "small" }, h.change_reason || "")))),
    el("h2", {}, "Periodic reviews"),
    rev.inspections.length ? el("table", { class: "list" },
      el("tr", {}, el("th", {}, "Date"), el("th", {}, "Reviewed by"), el("th", {}, "Note")),
      rev.inspections.map((i) => el("tr", {}, el("td", {}, i.inspected_date), el("td", {}, i.inspector), el("td", { class: "small" }, i.note))))
      : el("p", { class: "muted" }, "None recorded."),
    el("h2", {}, "Activity"),
    rev.events.length ? el("table", { class: "list" },
      el("tr", {}, el("th", {}, "When (UTC)"), el("th", {}, "Who"), el("th", {}, "What")),
      rev.events.map((e) => el("tr", {}, el("td", { class: "small" }, e.at), el("td", { class: "small" }, e.who || "system"),
        el("td", { class: "small" }, eventText(e)))))
      : el("p", { class: "muted" }, "No activity."));
}

// ------------------------------------------------------------------ dashboard

async function viewDashboard() {
  const d = await api("GET", "/api/dashboard");
  const link = (r, extra) => el("a", { class: "card link", href: `#/rev/${r.revision_id}` },
    el("div", { class: "spread" }, el("strong", {}, r.procedure_number), r.status ? statusBadge(r.status) : null),
    r.asset_number ? el("div", {}, `${r.asset_number} – ${r.machine_type}`, el("span", { class: "muted" }, `  ·  ${r.dept_name}`)) : null,
    extra ? el("div", { class: "small" }, extra) : null);
  const section = (title, items, render, empty = "None.") => [
    el("h2", {}, `${title} (${items.length})`),
    items.length ? items.map(render) : el("p", { class: "muted" }, empty)];
  const tile = (n, label, cls = "") => el("div", { class: `tile ${cls}` }, el("div", { class: "n" }, n), el("div", { class: "small" }, label));
  const box = el("div", {}, el("h1", {}, "Dashboard"));
  append(box,
    el("div", { class: "tiles" },
      tile(d.overdue.length, "reviews overdue", d.overdue.length ? "bad" : ""),
      tile(d.due_soon.length, "due in 60 days", d.due_soon.length ? "warn" : ""),
      tile(d.flagged.length, "flagged for revision", d.flagged.length ? "warn" : ""),
      tile(d.counts.active, "active procedures"),
      editor() ? tile(d.counts.printed_for_review + d.counts.approved, "with reviewers / to activate") : null),
    section("Periodic review overdue", d.overdue, (r) => link(r, `Due ${r.next_review_due}`), "Nothing overdue."),
    section("Review due within 60 days", d.due_soon, (r) => link(r, `Due ${r.next_review_due}`)),
    section("Flagged for revision", d.flagged, (r) => link(r, r.flag_reason)));
  if (editor()) {
    const stages = [["printed_for_review", "With the reviewers"], ["approved", "Approved – waiting to be activated"],
      ["returned", "Returned with markups"], ["ready_for_review", "Ready to print for review"], ["draft", "Drafts"]];
    const busy = stages.filter(([s]) => d.in_progress[s].length);
    append(box, el("h2", {}, "In progress"), busy.length ? null : el("p", { class: "muted" }, "Nothing in progress."));
    for (const [s, label] of busy) {
      append(box, el("h3", {}, `${label} (${d.in_progress[s].length})`),
        d.in_progress[s].map((r) => link(r, `Last change ${r.updated_at} UTC`)));
    }
    append(box,
      section("Copies whose original was revised", d.source_revised,
        (c) => el("a", { class: "card link", href: `#/rev/${c.revision_id}` }, el("strong", {}, c.procedure),
          el("div", { class: "small" }, `Copied from a procedure now at ${c.source}. Check whether the same change applies.`))),
      section("Machines without a procedure", d.no_procedure,
        (a) => el("a", { class: "card link", href: `#/new?asset=${a.id}` }, el("strong", {}, `${a.asset_number} – ${a.machine_type}`),
          el("span", { class: "muted" }, `  ·  ${a.dept_name}`))),
      section("Checked out to devices", d.checked_out,
        (c) => link(c, `${c.user_name} on ${c.checkout_device_name} since ${c.checkout_at} UTC`)));
  }
  render(box);
}

// ------------------------------------------------------------------ copy tab

async function tabCopy(rev) {
  const blocked = await needsServer(rev);
  if (blocked) return blocked;
  const free = await api("GET", "/api/assets?without_procedure=1");
  const form = el("form");
  const newRows = el("div");
  const addRow = () => {
    const row = el("div", { class: "card" },
      el("div", { class: "spread" }, el("strong", {}, "New machine"),
        el("button", { type: "button", class: "small", onclick: () => row.remove() }, "Remove")),
      el("div", { class: "two" },
        field("Asset #", text("n_asset_number", "", { autocapitalize: "characters" })),
        field("Machine type", text("n_machine_type", rev.machine_type))),
      field("Department", departmentSelect("n_department_id", rev.department_id)));
    row.dataset.newMachine = "1";
    newRows.append(row);
    row.querySelector("input").focus();
  };
  append(form,
    el("p", {}, "Copy this procedure to similar machines. Each copy starts as a new V00 draft. ",
      el("strong", {}, "Every copied isolation point must be checked at the new machine"),
      " (retake or confirm the photo and check the details) before the copy can be marked ready for review."),
    el("p", { class: "small muted" }, `The asset # ${rev.asset_number} is replaced with the new asset # wherever it appears in the text. Other numbers (breakers, panels, valve tags) are highlighted for checking but not changed. Photo markup is copied too.`),
    el("h2", {}, "Copy to"),
    free.length ? el("div", {}, free.map((a) => el("label", { class: "check" },
      el("input", { type: "checkbox", name: "asset", value: a.id }), `${a.asset_number} – ${a.machine_type} (${a.dept_code})`)))
      : el("p", { class: "muted small" }, "No existing machines without a procedure."),
    newRows,
    el("button", { type: "button", onclick: addRow }, "+ Add a new machine"),
    el("h2", {}, "What to copy"),
    el("label", { class: "check" }, el("input", { type: "checkbox", name: "inc_description", checked: true }), "Description and procedure steps (tasks, roles, devices, restart…)"),
    el("label", { class: "check" }, el("input", { type: "checkbox", name: "inc_points", checked: true }), `Isolation points (${rev.points.length})`),
    el("label", { class: "check" }, el("input", { type: "checkbox", name: "inc_photos", checked: true }), "Photos (must be confirmed or retaken at each machine)"),
    el("div", { class: "sticky-actions" }, el("div", { class: "inner" }, el("button", { class: "primary", type: "submit" }, "Copy"))));
  form.inc_points.addEventListener("change", () => {
    form.inc_photos.disabled = !form.inc_points.checked;
    if (!form.inc_points.checked) form.inc_photos.checked = false;
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const targets = [...form.querySelectorAll("input[name=asset]:checked")].map((c) => ({ asset_id: Number(c.value) }));
    for (const row of newRows.querySelectorAll("[data-new-machine]")) {
      const v = (n) => row.querySelector(`[name=${n}]`).value;
      targets.push({ asset_number: v("n_asset_number"), machine_type: v("n_machine_type"), department_id: v("n_department_id") });
    }
    if (!targets.length) return showError(form, new Error("Choose or add at least one machine."));
    try {
      const created = await api("POST", `/api/revisions/${rev.id}/copy`, { targets, include: {
        description: form.inc_description.checked, points: form.inc_points.checked, photos: form.inc_photos.checked } });
      state.dirty = false;
      render(...revHeader(rev, "copy"),
        el("div", { class: "notice ok" }, el("strong", {}, `Created ${created.length} draft${created.length > 1 ? "s" : ""}. `),
          "Open each one at its machine to check the copied points."),
        created.map((c) => el("a", { class: "card link", href: `#/rev/${c.revision_id}/points` }, el("strong", {}, c.procedure_number))));
    } catch (err) { showError(form, err); }
  });
  markDirty(form);
  return form;
}

// ------------------------------------------------------------------ add point (energy picker)

function energyButtons(selected, onPick) {
  return el("div", { class: "energy-grid" }, state.config.energy_types.map((t) =>
    el("button", { type: "button", class: `energy-btn ${t.code === selected ? "selected" : ""}`, style: energyStyle(t),
      "aria-pressed": t.code === selected ? "true" : "false", onclick: () => onPick(t.code) },
      t.name, t.also_covers ? el("span", { class: "covers" }, `also: ${t.also_covers}`) : null)));
}

async function viewAddPoint(rid) {
  const rev = await data.loadRevision(rid);
  const box = el("div");
  append(box,
    el("p", {}, el("a", { href: `#/rev/${rid}/points` }, "← Back to isolation points")),
    el("h1", {}, "New isolation point"),
    el("p", {}, `Point ${rev.points.length + 1} for ${rev.procedure_number}. Choose the energy type:`),
    energyButtons(null, async (code) => {
      try {
        const { id } = await change(rid, { type: "point.add", temp_id: data.tempId(), fields: { energy_type: code } });
        go(`#/rev/${rid}/point/${id}`);
      } catch (err) { showError(box, err); }
    }));
  render(box);
}

// ------------------------------------------------------------------ point editor

function highlight(textValue, tokens) {
  if (!tokens || !tokens.length || !textValue) return null;
  const found = tokens.filter((t) => textValue.includes(t));
  return found.length ? el("div", { class: "small" }, "Check: ", found.map((t) => el("span", { class: "mark" }, t, " "))) : null;
}

async function imageSize(file) {
  try {
    const bmp = await createImageBitmap(file);  // applies the photo's orientation
    const size = { width: bmp.width, height: bmp.height };
    bmp.close();
    return size;
  } catch {
    return { width: 1000, height: 750 };
  }
}

async function viewPoint(rid, pidParam) {
  let rev = await data.loadRevision(rid);
  const want = await data.realId(pidParam);
  let p = rev.points.find((x) => x.id === pidParam || x.id === want);
  if (!p) throw new Error("This isolation point no longer exists.");
  if (!canEdit(rev)) { go(`#/rev/${rid}/points`); return; }
  const pid = p.id;
  let energy = p.energy_type;
  let version = p.version;
  const flags = p.copy_flags || {};
  const tokens = flags.check_tokens || [];

  const form = el("form");
  const energyBox = el("div");
  const unitBox = el("div", { class: "row" });
  const drawEnergy = () => {
    energyBox.replaceChildren(energyButtons(energy, (code) => { energy = code; state.dirty = true; drawEnergy(); }));
    const et = state.energy[energy];
    unitBox.replaceChildren(...(et ? et.units : []).map((u) => el("button", { type: "button", class: "small",
      onclick: () => { const m = form.magnitude; if (!m.value.includes(u)) m.value = `${m.value.trim()} ${u}`.trim(); m.focus(); state.dirty = true; } }, u)));
  };

  const txt = (name, label, hint, rows = 2) => {
    const t = area(name, p[name], { rows });
    return el("div", {}, field(label, t, hint), highlight(p[name], tokens));
  };

  // Photos
  const photosBox = el("div", { class: "photos" });
  const camera = el("input", { type: "file", accept: "image/*", capture: "environment", class: "hidden-input", "aria-label": "Take photo" });
  const picker = el("input", { type: "file", accept: "image/*,.heic,.heif", multiple: true, class: "hidden-input", "aria-label": "Choose photos" });
  const uploadStatus = el("div", { class: "small muted", "aria-live": "polite" });

  async function refreshPhotos() {
    rev = await data.loadRevision(rid);
    const real = await data.realId(pid);
    const fp = rev.points.find((x) => x.id === pid || x.id === real);
    if (fp) { p.photos = fp.photos; version = fp.version; }  // keep typed text; photo changes bump the version
    await drawPhotos();
  }
  async function photoAction(op) {
    try { await change(rid, op); await refreshPhotos(); } catch (err) { showError(form, err); }
  }
  async function drawPhotos() {
    const cards = [];
    for (const [i, ph] of p.photos.entries()) {
      const src = await photoSrc(ph);
      const others = p.photos.filter((o) => o !== ph && o.annotations && o.annotations.length)
        .map((o) => ({ name: `photo ${p.photos.indexOf(o) + 1}`, annotations: o.annotations }));
      cards.push(el("div", { class: "photo" },
        src ? photoFigure(src, ph, `Photo ${i + 1} of ${p.label}`) : el("div", { class: "figure muted small" }, "Uploading…"),
        ph.copied_from_photo_id ? el("span", { class: "tag" }, "Copied – confirm or retake") : null,
        ph.local ? el("span", { class: "tag" }, "Not uploaded yet") : null,
        el("div", { class: "photo-actions" },
          el("button", { type: "button", class: "small", disabled: !src, onclick: async () => {
            const shapes = await editMarkup({ src, photo: ph, label: p.label, others });
            if (shapes) { await photoAction({ type: "photo.annotate", photo: ph.id, annotations: shapes }); toast("Markup saved"); }
          } }, "✎ Mark up"),
          el("button", { type: "button", class: "small icon", title: "Move earlier", "aria-label": "Move photo earlier", disabled: i === 0,
            onclick: () => photoAction({ type: "photo.move", photo: ph.id, direction: "up" }) }, "◀"),
          el("button", { type: "button", class: "small icon", title: "Move later", "aria-label": "Move photo later", disabled: i === p.photos.length - 1,
            onclick: () => photoAction({ type: "photo.move", photo: ph.id, direction: "down" }) }, "▶"),
          el("button", { type: "button", class: "small danger", onclick: async () => {
            if (!confirm("Remove this photo from the point?")) return;
            await photoAction({ type: "photo.delete", photo: ph.id });
          } }, "Remove"))));
    }
    photosBox.replaceChildren(...cards);
    if (p.photos.length > 2) photosBox.append(el("div", { class: "notice warn small" }, "Only the first 2 photos are printed."));
  }
  async function upload(files) {
    for (const [n, f] of [...files].entries()) {
      uploadStatus.textContent = `Saving photo ${n + 1} of ${files.length}…`;
      try {
        const size = await imageSize(f);
        await change(rid, { type: "photo.add", point: pid, temp_id: data.tempId(), filename: f.name || "photo.jpg",
          width: size.width, height: size.height, annotations: [] }, f);
      } catch (err) { showError(form, err); }
    }
    uploadStatus.textContent = "";
    camera.value = ""; picker.value = "";
    await refreshPhotos();
    toast("Photo added");
  }
  camera.addEventListener("change", () => camera.files.length && upload(camera.files));
  picker.addEventListener("change", () => picker.files.length && upload(picker.files));

  async function save() {
    const locks = Number(form.locks.value || 0);
    const fields = {
      energy_type: energy,
      label: form.label.value.trim(), magnitude: form.magnitude.value, device: form.device.value,
      deenergize: form.deenergize.value, lock: form.lock.value, stored_energy: form.stored_energy.value,
      verify: form.verify.value, notes: form.notes.value, locks,
      photo_missing_reason: form.photo_missing_reason.value,
    };
    await change(rid, { type: "point.update", point: pid, fields, version });
    state.dirty = false;
  }

  const checkBanner = p.copy_status === "needs_check" ? el("div", { class: "notice warn" },
    el("strong", {}, "Copied from another machine – check at this machine."),
    el("ul", {},
      el("li", {}, `Source point: ${flags.source_point || "?"}`),
      flags.replaced_fields && flags.replaced_fields.length ? el("li", {}, `Asset # replaced in: ${flags.replaced_fields.join(", ")}`) : null,
      tokens.length ? el("li", {}, "Numbers to check: ", tokens.map((t) => el("span", { class: "mark" }, t, " "))) : null,
      el("li", {}, "Retake the photo, or confirm the copied photo shows this machine’s device.")),
    el("div", { class: "row" },
      el("button", { type: "button", class: "primary", onclick: async () => {
        const copied = p.photos.some((ph) => ph.copied_from_photo_id);
        const msg = copied
          ? `Confirm that the copied photo(s) and all details are correct for ${rev.asset_number}?`
          : `Confirm that all details are correct for ${rev.asset_number}?`;
        if (!confirm(msg)) return;
        try {
          if (state.dirty) await save();
          await change(rid, { type: "point.check", point: pid });
          toast("Point checked");
          go(`#/rev/${rid}/points`);
        } catch (err) { showError(form, err); }
      } }, "Mark checked"))) : null;

  append(form,
    el("p", {}, el("a", { href: `#/rev/${rid}/points` }, "← Back to isolation points")),
    el("h1", {}, `Isolation point ${p.label}`),
    checkBanner,
    el("h2", {}, "Energy type"), energyBox,
    el("div", { class: "two" },
      field("Point label", text("label", p.label, { required: true }), "e.g. E-1 (tag on the machine)"),
      field("Locks at this point", el("input", { type: "number", name: "locks", min: 0, max: 50, value: p.locks, inputmode: "numeric" }))),
    field("Magnitude", text("magnitude", p.magnitude), "e.g. 480 VAC 3-phase, 95 PSI, or “Unknown – verify”"),
    unitBox,
    el("h2", {}, "Photos"),
    el("p", { class: "small muted" }, "Photograph the isolating device from a safe position. Avoid people’s faces. Two photos print per point (e.g. wide + close-up). Use ✎ Mark up to point at the device."),
    photosBox,
    el("div", { class: "row" },
      el("button", { type: "button", class: "primary big", onclick: () => camera.click() }, "📷 Take photo"),
      el("button", { type: "button", class: "big", onclick: () => picker.click() }, "Choose from device"),
      camera, picker),
    uploadStatus,
    field("If no photo: reason", text("photo_missing_reason", p.photo_missing_reason), "e.g. camera not allowed in area, take at next shutdown"),
    el("h2", {}, "Energy control information"),
    txt("device", "Isolation device & location", "what and where"),
    txt("deenergize", "De-energize", "how to shut off this source"),
    txt("lock", "Lock", "device and where the lock goes"),
    txt("stored_energy", "Stored energy", "release or restrain (bleed, block, discharge, wait)"),
    txt("verify", "Verify zero energy", "how to confirm"),
    txt("notes", "Note", "optional"),
    el("div", { class: "sticky-actions" }, el("div", { class: "inner" },
      el("button", { type: "button", class: "danger", onclick: async () => {
        if (!confirm(`Delete isolation point ${p.label}? Its photos are removed from this procedure.`)) return;
        try { await change(rid, { type: "point.delete", point: pid }); state.dirty = false; toast("Point deleted"); go(`#/rev/${rid}/points`); }
        catch (err) { showError(form, err); }
      } }, "Delete"),
      el("button", { class: "primary", type: "submit" }, "Save point"))));

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    try { await save(); toast("Point saved"); go(`#/rev/${rid}/points`); }
    catch (err) { showError(form, err); }
  });
  markDirty(form);
  drawEnergy();
  await drawPhotos();
  render(form);
}

// ------------------------------------------------------------------ upload status

async function viewSync() {
  const pending = await data.pendingOps();
  const probs = await data.problems();
  const lost = data.status.lost;
  const box = el("div");
  append(box,
    el("h1", {}, "Upload status"),
    el("p", {}, data.status.online ? "Connected to the server." : "Offline – changes are saved on this device and upload when connected.",
      data.status.lastSync ? ` Last upload: ${data.status.lastSync.toLocaleTimeString()}.` : ""),
    el("div", { class: "row" }, el("button", { class: "primary", onclick: async () => { await data.syncNow(); route(); } }, "Upload now")),
    el("h2", {}, `Waiting to upload (${pending.length})`),
    pending.length ? el("table", { class: "list" },
      el("tr", {}, el("th", {}, "Procedure"), el("th", {}, "Change"), el("th", {}, "")),
      pending.map((o) => el("tr", {}, el("td", {}, el("a", { href: `#/rev/${o.rid}` }, `#${o.rid}`)),
        el("td", {}, OP_TEXT[o.type] || o.type),
        el("td", { class: "small" }, lost[o.rid] ? "Blocked – checked out elsewhere" : ""))))
      : el("p", { class: "muted" }, "Nothing waiting."),
    el("h2", {}, `Problems (${probs.length})`),
    el("p", { class: "small muted" }, "Changes the server refused. Re-enter them if they are still needed."),
    probs.length ? [el("table", { class: "list" },
      el("tr", {}, el("th", {}, "When"), el("th", {}, "Change"), el("th", {}, "Reason")),
      probs.map((pr) => el("tr", {}, el("td", { class: "small" }, new Date(pr.at).toLocaleString()),
        el("td", {}, el("a", { href: `#/rev/${pr.rid}` }, OP_TEXT[pr.op.type] || pr.op.type),
          el("div", { class: "small muted" }, JSON.stringify(pr.op.fields || pr.op.annotations || "").slice(0, 160))),
        el("td", {}, pr.error)))),
    el("div", { class: "row", style: { marginTop: "8px" } },
      el("button", { onclick: async () => { if (confirm("Clear the problem list?")) { await data.clearProblems(); route(); } } }, "Clear list"))]
      : el("p", { class: "muted" }, "None."));
  render(box);
}

// ------------------------------------------------------------------ admin

async function viewAdmin() {
  const users = await api("GET", "/api/admin/users");
  const roleSel = () => el("select", {},
    ["admin", "full", "read_only"].map((r) => el("option", { value: r }, { admin: "Admin", full: "Full access", read_only: "Read-only" }[r])));
  const box = el("div");
  const userRows = users.map((u) => {
    const sel = roleSel();
    sel.value = u.role;
    const act = el("input", { type: "checkbox", checked: !!u.active, "aria-label": `Active: ${u.username}` });
    const save = async () => {
      try {
        await api("PUT", `/api/admin/users/${u.id}`, { role: sel.value, active: act.checked });
        u.role = sel.value; u.active = act.checked;
        toast("User updated");
      } catch (err) {
        sel.value = u.role; act.checked = !!u.active;  // put the controls back
        showError(box, err);
      }
    };
    sel.addEventListener("change", save);
    act.addEventListener("change", save);
    return el("tr", {},
      el("td", {}, el("strong", {}, u.username), el("div", { class: "small muted" }, u.display_name),
        u.must_change_password ? el("div", { class: "small muted" }, "Must change password") : null),
      el("td", {}, sel),
      el("td", {}, act),
      el("td", { class: "small" }, u.last_login ? `${u.last_login} UTC` : "Never"),
      el("td", {}, el("button", { class: "small", onclick: async () => {
        const temp = prompt(`Temporary password for ${u.username} (at least 8 characters). They must change it at sign-in.`);
        if (!temp) return;
        try { await api("POST", `/api/admin/users/${u.id}/reset-password`, { temp_password: temp }); toast("Password reset"); }
        catch (err) { showError(box, err); }
      } }, "Reset password")));
  });

  const addUser = el("form", { class: "card" }, el("h3", { style: { marginTop: 0 } }, "Add user"),
    el("div", { class: "two" },
      field("User name", text("username", "", { required: true, autocapitalize: "none" })),
      field("Full name", text("display_name", ""))),
    el("div", { class: "two" },
      field("Access", (() => { const s = roleSel(); s.name = "role"; s.value = "full"; return s; })()),
      field("Temporary password", text("temp_password", "", { required: true, minlength: 8 }), "they change it at first sign-in")),
    el("div", { class: "row", style: { marginTop: "12px" } }, el("button", { class: "primary", type: "submit" }, "Add user")));
  addUser.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("POST", "/api/admin/users", { username: addUser.username.value, display_name: addUser.display_name.value,
        role: addUser.role.value, temp_password: addUser.temp_password.value });
      toast("User added"); route();
    } catch (err) { showError(addUser, err); }
  });

  const addSite = el("form", { class: "card" }, el("h3", { style: { marginTop: 0 } }, "Add site"),
    el("div", { class: "two" }, field("Code", text("code", "", { required: true })), field("Name", text("name", "", { required: true }))),
    el("div", { class: "row", style: { marginTop: "12px" } }, el("button", { type: "submit" }, "Add site")));
  addSite.addEventListener("submit", async (e) => {
    e.preventDefault();
    try { await api("POST", "/api/admin/sites", { code: addSite.code.value, name: addSite.name.value }); await loadConfig(); toast("Site added"); route(); }
    catch (err) { showError(addSite, err); }
  });
  const siteSel = el("select", { name: "site_id" }, state.config.sites.map((s) => el("option", { value: s.id }, `${s.code} – ${s.name}`)));
  const addDept = el("form", { class: "card" }, el("h3", { style: { marginTop: 0 } }, "Add department"),
    field("Site", siteSel),
    el("div", { class: "two" }, field("Code", text("code", "", { required: true })), field("Name", text("name", "", { required: true }))),
    el("div", { class: "row", style: { marginTop: "12px" } }, el("button", { type: "submit" }, "Add department")));
  addDept.addEventListener("submit", async (e) => {
    e.preventDefault();
    try { await api("POST", "/api/admin/departments", { site_id: Number(siteSel.value), code: addDept.code.value, name: addDept.name.value }); await loadConfig(); toast("Department added"); route(); }
    catch (err) { showError(addDept, err); }
  });

  append(box,
    el("h1", {}, "Admin"),
    el("h2", {}, "Users"),
    el("p", { class: "small muted" }, "Accounts are deactivated, not deleted, so the history keeps who did what. There must always be one active Admin."),
    el("div", { style: { overflowX: "auto" } }, el("table", { class: "list" },
      el("tr", {}, el("th", {}, "User"), el("th", {}, "Access"), el("th", {}, "Active"), el("th", {}, "Last sign-in"), el("th", {}, "")),
      userRows)),
    addUser,
    el("h2", {}, "Sites and departments"),
    el("table", { class: "list" }, el("tr", {}, el("th", {}, "Site"), el("th", {}, "Departments")),
      state.config.sites.map((s) => el("tr", {}, el("td", {}, `${s.code} – ${s.name}`),
        el("td", {}, state.config.departments.filter((d) => d.site_id === s.id).map((d) => `${d.code} – ${d.name}`).join(", "))))),
    el("div", { class: "two" }, addSite, addDept));
  render(box);
}

route();
