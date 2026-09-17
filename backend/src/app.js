const express = require('express');
const path = require('path');
const cors = require('cors');
const reportingRouter = require('./routes/reporting');
const authRouter = require('./routes/auth');
const dispatchPlanRouter = require('./routes/dispatch-plan');
const productionRouter = require('./routes/production');
const { notFoundHandler, errorHandler } = require('./middleware/errorHandler');

const FRONTEND_DIR = path.join(__dirname, '../../frontend');
const FLOOR_APP_DIR = path.join(__dirname, '../../production/floor-app');
const PRODUCTION_ADMIN_DIR = path.join(__dirname, '../../production/admin');

// Same Render service, same Express process, same Supabase project --
// just a second hostname routed to it so the production/warehouse system
// has its own URL instead of living under paths on the daily report's
// domain. See production/README.md "A dedicated URL" for the reasoning.
// PRODUCTION_HOST is the bare hostname (e.g. "production.shonrei.co.nz"),
// matched against the request's Host header -- no new Render service, no
// new deploy, just a DNS CNAME plus a custom domain added to this same
// service in Render's dashboard.
const PRODUCTION_HOST = process.env.PRODUCTION_HOST;

function buildMainApp() {
  const app = express();

  app.get('/health', (req, res) => res.json({ status: 'ok' }));

  // Public runtime config for the static frontend -- SUPABASE_URL/ANON_KEY
  // are meant to be public (the anon key only ever exchanges credentials
  // for a session token; it grants no access on its own), so this lets one
  // build of the frontend work across environments without a bundler.
  // PRODUCTION_APP_URL (also public -- just a URL, not a secret) lets the
  // dashboard's nav link point at the dedicated domain once it's set up,
  // without hardcoding it at build time.
  app.get('/config.js', (req, res) => {
    res.type('application/javascript').send(
      `window.__CONFIG__ = ${JSON.stringify({
        SUPABASE_URL: process.env.SUPABASE_URL,
        SUPABASE_ANON_KEY: process.env.SUPABASE_ANON_KEY,
        PRODUCTION_APP_URL: PRODUCTION_HOST ? `https://${PRODUCTION_HOST}/` : null,
      })};`
    );
  });

  // Same shape as buildProductionApp's own /production-config.js below,
  // served here too so admin's script tag doesn't 404 when reached via
  // the old /production-admin/ path on this domain. DASHBOARD_URL is "/"
  // here (not a URL) since on this domain "/" genuinely is the dashboard
  // -- just a convenience link, not an auth handoff (admin has its own
  // login regardless of which path/domain served it).
  app.get('/production-config.js', (req, res) => {
    res.type('application/javascript').send(
      `window.__PRODUCTION_CONFIG__ = ${JSON.stringify({
        SUPABASE_URL: process.env.SUPABASE_URL,
        SUPABASE_ANON_KEY: process.env.SUPABASE_ANON_KEY,
        DASHBOARD_URL: '/',
      })};`
    );
  });

  app.use('/reporting', reportingRouter);
  app.use('/reporting/dispatch-plan', dispatchPlanRouter);
  app.use('/auth', authRouter);
  app.use('/production', productionRouter);

  // Old paths, kept working for anyone with an existing bookmark --
  // superseded by the dedicated production.* host below once that's set
  // up, not removed.
  app.use('/production-floor', express.static(FLOOR_APP_DIR));
  app.use('/production-admin', express.static(PRODUCTION_ADMIN_DIR));

  app.use(express.static(FRONTEND_DIR));

  app.use(notFoundHandler);
  app.use(errorHandler);

  return app;
}

// The production/warehouse system at its own root: admin screen at '/',
// floor app at '/floor'. Still the same production API router (mounted
// at '/production', same as the main app) -- both apps' JS already fetch
// relative paths like /production/targets, so nothing there needs to
// change, it just resolves against whichever host served the page.
function buildProductionApp() {
  const app = express();

  app.get('/health', (req, res) => res.json({ status: 'ok' }));

  // The shared base stylesheet (design tokens, .topbar, .card, etc.) that
  // production/admin/index.html links as an absolute "/styles.css" --
  // served explicitly rather than mounting the whole frontend dir, so
  // this host never accidentally serves the daily report itself.
  app.get('/styles.css', (req, res) => res.sendFile(path.join(FRONTEND_DIR, 'styles.css')));

  // Admin has its own login on this host (production.production_users --
  // deliberately NOT reporting.report_users, see production/README.md
  // "Its own login") rather than borrowing a session from the dashboard,
  // so it needs its own copy of the Supabase creds to sign in with.
  // DASHBOARD_URL is purely a "jump to the daily report" convenience
  // link for whoever happens to have both accounts -- not an auth path.
  app.get('/production-config.js', (req, res) => {
    res.type('application/javascript').send(
      `window.__PRODUCTION_CONFIG__ = ${JSON.stringify({
        SUPABASE_URL: process.env.SUPABASE_URL,
        SUPABASE_ANON_KEY: process.env.SUPABASE_ANON_KEY,
        DASHBOARD_URL: process.env.MAIN_APP_URL || null,
      })};`
    );
  });

  app.use('/production', productionRouter);
  app.use('/floor', express.static(FLOOR_APP_DIR));
  app.use('/', express.static(PRODUCTION_ADMIN_DIR));

  app.use(notFoundHandler);
  app.use(errorHandler);

  return app;
}

function createApp() {
  const mainApp = buildMainApp();
  const productionApp = PRODUCTION_HOST ? buildProductionApp() : null;

  const app = express();
  app.use(cors());
  app.use(express.json());

  app.use((req, res, next) => {
    // req.hostname already strips a trailing :port, so this matches
    // regardless of whether Render/a proxy adds one.
    if (productionApp && req.hostname === PRODUCTION_HOST) {
      return productionApp(req, res, next);
    }
    return mainApp(req, res, next);
  });

  return app;
}

module.exports = { createApp };
