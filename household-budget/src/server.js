const config = require('./config');
const db = require('./db');
const ledger = require('./ledger');
const { createApp } = require('./app');

db.open(config.dbPath);
const { n } = db.get().prepare('SELECT COUNT(*) AS n FROM users').get();
if (n === 0) {
  console.warn('No users yet -- create one with: npm run user -- add <username>');
}

createApp().listen(config.port, () => {
  console.log(`Household budget running on http://localhost:${config.port}`);
  ledger.startScheduler();
});
