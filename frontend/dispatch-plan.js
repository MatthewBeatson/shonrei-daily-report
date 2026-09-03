// Shonrei Monthly Dispatch Plan -- frontend.
//
// Its own page (dispatch-plan.html), not a section of the daily dashboard
// -- Matthew's call 2026-09-04. This file duplicates app.js's whole
// login/PIN/lock/session block rather than sharing it via a common module:
// this project deliberately has no build step, and every page here is
// meant to work standalone by just being served as a static file. Keep
// the two blocks in sync by hand if session/PIN/lock behavior ever
// changes -- see app.js for the fuller comments on why each piece exists
// (remember-me vault, idle lock, MFA recovery, etc.), not repeated here.

(() => {
  const CONFIG = window.__CONFIG__ || {};
  const SUPABASE_URL = CONFIG.SUPABASE_URL;
  const SUPABASE_ANON_KEY = CONFIG.SUPABASE_ANON_KEY;
  const IDLE_MS = 2 * 60 * 1000;

  const $ = (id) => document.getElementById(id);
  const views = {
    login: $('view-login'),
    forgot: $('view-forgot'),
    setPassword: $('view-set-password'),
    setPasswordMfa: $('view-set-password-mfa'),
    pinSetup: $('view-pin-setup'),
    dashboard: $('view-dashboard'),
    locked: $('view-locked'),
  };

  function showView(name) {
    for (const key of Object.keys(views)) {
      views[key].classList.toggle('hidden', key !== name);
    }
  }

  // ---------------------------------------------------------------
  // Session storage helpers (identical to app.js)
  // ---------------------------------------------------------------
  const session = {
    get access_token() { return sessionStorage.getItem('access_token'); },
    get refresh_token() { return sessionStorage.getItem('refresh_token'); },
    get expires_at() { return Number(sessionStorage.getItem('expires_at') || 0); },
    set(tokens) {
      sessionStorage.setItem('access_token', tokens.access_token);
      sessionStorage.setItem('refresh_token', tokens.refresh_token);
      sessionStorage.setItem('expires_at', String(Date.now() + (tokens.expires_in || 3600) * 1000));
      if (remember.isEnabled()) remember.save(tokens);
    },
    clear() {
      sessionStorage.removeItem('access_token');
      sessionStorage.removeItem('refresh_token');
      sessionStorage.removeItem('expires_at');
    },
  };

  const REMEMBER_KEY = 'remember_device_enabled';
  const remember = {
    isEnabled() { return localStorage.getItem(REMEMBER_KEY) === 'true'; },
    isNeverSet() { return localStorage.getItem(REMEMBER_KEY) === null; },
    save(tokens) {
      localStorage.setItem('r_access_token', tokens.access_token);
      localStorage.setItem('r_refresh_token', tokens.refresh_token);
      localStorage.setItem('r_expires_at', String(Date.now() + (tokens.expires_in || 3600) * 1000));
    },
    load() {
      const refresh_token = localStorage.getItem('r_refresh_token');
      if (!refresh_token) return null;
      return {
        access_token: localStorage.getItem('r_access_token'),
        refresh_token,
        expires_at: Number(localStorage.getItem('r_expires_at') || 0),
      };
    },
    clearVault() {
      localStorage.removeItem('r_access_token');
      localStorage.removeItem('r_refresh_token');
      localStorage.removeItem('r_expires_at');
    },
    enable() {
      localStorage.setItem(REMEMBER_KEY, 'true');
      if (session.refresh_token) {
        remember.save({
          access_token: session.access_token,
          refresh_token: session.refresh_token,
          expires_in: Math.max(0, Math.round((session.expires_at - Date.now()) / 1000)),
        });
      }
    },
    disable() {
      localStorage.setItem(REMEMBER_KEY, 'false');
      remember.clearVault();
    },
  };

  // ---------------------------------------------------------------
  // Supabase Auth (identical to app.js)
  // ---------------------------------------------------------------
  async function supabaseSignIn(email, password) {
    const r = await fetch(`${SUPABASE_URL}/auth/v1/token?grant_type=password`, {
      method: 'POST',
      headers: { apikey: SUPABASE_ANON_KEY, 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    });
    const body = await r.json();
    if (!r.ok) throw new Error(body.error_description || body.msg || 'Sign in failed');
    session.set(body);
  }

  async function supabaseRefresh() {
    const rt = session.refresh_token;
    if (!rt) throw new Error('No refresh token');
    const r = await fetch(`${SUPABASE_URL}/auth/v1/token?grant_type=refresh_token`, {
      method: 'POST',
      headers: { apikey: SUPABASE_ANON_KEY, 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: rt }),
    });
    const body = await r.json();
    if (!r.ok) throw new Error('Session expired');
    session.set(body);
  }

  async function supabaseRequestPasswordReset(email) {
    const r = await fetch(`${SUPABASE_URL}/auth/v1/recover`, {
      method: 'POST',
      headers: { apikey: SUPABASE_ANON_KEY, 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, options: { redirect_to: window.location.origin } }),
    });
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      throw new Error(body.error_description || body.msg || 'Could not send reset email');
    }
  }

  async function supabaseSetNewPassword(recoveryAccessToken, newPassword) {
    const r = await fetch(`${SUPABASE_URL}/auth/v1/user`, {
      method: 'PUT',
      headers: {
        apikey: SUPABASE_ANON_KEY,
        Authorization: `Bearer ${recoveryAccessToken}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ password: newPassword }),
    });
    const body = await r.json();
    if (!r.ok) throw new Error(body.error_description || body.msg || 'Could not set password');
  }

  async function supabaseGetVerifiedTotpFactor(accessToken) {
    const r = await fetch(`${SUPABASE_URL}/auth/v1/user`, {
      headers: { apikey: SUPABASE_ANON_KEY, Authorization: `Bearer ${accessToken}` },
    });
    const body = await r.json();
    if (!r.ok) throw new Error(body.error_description || body.msg || 'Could not check account');
    return (body.factors || []).find((f) => f.factor_type === 'totp' && f.status === 'verified') || null;
  }

  async function supabaseMfaChallengeAndVerify(accessToken, factorId, code) {
    const challengeRes = await fetch(`${SUPABASE_URL}/auth/v1/factors/${factorId}/challenge`, {
      method: 'POST',
      headers: { apikey: SUPABASE_ANON_KEY, Authorization: `Bearer ${accessToken}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    const challengeBody = await challengeRes.json();
    if (!challengeRes.ok) throw new Error(challengeBody.error_description || challengeBody.msg || 'Could not start 2FA challenge');

    const verifyRes = await fetch(`${SUPABASE_URL}/auth/v1/factors/${factorId}/verify`, {
      method: 'POST',
      headers: { apikey: SUPABASE_ANON_KEY, Authorization: `Bearer ${accessToken}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ challenge_id: challengeBody.id, code }),
    });
    const verifyBody = await verifyRes.json();
    if (!verifyRes.ok) throw new Error(verifyBody.error_description || verifyBody.msg || 'Incorrect code');
    return verifyBody.access_token;
  }

  async function ensureFreshToken() {
    if (!session.access_token || Date.now() > session.expires_at - 30000) {
      await supabaseRefresh();
    }
  }

  function fullSignOut(message) {
    session.clear();
    remember.disable();
    state.reportUser = null;
    $('login-error').textContent = message || '';
    updateUsePinLinkVisibility();
    showView('login');
    stopIdleWatch();
  }

  // ---------------------------------------------------------------
  // Backend API helper (identical to app.js)
  // ---------------------------------------------------------------
  async function api(path, options = {}) {
    await ensureFreshToken();
    const r = await fetch(path, {
      ...options,
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${session.access_token}`,
        ...(options.headers || {}),
      },
    });
    if (r.status === 401) {
      fullSignOut('Your session expired. Please sign in again.');
      throw new Error('Unauthorized');
    }
    const text = await r.text();
    const body = text ? JSON.parse(text) : {};
    if (!r.ok) {
      const err = new Error(body.error || `Request failed (${r.status})`);
      err.status = r.status;
      err.details = body.details;
      throw err;
    }
    return body;
  }

  const state = { reportUser: null };

  function fmtTime(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    return d.toLocaleString('en-NZ', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
  }

  // ---------------------------------------------------------------
  // Monthly Dispatch Plan (this page's actual content)
  // ---------------------------------------------------------------
  async function loadDispatchPlan() {
    try {
      const data = await api('/reporting/dispatch-plan');
      const statusEl = $('dispatch-plan-status');
      const downloadBtn = $('dispatch-plan-download');
      if (data.status === 'ok' && data.download_url) {
        const months = (data.months_covered || []).join(', ') || 'no months';
        statusEl.textContent = `Generated ${fmtTime(data.generated_at)} · covers ${months}`;
        downloadBtn.href = data.download_url;
        downloadBtn.classList.remove('hidden');
      } else if (data.status === 'error') {
        statusEl.textContent = 'Last generation attempt failed -- see the refresh log, or try Generate now.';
        downloadBtn.classList.add('hidden');
      } else {
        statusEl.textContent = 'Not generated yet -- runs automatically every Monday morning.';
        downloadBtn.classList.add('hidden');
      }
      if (state.reportUser?.can_edit) {
        $('dispatch-plan-generate').classList.remove('hidden');
        $('dispatch-plan-overrides-wrap').classList.remove('hidden');
        await loadDispatchPlanOverrides();
      }
    } catch (e) {
      if (e.message !== 'Unauthorized') $('dispatch-plan-error').textContent = e.message;
    }
  }

  async function loadDispatchPlanOverrides() {
    try {
      const { overrides } = await api('/reporting/dispatch-plan/overrides');
      const body = $('dispatch-plan-overrides-body');
      body.innerHTML = overrides.length
        ? overrides.map((o) => `
          <tr>
            <td>${o.order_number}</td>
            <td>${o.group_label_override || ''}</td>
            <td>${o.dispatch_date_override ? o.dispatch_date_override.slice(0, 10) : ''}</td>
            <td>${o.hold ? 'Yes' : ''}</td>
            <td>${o.note || ''}</td>
            <td class="override-delete"><button class="link-danger" data-delete-override="${o.order_number}">Remove</button></td>
          </tr>`).join('')
        : '<tr><td colspan="6" style="color:var(--gray);">No overrides set.</td></tr>';
    } catch (e) {
      $('dispatch-plan-error').textContent = e.message;
    }
  }

  $('dispatch-plan-generate').addEventListener('click', async () => {
    const btn = $('dispatch-plan-generate');
    btn.disabled = true;
    $('dispatch-plan-error').textContent = '';
    try {
      await api('/reporting/dispatch-plan/generate', { method: 'POST' });
      $('dispatch-plan-status').textContent = 'Generating… this can take a few minutes (pulling every open Cin7 order). Refresh the page shortly.';
    } catch (e) {
      $('dispatch-plan-error').textContent = e.message;
    } finally {
      btn.disabled = false;
    }
  });

  $('dispatch-plan-overrides-body').addEventListener('click', async (e) => {
    const orderNumber = e.target?.dataset?.deleteOverride;
    if (!orderNumber) return;
    try {
      await api(`/reporting/dispatch-plan/overrides/${encodeURIComponent(orderNumber)}`, { method: 'DELETE' });
      loadDispatchPlanOverrides();
    } catch (err) {
      $('dispatch-plan-error').textContent = err.message;
    }
  });

  $('override-save').addEventListener('click', async () => {
    const orderNumber = $('override-so').value.trim();
    if (!orderNumber) {
      $('dispatch-plan-error').textContent = 'Enter an SO# to save an override.';
      return;
    }
    try {
      await api(`/reporting/dispatch-plan/overrides/${encodeURIComponent(orderNumber)}`, {
        method: 'PUT',
        body: JSON.stringify({
          group_label_override: $('override-label').value.trim() || null,
          dispatch_date_override: $('override-date').value || null,
          hold: $('override-hold').checked,
          note: $('override-note').value.trim() || null,
        }),
      });
      $('override-so').value = '';
      $('override-label').value = '';
      $('override-date').value = '';
      $('override-hold').checked = false;
      $('override-note').value = '';
      $('dispatch-plan-error').textContent = '';
      loadDispatchPlanOverrides();
    } catch (e) {
      $('dispatch-plan-error').textContent = e.message;
    }
  });

  // ---------------------------------------------------------------
  // Login (identical to app.js)
  // ---------------------------------------------------------------
  $('login-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const btn = $('login-submit');
    $('login-error').textContent = '';
    btn.disabled = true;
    try {
      await supabaseSignIn($('login-email').value.trim(), $('login-password').value);
      if (remember.isNeverSet() || remember.isEnabled()) remember.enable();
      await afterSignIn();
    } catch (e) {
      $('login-error').textContent = e.message;
    } finally {
      btn.disabled = false;
    }
  });

  $('use-pin-link').addEventListener('click', async () => {
    const link = $('use-pin-link');
    const vault = remember.load();
    if (!vault?.refresh_token) {
      $('login-error').textContent = 'No remembered device found here -- sign in with your password.';
      link.classList.add('hidden');
      return;
    }
    $('login-error').textContent = '';
    link.disabled = true;
    link.textContent = 'Checking…';
    try {
      session.set({ access_token: vault.access_token, refresh_token: vault.refresh_token, expires_in: 0 });
      await ensureFreshToken();
      await afterSignIn({ startLocked: true });
    } catch (e) {
      $('login-error').textContent = "Couldn't verify this device -- sign in with your password instead.";
      session.clear();
    } finally {
      link.disabled = false;
      link.textContent = 'Use PIN instead';
    }
  });

  async function afterSignIn({ startLocked = false } = {}) {
    const me = await api('/auth/me');
    state.reportUser = me;
    if (!me.pin_set) {
      showView('pinSetup');
    } else {
      showView('dashboard');
      updateRememberButton();
      resetIdleTimer();
      startIdleWatch();
      loadDispatchPlan();
      if (startLocked) lockScreen();
    }
  }

  function updateRememberButton() {
    $('remember-toggle-btn').textContent = `PIN sign-in: ${remember.isEnabled() ? 'On' : 'Off'}`;
  }
  function updateUsePinLinkVisibility() {
    $('use-pin-link').classList.toggle('hidden', !remember.isEnabled());
  }
  $('remember-toggle-btn').addEventListener('click', () => {
    if (remember.isEnabled()) {
      remember.disable();
    } else {
      remember.enable();
    }
    updateRememberButton();
  });

  // ---------------------------------------------------------------
  // Forgot password / set new password (identical to app.js)
  // ---------------------------------------------------------------
  $('forgot-password-link').addEventListener('click', () => {
    $('forgot-email').value = $('login-email').value;
    $('forgot-error').textContent = '';
    $('forgot-success').style.display = 'none';
    showView('forgot');
  });
  $('forgot-back').addEventListener('click', () => showView('login'));
  $('forgot-submit').addEventListener('click', async (ev) => {
    ev.preventDefault();
    const btn = $('forgot-submit');
    const email = $('forgot-email').value.trim();
    $('forgot-error').textContent = '';
    if (!email) { $('forgot-error').textContent = 'Enter your email.'; return; }
    btn.disabled = true;
    try {
      await supabaseRequestPasswordReset(email);
      $('forgot-success').style.display = 'block';
    } catch (e) {
      $('forgot-error').textContent = e.message;
    } finally {
      btn.disabled = false;
    }
  });

  let recoveryAccessToken = null;
  $('set-password-submit').addEventListener('click', async () => {
    const p1 = $('set-password-1').value;
    const p2 = $('set-password-2').value;
    const err = $('set-password-error');
    err.textContent = '';
    if (p1.length < 8) { err.textContent = 'Password must be at least 8 characters.'; return; }
    if (p1 !== p2) { err.textContent = 'Passwords do not match.'; return; }
    try {
      await supabaseSetNewPassword(recoveryAccessToken, p1);
      session.set({ access_token: recoveryAccessToken, refresh_token: recoveryRefreshToken, expires_in: 3600 });
      await afterSignIn();
    } catch (e) {
      err.textContent = e.message;
    }
  });

  $('set-password-mfa-submit').addEventListener('click', async () => {
    const code = $('set-password-mfa-code').value.trim();
    const err = $('set-password-mfa-error');
    err.textContent = '';
    if (!/^\d{6}$/.test(code)) { err.textContent = 'Enter the 6-digit code.'; return; }
    try {
      recoveryAccessToken = await supabaseMfaChallengeAndVerify(recoveryAccessToken, pendingMfaFactorId, code);
      showView('setPassword');
    } catch (e) {
      err.textContent = e.message;
    }
  });

  let recoveryRefreshToken = null;
  let pendingMfaFactorId = null;
  function checkForRecoveryLink() {
    const hash = window.location.hash.startsWith('#') ? window.location.hash.slice(1) : '';
    const params = new URLSearchParams(hash);
    if (params.get('type') === 'recovery' && params.get('access_token')) {
      recoveryAccessToken = params.get('access_token');
      recoveryRefreshToken = params.get('refresh_token');
      history.replaceState(null, '', window.location.pathname);
      showView('setPassword');
      supabaseGetVerifiedTotpFactor(recoveryAccessToken)
        .then((factor) => {
          if (factor) {
            pendingMfaFactorId = factor.id;
            showView('setPasswordMfa');
          }
        })
        .catch(() => {});
      return true;
    }
    return false;
  }

  // ---------------------------------------------------------------
  // PIN setup (identical to app.js)
  // ---------------------------------------------------------------
  $('pin-setup-submit').addEventListener('click', async () => {
    const p1 = $('pin-setup-1').value;
    const p2 = $('pin-setup-2').value;
    const err = $('pin-setup-error');
    if (!/^\d{4}$/.test(p1)) { err.textContent = 'PIN must be exactly 4 digits.'; return; }
    if (p1 !== p2) { err.textContent = 'PINs do not match.'; return; }
    try {
      await api('/auth/pin/set', { method: 'POST', body: JSON.stringify({ pin: p1 }) });
      state.reportUser.pin_set = true;
      showView('dashboard');
      updateRememberButton();
      resetIdleTimer();
      startIdleWatch();
      loadDispatchPlan();
    } catch (e) {
      err.textContent = e.message;
    }
  });

  // ---------------------------------------------------------------
  // Idle watch + lock screen (identical to app.js)
  // ---------------------------------------------------------------
  let lastActivity = Date.now();
  let idleInterval = null;
  const ACTIVITY_EVENTS = ['mousemove', 'keydown', 'touchstart', 'scroll', 'click'];

  function resetIdleTimer() { lastActivity = Date.now(); }
  function onActivity() {
    if (!views.dashboard.classList.contains('hidden')) resetIdleTimer();
  }

  function startIdleWatch() {
    ACTIVITY_EVENTS.forEach((ev) => document.addEventListener(ev, onActivity, { passive: true }));
    if (idleInterval) clearInterval(idleInterval);
    idleInterval = setInterval(() => {
      if (!views.dashboard.classList.contains('hidden') && Date.now() - lastActivity > IDLE_MS) {
        lockScreen();
      }
    }, 5000);
  }
  function stopIdleWatch() {
    ACTIVITY_EVENTS.forEach((ev) => document.removeEventListener(ev, onActivity));
    if (idleInterval) clearInterval(idleInterval);
    idleInterval = null;
  }

  let pinBuffer = '';
  function updatePinDots() {
    const dots = document.querySelectorAll('#lock-pin-dots .pin-dot');
    dots.forEach((d, i) => d.classList.toggle('filled', i < pinBuffer.length));
  }

  function lockScreen() {
    pinBuffer = '';
    updatePinDots();
    $('lock-error').textContent = '';
    $('lock-user-text').textContent = state.reportUser ? `Enter your PIN, ${state.reportUser.full_name || state.reportUser.email}` : 'Enter your PIN';
    showView('locked');
  }

  $('lock-keypad').addEventListener('click', async (ev) => {
    const key = ev.target.closest('.pin-key')?.dataset.key;
    if (!key) return;
    if (key === 'back') {
      pinBuffer = pinBuffer.slice(0, -1);
      updatePinDots();
      return;
    }
    if (pinBuffer.length >= 4) return;
    pinBuffer += key;
    updatePinDots();
    if (pinBuffer.length === 4) {
      try {
        await ensureFreshToken();
        await api('/auth/pin/verify', { method: 'POST', body: JSON.stringify({ pin: pinBuffer }) });
        showView('dashboard');
        resetIdleTimer();
        loadDispatchPlan();
      } catch (e) {
        $('lock-error').textContent = e.message || 'Incorrect PIN';
        pinBuffer = '';
        updatePinDots();
        if (e.status === 423) {
          setTimeout(() => fullSignOut('Too many incorrect PIN attempts. Please sign in again.'), 1500);
        }
      }
    }
  });

  $('lock-use-password').addEventListener('click', () => fullSignOut());

  $('lock-now-btn').addEventListener('click', lockScreen);
  $('sign-out-btn').addEventListener('click', () => fullSignOut());

  // ---------------------------------------------------------------
  // Boot (identical to app.js)
  // ---------------------------------------------------------------
  (async function boot() {
    if (checkForRecoveryLink()) return;

    if (session.access_token) {
      try {
        await afterSignIn();
        return;
      } catch (e) {
        session.clear();
      }
    }
    updateUsePinLinkVisibility();
    showView('login');
  })();
})();
