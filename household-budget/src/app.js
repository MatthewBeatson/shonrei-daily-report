const path = require('path');
const express = require('express');
const config = require('./config');
const auth = require('./auth');
const apiRouter = require('./routes/api');

function createApp() {
  const app = express();
  app.disable('x-powered-by');
  app.set('trust proxy', config.trustProxy);

  // Security headers on every response.
  app.use((req, res, next) => {
    res.setHeader('Content-Security-Policy', [
      "default-src 'self'", "script-src 'self'", "style-src 'self'", "img-src 'self' data:",
      "connect-src 'self'", "font-src 'self'", "object-src 'none'", "base-uri 'none'",
      "form-action 'self'", "frame-ancestors 'none'",
    ].join('; '));
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('X-Frame-Options', 'DENY');
    res.setHeader('Referrer-Policy', 'no-referrer');
    res.setHeader('Permissions-Policy', 'camera=(), microphone=(), geolocation=(), payment=()');
    res.setHeader('Cross-Origin-Opener-Policy', 'same-origin');
    res.setHeader('Cross-Origin-Resource-Policy', 'same-origin');
    if (config.cookieSecure) res.setHeader('Strict-Transport-Security', 'max-age=31536000; includeSubDomains');
    next();
  });

  app.get('/health', (req, res) => res.json({ ok: true }));

  app.use('/api', (req, res, next) => { res.setHeader('Cache-Control', 'no-store'); next(); });
  app.use('/api', express.json({ limit: '200kb' }));
  app.use('/api', auth.requireCsrfHeader);
  app.post('/api/login', auth.login);
  app.post('/api/logout', auth.logout);
  app.use('/api', auth.requireAuth, apiRouter);
  app.use('/api', (req, res) => res.status(404).json({ error: 'Not found' }));

  app.use(express.static(path.join(__dirname, '..', 'public'), { index: 'index.html', maxAge: '1h' }));
  return app;
}

module.exports = { createApp };
