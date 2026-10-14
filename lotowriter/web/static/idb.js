// Minimal promise wrapper around IndexedDB (no library).
const DB_NAME = "lotowriter";
const DB_VERSION = 1;
let dbPromise = null;

function open() {
  if (!dbPromise) {
    dbPromise = new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = () => {
        const db = req.result;
        db.createObjectStore("revisions", { keyPath: "id" });     // checked-out procedures (working copies)
        const out = db.createObjectStore("outbox", { keyPath: "seq", autoIncrement: true });  // changes to upload
        out.createIndex("rid", "rid");
        db.createObjectStore("blobs");                             // photos waiting to upload, key = temp id
        db.createObjectStore("meta");                              // me, config, id map, flags
        db.createObjectStore("problems", { keyPath: "seq", autoIncrement: true });  // changes the server refused
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }
  return dbPromise;
}

function done(req) {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function store(name, mode = "readonly") {
  return (await open()).transaction(name, mode).objectStore(name);
}

export const idb = {
  async get(name, key) { return done((await store(name)).get(key)); },
  async getAll(name) { return done((await store(name)).getAll()); },
  async getAllByIndex(name, index, value) { return done((await store(name)).index(index).getAll(value)); },
  async put(name, value, key) { return done((await store(name, "readwrite")).put(value, key)); },
  async add(name, value) { return done((await store(name, "readwrite")).add(value)); },
  async delete(name, key) { return done((await store(name, "readwrite")).delete(key)); },
  async clear(name) { return done((await store(name, "readwrite")).clear()); },
  async count(name) { return done((await store(name)).count()); },
};
