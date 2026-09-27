// Minimal Akahu API client (https://developers.akahu.nz). Akahu is the NZ
// open-banking aggregator: you connect ANZ and ASB once inside Akahu's own
// consent flow (your bank passwords never touch this app), and this app
// receives read-only access via two tokens.
const config = require('./config');

class AkahuError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

function isConfigured() {
  return Boolean(config.akahuAppToken && config.akahuUserToken);
}

async function request(method, path, { query, fetchImpl = fetch } = {}) {
  if (!isConfigured()) throw new AkahuError('Akahu tokens are not configured (AKAHU_APP_TOKEN / AKAHU_USER_TOKEN)', 0);
  const url = new URL(config.akahuBaseUrl + path);
  for (const [k, v] of Object.entries(query || {})) if (v != null) url.searchParams.set(k, v);
  const res = await fetchImpl(url, {
    method,
    headers: {
      Authorization: `Bearer ${config.akahuUserToken}`,
      'X-Akahu-Id': config.akahuAppToken,
      Accept: 'application/json',
    },
    signal: AbortSignal.timeout(30000),
  });
  let body = null;
  try { body = await res.json(); } catch { /* non-JSON error page */ }
  if (!res.ok || (body && body.success === false)) {
    // Deliberately never echo headers/tokens into the error.
    const msg = body?.message || `Akahu ${method} ${path} failed with HTTP ${res.status}`;
    throw new AkahuError(msg, res.status);
  }
  return body;
}

async function getAccounts(opts) {
  const body = await request('GET', '/accounts', opts);
  return body.items || [];
}

async function getTransactions(startIso, endIso, opts = {}) {
  const items = [];
  let cursor;
  for (let page = 0; page < 200; page++) {
    const body = await request('GET', '/transactions', { ...opts, query: { start: startIso, end: endIso, cursor } });
    items.push(...(body.items || []));
    cursor = body.cursor?.next;
    if (!cursor) break;
  }
  return items;
}

// Ask Akahu to fetch fresh data from the banks now. Asynchronous on Akahu's
// side: new transactions show up on the next sync a minute or two later.
async function requestRefresh(opts) {
  await request('POST', '/refresh', opts);
}

module.exports = { isConfigured, getAccounts, getTransactions, requestRefresh, AkahuError };
