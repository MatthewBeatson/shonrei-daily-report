const express = require('express');
const path = require('path');
const cors = require('cors');
const reportingRouter = require('./routes/reporting');
const authRouter = require('./routes/auth');
const { notFoundHandler, errorHandler } = require('./middleware/errorHandler');

const FRONTEND_DIR = path.join(__dirname, '../../frontend');

function createApp() {
  const app = express();

  app.use(cors());
  app.use(express.json());

  app.get('/health', (req, res) => res.json({ status: 'ok' }));

  // Public runtime config for the static frontend -- SUPABASE_URL/ANON_KEY
  // are meant to be public (the anon key only ever exchanges credentials
  // for a session token; it grants no access on its own), so this lets one
  // build of the frontend work across environments without a bundler.
  app.get('/config.js', (req, res) => {
    res.type('application/javascript').send(
      `window.__CONFIG__ = ${JSON.stringify({
        SUPABASE_URL: process.env.SUPABASE_URL,
        SUPABASE_ANON_KEY: process.env.SUPABASE_ANON_KEY,
      })};`
    );
  });

  app.use('/reporting', reportingRouter);
  app.use('/auth', authRouter);

  app.use(express.static(FRONTEND_DIR));

  app.use(notFoundHandler);
  app.use(errorHandler);

  return app;
}

module.exports = { createApp };
