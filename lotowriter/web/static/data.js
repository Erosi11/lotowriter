// Data layer: talks to the server, and keeps checked-out procedures working offline.
//
// A procedure "checked out" to this device has a working copy in IndexedDB. Every change to it
// is applied to the working copy immediately and queued in the outbox; the queue uploads in
// order whenever the server is reachable. Procedures that aren't checked out are edited
// directly on the server (online only).
import { idb } from "./idb.js";

// ------------------------------------------------------------------ device identity

function makeId() {
  return (crypto.randomUUID ? crypto.randomUUID() : `${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`);
}

export const device = (() => {
  let id = null;
  try { id = localStorage.getItem("lw.device"); } catch { /* private mode */ }
  if (!id || !/^[A-Za-z0-9-]{8,64}$/.test(id)) {
    id = makeId();
    try { localStorage.setItem("lw.device", id); } catch { /* ignore */ }
  }
  const ua = navigator.userAgent;
  const name = /iPad/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1) ? "iPad"
    : /iPhone/.test(ua) ? "iPhone"
      : /Android/.test(ua) ? (/Mobile/.test(ua) ? "Android phone" : "Android tablet")
        : /Windows/.test(ua) ? "Windows PC" : /Mac/.test(ua) ? "Mac" : /Linux/.test(ua) ? "Linux PC" : "Device";
  const touch = window.matchMedia && window.matchMedia("(pointer: coarse)").matches;
  return { id, name, touch };
})();

export const tempId = () => `t-${makeId()}`;

// ------------------------------------------------------------------ status (online / pending / syncing)

const listeners = new Set();
export const status = { online: true, syncing: false, pending: 0, problems: 0, lost: {}, lastSync: null };

export function onStatus(fn) { listeners.add(fn); fn(status); return () => listeners.delete(fn); }

async function emit() {
  status.pending = await idb.count("outbox");
  status.problems = await idb.count("problems");
  status.lost = (await idb.get("meta", "lost")) || {};
  listeners.forEach((fn) => fn(status));
}

function setOnline(v) {
  if (status.online !== v) { status.online = v; emit(); }
}

// ------------------------------------------------------------------ HTTP

export class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}
export class OfflineError extends Error {
  constructor() { super("You're offline. This needs a connection to the server."); this.offline = true; }
}

export async function api(method, path, body) {
  const headers = { "X-Requested-With": "LotoWriter", "X-Device-Id": device.id, "X-Device-Name": device.name };
  const opts = { method, headers, credentials: "same-origin" };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch {
    setOnline(false);
    throw new OfflineError();
  }
  setOnline(true);
  let data = null;
  try { data = await res.json(); } catch { /* empty */ }
  if (!res.ok) {
    const msg = (data && data.detail) || `Request failed (${res.status})`;
    throw new ApiError(res.status, typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return data;
}

// ------------------------------------------------------------------ cached session data

export async function getMe() {
  try {
    const me = await api("GET", "/api/me");
    await idb.put("meta", me, "me");
    return me;
  } catch (err) {
    if (err.offline) {
      const me = await idb.get("meta", "me");
      if (me) return me;
    }
    throw err;
  }
}

export async function getConfig() {
  try {
    const cfg = await api("GET", "/api/config");
    await idb.put("meta", cfg, "config");
    return cfg;
  } catch (err) {
    const cfg = err.offline ? await idb.get("meta", "config") : null;
    if (cfg) return cfg;
    throw err;
  }
}

export async function forgetSession() {
  // Shared devices: remove this user's offline data on sign-out.
  for (const s of ["revisions", "outbox", "blobs", "problems"]) await idb.clear(s);
  for (const k of ["me", "idmap", "lost"]) await idb.delete("meta", k);
  if (window.caches) await caches.delete("lw-photos");
  await emit();
}

// ------------------------------------------------------------------ serialised local writes

let chain = Promise.resolve();
function exclusive(fn) {
  const run = chain.then(fn, fn);
  chain = run.catch(() => {});
  return run;
}

async function idMap() { return (await idb.get("meta", "idmap")) || {}; }
export async function realId(id) { return (await idMap())[id] || id; }

async function pendingFor(rid) { return (await idb.getAllByIndex("outbox", "rid", rid)).length; }

// ------------------------------------------------------------------ procedures

export async function listProcedures(query) {
  try {
    return { rows: await api("GET", `/api/procedures?${query}`), offline: false };
  } catch (err) {
    if (!err.offline) throw err;
    const revs = await idb.getAll("revisions");
    return {
      offline: true,
      rows: revs.map((r) => ({
        revision_id: r.id, procedure_number: r.procedure_number, status: r.status, asset_number: r.asset_number,
        machine_type: r.machine_type, dept_name: r.dept_name, point_count: r.points.length,
        needs_check: r.points.filter((p) => p.copy_status === "needs_check").length,
      })),
    };
  }
}

function isMine(rev) { return !!(rev && rev.checkout && rev.checkout.mine); }

export async function loadRevision(rid) {
  const local = await idb.get("revisions", rid);
  if (local && (await pendingFor(rid)) > 0) {
    syncSoon();
    return { ...local, _local: true };
  }
  try {
    const rev = await api("GET", `/api/revisions/${rid}`);
    if (isMine(rev)) await exclusive(() => idb.put("revisions", rev));
    else if (local) await idb.delete("revisions", rid);  // released or taken over elsewhere
    return rev;
  } catch (err) {
    if (err.offline && local) return { ...local, _local: true };
    throw err;
  }
}

async function prefetchPhotos(rev) {
  for (const p of rev.points) {
    for (const ph of p.photos) {
      if (ph.file_id) fetch(`/api/photo-files/${ph.file_id}`, { credentials: "same-origin" }).catch(() => {});
    }
  }
}

export async function checkout(rid, takeOver = false) {
  const rev = await api("POST", `/api/revisions/${rid}/checkout`, { device_name: device.name, take_over: takeOver });
  await exclusive(() => idb.put("revisions", rev));
  const lost = (await idb.get("meta", "lost")) || {};
  if (lost[rid]) { delete lost[rid]; await idb.put("meta", lost, "lost"); }
  prefetchPhotos(rev);
  await emit();
  return rev;
}

export async function release(rid, force = false) {
  if (!force) {
    await syncNow();
    if ((await pendingFor(rid)) > 0) throw new Error("Some changes haven't uploaded yet. Connect to the network and try again.");
  }
  await api("POST", `/api/revisions/${rid}/release`, { force });
  await exclusive(() => idb.delete("revisions", rid));
  await emit();
}

export async function discardLocal(rid) {
  // After a take-over elsewhere: throw away this device's unsent changes for the procedure.
  await exclusive(async () => {
    for (const op of await idb.getAllByIndex("outbox", "rid", rid)) {
      await idb.delete("outbox", op.seq);
      if (op.type === "photo.add") await idb.delete("blobs", op.temp_id);
    }
    await idb.delete("revisions", rid);
    const lost = (await idb.get("meta", "lost")) || {};
    delete lost[rid];
    await idb.put("meta", lost, "lost");
  });
  await emit();
}

export async function pendingOps(rid) {
  return rid === undefined ? idb.getAll("outbox") : idb.getAllByIndex("outbox", "rid", rid);
}
export async function problems() { return idb.getAll("problems"); }
export async function clearProblems() { await idb.clear("problems"); await emit(); }

// ------------------------------------------------------------------ local rules (mirror the server)

let energyTypes = {};
export function setEnergyTypes(map) { energyTypes = map; }

const PREFIX = { electrical: "E", chemical: "C", mechanical: "M", pneumatic: "P", hydraulic: "H", steam: "S",
  startstop: "SS", thermal: "T" };
const POINT_FIELDS = ["label", "energy_type", "magnitude", "device", "deenergize", "lock", "stored_energy", "verify",
  "notes", "photo_missing_reason"];

function addYear(iso) {
  if (!iso) return null;
  const [y, m, d] = iso.split("-").map(Number);
  const dt = new Date(Date.UTC(y + 1, m - 1, d));
  if (dt.getUTCMonth() !== m - 1) dt.setUTCDate(0);  // Feb 29 -> Feb 28
  return dt.toISOString().slice(0, 10);
}

async function findPoint(rev, id) {
  const real = await realId(id);
  const p = rev.points.find((x) => x.id === id || x.id === real);
  if (!p) throw new Error("This isolation point no longer exists.");
  return p;
}

async function findPhoto(rev, id) {
  const real = await realId(id);
  for (const p of rev.points) {
    const ph = p.photos.find((x) => x.id === id || x.id === real);
    if (ph) return { p, ph };
  }
  throw new Error("Photo not found.");
}

function touched(rev) {
  if (rev.status === "ready_for_review") rev.status = "draft";
  rev.locks_calculated = rev.points.reduce((n, p) => n + (Number(p.locks) || 0), 0);
}

const reducers = {
  "revision.update": async (rev, op) => {
    const f = op.fields;
    Object.assign(rev, f);
    if ("review_date_manual" in f || "effective_date" in f) {
      if (!rev.review_date_manual) rev.review_date = addYear(rev.effective_date);
    }
  },
  "point.add": async (rev, op) => {
    const energy = op.fields.energy_type;
    if (!energyTypes[energy]) throw new Error("Choose an energy type.");
    const used = new Set(rev.points.map((p) => p.label));
    let n = 1;
    while (used.has(`${PREFIX[energy] || "X"}-${n}`)) n += 1;
    const p = { id: op.temp_id, seq: rev.points.length + 1, version: 1, copy_status: "n/a", copy_flags: {},
      photos: [], locks: energy === "startstop" ? 0 : 1 };
    for (const k of POINT_FIELDS) p[k] = "";
    p.energy_type = energy;
    p.label = `${PREFIX[energy] || "X"}-${n}`;
    rev.points.push(p);
  },
  "point.update": async (rev, op) => {
    const p = await findPoint(rev, op.point);
    const f = op.fields;
    if ("energy_type" in f && !energyTypes[f.energy_type]) throw new Error("Choose an energy type.");
    if ("label" in f) {
      if (!f.label.trim()) throw new Error("Point label is required (e.g. E-1).");
      if (rev.points.some((x) => x !== p && x.label === f.label.trim())) throw new Error(`Label ${f.label} is already used in this procedure.`);
    }
    if ("locks" in f && !(Number.isInteger(f.locks) && f.locks >= 0 && f.locks <= 50)) throw new Error("Locks must be a whole number from 0 to 50.");
    for (const k of POINT_FIELDS) if (k in f) p[k] = String(f[k] ?? "").trim();
    if ("locks" in f) p.locks = f.locks;
  },
  "point.delete": async (rev, op) => {
    const p = await findPoint(rev, op.point);
    rev.points = rev.points.filter((x) => x !== p);
    rev.points.forEach((x, i) => { x.seq = i + 1; });
  },
  "point.reorder": async (rev, op) => {
    const order = [];
    for (const id of op.ids) order.push(await realId(id));
    rev.points.sort((a, b) => order.indexOf(a.id) - order.indexOf(b.id));
    rev.points.forEach((x, i) => { x.seq = i + 1; });
  },
  "point.check": async (rev, op) => {
    const p = await findPoint(rev, op.point);
    p.copy_status = p.photos.some((ph) => ph.copied_from_photo_id) ? "confirmed" : "replaced";
  },
  "photo.add": async (rev, op) => {
    const p = await findPoint(rev, op.point);
    p.photos.push({ id: op.temp_id, seq: p.photos.length + 1, file_id: null, local: true, width: op.width,
      height: op.height, copied_from_photo_id: null, annotations: op.annotations || [] });
  },
  "photo.delete": async (rev, op) => {
    const { p, ph } = await findPhoto(rev, op.photo);
    p.photos = p.photos.filter((x) => x !== ph);
  },
  "photo.move": async (rev, op) => {
    const { p, ph } = await findPhoto(rev, op.photo);
    const i = p.photos.indexOf(ph);
    const j = i + (op.direction === "up" ? -1 : 1);
    if (j >= 0 && j < p.photos.length) [p.photos[i], p.photos[j]] = [p.photos[j], p.photos[i]];
  },
  "photo.annotate": async (rev, op) => {
    const { ph } = await findPhoto(rev, op.photo);
    ph.annotations = op.annotations;
  },
};

// ------------------------------------------------------------------ changes (local when checked out, else direct)

export async function change(rid, op, blob) {
  op = { op_id: makeId(), rid, ...op };
  const local = await idb.get("revisions", rid);
  if (local && isMine(local)) {
    const rev = await exclusive(async () => {
      const r = await idb.get("revisions", rid);
      await reducers[op.type](r, op);
      touched(r);
      if (blob) await idb.put("blobs", blob, op.temp_id);
      await idb.put("revisions", r);
      await idb.add("outbox", op);
      return r;
    });
    await emit();
    syncSoon();
    return { rev: { ...rev, _local: true }, id: op.temp_id };
  }
  return { id: await direct(rid, op, blob) };
}

async function direct(rid, op, blob) {
  const v = op.version;
  switch (op.type) {
    case "revision.update": await api("PUT", `/api/revisions/${rid}`, { ...op.fields, version: v }); return null;
    case "point.add": return (await api("POST", `/api/revisions/${rid}/points`, op.fields)).id;
    case "point.update": await api("PUT", `/api/points/${op.point}`, { ...op.fields, version: v }); return null;
    case "point.delete": await api("DELETE", `/api/points/${op.point}`); return null;
    case "point.reorder": await api("PUT", `/api/revisions/${rid}/point-order`, { ids: op.ids, version: v }); return null;
    case "point.check": await api("POST", `/api/points/${op.point}/check`); return null;
    case "photo.add": {
      const fd = new FormData();
      fd.append("file", blob, op.filename || "photo.jpg");
      return (await api("POST", `/api/points/${op.point}/photos`, fd)).id;
    }
    case "photo.delete": await api("DELETE", `/api/photos/${op.photo}`); return null;
    case "photo.move": await api("POST", `/api/photos/${op.photo}/move`, { direction: op.direction }); return null;
    case "photo.annotate": await api("PUT", `/api/photos/${op.photo}/annotations`, { annotations: op.annotations }); return null;
    default: throw new Error(`Unknown change ${op.type}`);
  }
}

export async function photoBlobUrl(tempIdValue) {
  const blob = await idb.get("blobs", tempIdValue);
  return blob ? URL.createObjectURL(blob) : null;
}

// ------------------------------------------------------------------ sync engine

let syncing = null;
let timer = null;

export function syncSoon(ms = 300) {
  clearTimeout(timer);
  timer = setTimeout(() => { syncNow().catch(() => {}); }, ms);
}

export function syncNow() {
  if (!syncing) syncing = runSync().finally(() => { syncing = null; });
  return syncing;
}

async function rememberIds(pairs) {
  if (!pairs.length) return;
  const map = await idMap();
  for (const [t, r] of pairs) map[t] = r;
  await idb.put("meta", map, "idmap");
}

async function refreshWorkingCopy(rid, serverRev) {
  await exclusive(async () => {
    if ((await pendingFor(rid)) > 0) return;  // newer local edits still queued; refresh after they upload
    if (serverRev && serverRev.checkout && serverRev.checkout.mine) await idb.put("revisions", serverRev);
  });
}

async function runSync() {
  const queue = await idb.getAll("outbox");
  if (!queue.length) { await emit(); return; }
  status.syncing = true;
  await emit();
  const lost = (await idb.get("meta", "lost")) || {};
  try {
    while (true) {
      const ops = (await idb.getAll("outbox")).filter((o) => !lost[o.rid]);
      if (!ops.length) break;
      const first = ops[0];
      const rid = first.rid;
      try {
        if (first.type === "photo.add") {
          const blob = await idb.get("blobs", first.temp_id);
          if (blob) {
            const fd = new FormData();
            fd.append("point", String(first.point));
            fd.append("temp_id", first.temp_id);
            fd.append("annotations", JSON.stringify(first.annotations || []));
            fd.append("file", blob, first.filename || "photo.jpg");
            const res = await api("POST", `/api/revisions/${rid}/sync-photo`, fd);
            await rememberIds([[first.temp_id, res.id]]);
          }
          await exclusive(async () => { await idb.delete("outbox", first.seq); await idb.delete("blobs", first.temp_id); });
          if (!(await pendingFor(rid))) await refreshWorkingCopy(rid, await api("GET", `/api/revisions/${rid}`));
        } else {
          const batch = [];
          for (const o of ops) {
            if (o.rid !== rid || o.type === "photo.add" || batch.length >= 50) break;
            batch.push(o);
          }
          const res = await api("POST", `/api/revisions/${rid}/sync`, { ops: batch });
          const pairs = [];
          await exclusive(async () => {
            for (const [i, r] of res.results.entries()) {
              const op = batch[i];
              if (r.ok && op.temp_id && r.id) pairs.push([op.temp_id, r.id]);
              if (!r.ok) await idb.add("problems", { rid, op, error: r.error, at: new Date().toISOString() });
              await idb.delete("outbox", op.seq);
            }
          });
          await rememberIds(pairs);
          await refreshWorkingCopy(rid, res.revision);
        }
      } catch (err) {
        if (err.offline) break;  // try again later
        if (err.status === 409) {  // checked out elsewhere: keep this device's changes, stop uploading them
          lost[rid] = err.message;
          await idb.put("meta", lost, "lost");
          continue;
        }
        if (err.status === 401 || err.status === 403) break;  // signed out / password change pending
        // Anything else: park the change as a problem so the queue can't jam.
        await exclusive(async () => {
          await idb.add("problems", { rid, op: first, error: err.message, at: new Date().toISOString() });
          await idb.delete("outbox", first.seq);
        });
      }
    }
    status.lastSync = new Date();
  } finally {
    status.syncing = false;
    await emit();
  }
}

// Retry regularly, and as soon as the browser thinks the network is back.
window.addEventListener("online", () => syncSoon(0));
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") syncSoon(0); });
setInterval(async () => {
  if ((await idb.count("outbox")) > 0) syncNow().catch(() => {});
  else if (!status.online) api("GET", "/api/health").catch(() => {});
}, 20000);
emit();
