const config = require('./config');
const db = require('./db');
const ledger = require('./ledger');
const { startBackups } = require('./backup');
const { createApp } = require('./app');

db.open(config.dbPath);
const { n } = db.get().prepare('SELECT COUNT(*) AS n FROM users').get();
if (n === 0) {
  console.warn('No users yet -- create one with: npm run user -- add <username>');
}

createApp().listen(config.port, config.host, () => {
  const where = config.host === '127.0.0.1' ? 'this computer only' : `interface ${config.host}`;
  console.log(`Household budget running on http://localhost:${config.port} (${where})`);
  ledger.startScheduler();
  startBackups(config.backupDir, config.backupKeep);
});
