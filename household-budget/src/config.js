// All configuration comes from environment variables. Bank access tokens
// live ONLY here (never in the database, never sent to the browser).
const path = require('path');

try {
  // Optional local .env (same folder as package.json). No dependency on dotenv:
  // Node 22 can load it natively.
  process.loadEnvFile(path.join(__dirname, '..', '.env'));
} catch {
  /* no .env file -- fine in production, env comes from the host */
}

function int(name, fallback) {
  const v = parseInt(process.env[name] ?? '', 10);
  return Number.isFinite(v) ? v : fallback;
}

const config = {
  port: int('PORT', 3100),
  dbPath: process.env.DB_PATH || path.join(__dirname, '..', 'data', 'budget.db'),
  timezone: 'Pacific/Auckland',

  // Akahu (NZ open banking aggregator -- connects ANZ, ASB and every other
  // major NZ bank). Create a free "Personal App" at https://my.akahu.nz/developers
  akahuAppToken: process.env.AKAHU_APP_TOKEN || '',
  akahuUserToken: process.env.AKAHU_USER_TOKEN || '',
  akahuBaseUrl: process.env.AKAHU_BASE_URL || 'https://api.akahu.io/v1',

  syncIntervalMinutes: int('SYNC_INTERVAL_MINUTES', 30),
  syncBackfillDays: int('SYNC_BACKFILL_DAYS', 180),

  // Sessions
  cookieSecure: process.env.COOKIE_SECURE !== 'false',
  sessionIdleMinutes: int('SESSION_IDLE_MINUTES', 60),
  sessionMaxHours: int('SESSION_MAX_HOURS', 24 * 7),
  // Number of reverse proxies in front of the app (Render/Fly = 1). Used so
  // login rate limiting sees the real client IP, not the proxy's.
  trustProxy: int('TRUST_PROXY', 1),
};

module.exports = config;
