// Daily backup of the database: a consistent snapshot via SQLite's
// VACUUM INTO (safe while the app is running), keeping the newest N copies.
const fs = require('fs');
const path = require('path');
const db = require('./db');
const { todayNZ } = require('./periods');

const NAME = /^budget-\d{4}-\d{2}-\d{2}\.db$/;

function runBackup(dir, keep, today = todayNZ()) {
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const file = path.join(dir, `budget-${today}.db`);
  if (fs.existsSync(file)) return { file, skipped: true };
  const tmp = `${file}.tmp`;
  fs.rmSync(tmp, { force: true });
  db.get().prepare('VACUUM INTO ?').run(tmp);
  fs.renameSync(tmp, file);
  try { fs.chmodSync(file, 0o600); } catch { /* not fatal on odd filesystems */ }
  const old = fs.readdirSync(dir).filter((f) => NAME.test(f)).sort().reverse().slice(Math.max(1, keep));
  for (const f of old) fs.rmSync(path.join(dir, f), { force: true });
  return { file, skipped: false, removed: old.length };
}

function startBackups(dir, keep) {
  if (!dir || dir === 'off') return null;
  const run = () => {
    try {
      const r = runBackup(dir, keep);
      if (!r.skipped) console.log(`[backup] wrote ${r.file}`);
    } catch (e) {
      console.error(`[backup] failed: ${e.message}`);
    }
  };
  setTimeout(run, 60 * 1000);
  // Check hourly; only writes once per NZ day, so a laptop that's only on
  // some of the time still gets a backup each day it's used.
  return setInterval(run, 60 * 60 * 1000);
}

module.exports = { runBackup, startBackups };
