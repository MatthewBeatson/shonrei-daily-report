// Shonrei Daily Management Summary -- frontend.
//
// No build step, no framework: vanilla JS talking to (a) Supabase's Auth
// REST endpoints directly for sign-in/sign-out/token refresh, and (b) our
// own Node backend (same origin) for everything report-related. The
// backend is what enforces who can see/edit what -- this file just renders
// what it's given and gets out of the way.
//
// Session tokens live in sessionStorage (cleared when the tab/browser
// closes) by default -- deliberate, given this shows financial figures on
// shared/mobile devices. A user can opt in to "Remember me on this device"
// (see the `remember` vault below), which mirrors the session into
// localStorage instead so it survives closing the browser; on the next
// visit that resumes straight into the PIN lock screen rather than full
// login. It's strictly opt-in and per-device, reversible with one tap.

(() => {
  const CONFIG = window.__CONFIG__ || {};
  const SUPABASE_URL = CONFIG.SUPABASE_URL;
  const SUPABASE_ANON_KEY = CONFIG.SUPABASE_ANON_KEY;
  const IDLE_MS = 2 * 60 * 1000; // 2 minutes, per requirement

  const $ = (id) => document.getElementById(id);
  const views = {
    login: $('view-login'),
    forgot: $('view-forgot'),
    setPassword: $('view-set-password'),
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
  // Session storage helpers
  // ---------------------------------------------------------------
  const session = {
    get access_token() { return sessionStorage.getItem('access_token'); },
    get refresh_token() { return sessionStorage.getItem('refresh_token'); },
    get expires_at() { return Number(sessionStorage.getItem('expires_at') || 0); },
    set(tokens) {
      sessionStorage.setItem('access_token', tokens.access_token);
      sessionStorage.setItem('refresh_token', tokens.refresh_token);
      sessionStorage.setItem('expires_at', String(Date.now() + (tokens.expires_in || 3600) * 1000));
      // Keep the persisted "remember me" copy in sync too, if enabled --
      // Supabase rotates the refresh token on every use, so without this
      // the vault's copy would go stale after the first refresh and
      // silently stop working a session later.
      if (remember.isEnabled()) remember.save(tokens);
    },
    clear() {
      sessionStorage.removeItem('access_token');
      sessionStorage.removeItem('refresh_token');
      sessionStorage.removeItem('expires_at');
    },
  };

  // ---------------------------------------------------------------
  // "Use PIN instead" vault -- localStorage. Distinct from `session`
  // above: this is what survives a closed browser. Deliberately NOT
  // auto-resumed on page load (that silently died on any transient
  // network hiccup, which is what made it feel unreliable) -- instead
  // the login screen shows a "Use PIN instead" link when this device has
  // something remembered, and the user explicitly taps it. On failure it
  // just shows an error and leaves the password field usable, rather
  // than silently disabling the feature forever over what might have
  // been a one-off blip.
  //
  // On by default after any successful login (no separate opt-in
  // checkbox) unless the user has explicitly turned it off for this
  // device via the dashboard toggle -- isNeverSet() distinguishes "never
  // touched" (default on) from an explicit past "off" (stays off).
  // ---------------------------------------------------------------
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
      // Seed the vault immediately from the session that's active right now.
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
  // Supabase Auth (direct REST calls -- no SDK dependency)
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

  // Sends Supabase's own password-reset email -- self-service, triggered
  // only when a user submits this form themselves, never by us on their
  // behalf. Requires the deployed URL to be in Supabase's Auth ->
  // URL Configuration -> Redirect URLs allowlist (see README).
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

  // Called after the user follows the emailed reset link and picks a new
  // password. Uses the short-lived recovery access_token Supabase put in
  // the URL fragment as the Bearer -- that's what authorizes changing the
  // password here, not the (unknown/throwaway) old one.
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

  async function ensureFreshToken() {
    // Refresh a bit before actual expiry so requests don't race it.
    if (!session.access_token || Date.now() > session.expires_at - 30000) {
      await supabaseRefresh();
    }
  }

  function fullSignOut(message) {
    session.clear();
    remember.disable(); // "sign out" means require real credentials next time, same as any app
    state.reportUser = null;
    $('login-error').textContent = message || '';
    updateUsePinLinkVisibility();
    showView('login');
    stopIdleWatch();
  }

  // ---------------------------------------------------------------
  // Backend API helper
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

  // ---------------------------------------------------------------
  // App state
  // ---------------------------------------------------------------
  const state = { reportUser: null, snapshotData: null };

  // ---------------------------------------------------------------
  // Formatting helpers -- mirror the Excel number format
  // $#,##0;[Red]($#,##0);-
  // ---------------------------------------------------------------
  function fmtMoney(v) {
    if (v === null || v === undefined) return '—';
    const n = Number(v);
    if (n === 0) return '-';
    const abs = Math.round(Math.abs(n)).toLocaleString('en-NZ');
    return n < 0 ? `($${abs})` : `$${abs}`;
  }
  function fmtRatio(v) {
    if (v === null || v === undefined) return '—';
    return `${Number(v).toFixed(2)}x`;
  }
  function fmtTime(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    return d.toLocaleString('en-NZ', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
  }
  // Plain date (no time) -- used to name the previous-workday row after
  // the actual day, e.g. "Friday, 7 Aug" rather than a generic label.
  function fmtDayLabel(dateStr) {
    if (!dateStr) return null;
    // node-postgres serializes a `date` column as a full ISO datetime
    // string (e.g. "2026-08-06T00:00:00.000Z"), not a plain 'YYYY-MM-DD'
    // -- slice(0, 10) handles both. Parsed as local, not UTC, so it
    // doesn't shift a day depending on the viewer's timezone.
    const [y, m, d] = dateStr.slice(0, 10).split('-').map(Number);
    const date = new Date(y, m - 1, d);
    return date.toLocaleDateString('en-NZ', { weekday: 'long', day: 'numeric', month: 'short' });
  }

  function statusBadge(status) {
    const map = { ok: 'OK', error: 'Error', manual: 'Manual', calculated: 'Calculated', partial: 'Partial' };
    const cls = status || 'calculated';
    return `<span class="status-badge ${cls}">${map[status] || status}</span>`;
  }

  // Renders one dashboard row. valueClass: 'imported' | 'manual' | 'calculated'
  function renderRow({ label, bold, value, valueClass, source, refreshedAt, status, editable }) {
    const rowClass = valueClass === 'calculated' ? 'calculated' : valueClass === 'manual' ? 'manual' : '';
    const valColorClass = valueClass === 'manual' ? 'manual-color' : valueClass === 'calculated' ? 'neutral' : (typeof value === 'string' && value.startsWith('(') ? 'negative' : '');
    return `
      <div class="metric-row ${rowClass}">
        <div class="metric-label ${bold ? 'bold' : ''}">${label}</div>
        <div class="metric-value-col">
          <div class="metric-value ${valColorClass}">${value}</div>
          <div class="metric-meta">
            ${source ? `<span>${source}</span>` : ''}
            ${refreshedAt ? `<span>${refreshedAt}</span>` : ''}
            ${status ? statusBadge(status) : ''}
          </div>
          ${editable ? `<button class="edit-inline-btn" data-open-edit="1">Edit</button>` : ''}
        </div>
      </div>`;
  }

  function renderDashboard(data) {
    state.snapshotData = data;
    const { snapshot, manual_inputs, calculated, refresh_state } = data;

    $('as-of-text').textContent = snapshot ? `As at ${fmtTime(snapshot.as_of)}` : 'No refresh yet';
    const overallBadge = $('overall-status-badge');
    const overall = snapshot ? snapshot.overall_status : 'calculated';
    overallBadge.className = `status-badge ${overall}`;
    overallBadge.textContent = (overall || '—').toUpperCase();

    const errBox = $('dashboard-error');
    errBox.textContent = refresh_state?.status === 'running' ? 'Refreshing… figures below are from the last completed refresh.' : '';

    // ---- Cash position ----
    $('row-bank').innerHTML = renderRow({
      label: 'Bank balances — NZD equivalent', value: fmtMoney(snapshot?.bank_balance), valueClass: 'imported',
      source: 'Xero', refreshedAt: fmtTime(snapshot?.as_of), status: snapshot?.bank_status,
    });
    $('row-net-short-term').innerHTML = renderRow({
      label: 'Net short-term position', bold: true, value: fmtMoney(calculated?.net_short_term_position),
      valueClass: 'calculated', source: 'Calculated', status: 'calculated',
    });

    // ---- Sales ----
    $('row-sales-mtd').innerHTML = renderRow({
      label: 'Sales invoiced — month to date', value: fmtMoney(snapshot?.sales_mtd), valueClass: 'imported',
      source: 'Xero P&L', refreshedAt: fmtTime(snapshot?.as_of), status: snapshot?.sales_status,
    });
    $('row-sales-workday').innerHTML = renderRow({
      label: `Sales invoiced — ${fmtDayLabel(snapshot?.sales_previous_workday_date) || 'previous working day'}`,
      value: fmtMoney(snapshot?.sales_previous_workday), valueClass: 'imported',
      source: 'Xero P&L', refreshedAt: fmtTime(snapshot?.as_of), status: snapshot?.sales_status,
    });
    $('row-sales-prev').innerHTML = renderRow({
      label: 'Sales invoiced — previous month', value: fmtMoney(snapshot?.sales_prev_month), valueClass: 'imported',
      source: 'Xero P&L', refreshedAt: fmtTime(snapshot?.as_of), status: snapshot?.sales_status,
    });
    $('row-projected').innerHTML = renderRow({
      label: 'Projected sales — current month', value: fmtMoney(manual_inputs?.projected_sales), valueClass: 'manual',
      source: 'Manual', status: 'manual', editable: state.reportUser?.can_edit,
    });
    $('row-breakeven').innerHTML = renderRow({
      label: 'Monthly breakeven', value: fmtMoney(manual_inputs?.monthly_breakeven), valueClass: 'manual',
      source: 'Manual', status: 'manual', editable: state.reportUser?.can_edit,
    });
    $('row-variance').innerHTML = renderRow({
      label: 'Variance to breakeven', bold: true, value: fmtMoney(calculated?.variance_to_breakeven),
      valueClass: 'calculated', source: 'Calculated', status: 'calculated',
    });
    $('row-sales-on-hand').innerHTML = renderRow({
      label: 'Sales on hand — open/uninvoiced', value: fmtMoney(snapshot?.sales_on_hand), valueClass: 'imported',
      source: 'Cin7 Core', refreshedAt: fmtTime(snapshot?.as_of), status: snapshot?.sales_on_hand_status,
    });
    $('row-remaining').innerHTML = renderRow({
      label: 'Remaining sales after month-end', bold: true, value: fmtMoney(calculated?.remaining_sales_after_month_end),
      valueClass: 'calculated', source: 'Sales on hand − (Projected − MTD)', status: 'calculated',
    });

    // ---- Working capital ----
    $('row-debtors-total').innerHTML = renderRow({
      label: 'Debtors — total outstanding', bold: true, value: fmtMoney(snapshot?.debtors_total), valueClass: 'imported',
      source: 'Xero', refreshedAt: fmtTime(snapshot?.as_of), status: snapshot?.debtors_status,
    });
    $('row-debtors-not-due').innerHTML = renderRow({
      label: 'Debtors — not yet due', value: fmtMoney(snapshot?.debtors_not_due), valueClass: 'imported',
      source: 'Xero', refreshedAt: fmtTime(snapshot?.as_of), status: snapshot?.debtors_status,
    });
    $('row-debtors-overdue').innerHTML = renderRow({
      label: 'Debtors — due / overdue', value: fmtMoney(snapshot?.debtors_overdue), valueClass: 'imported',
      source: 'Xero', refreshedAt: fmtTime(snapshot?.as_of), status: snapshot?.debtors_status,
    });
    $('row-creditors-total').innerHTML = renderRow({
      label: 'Creditors — total outstanding', bold: true, value: fmtMoney(snapshot?.creditors_total), valueClass: 'imported',
      source: 'Xero', refreshedAt: fmtTime(snapshot?.as_of), status: snapshot?.creditors_status,
    });
    $('row-creditors-nzd').innerHTML = renderRow({
      label: 'Creditors — NZD operational payables', value: fmtMoney(snapshot?.creditors_nzd_payables), valueClass: 'imported',
      source: 'Xero', refreshedAt: fmtTime(snapshot?.as_of), status: snapshot?.creditors_status,
    });
    $('row-wc').innerHTML = renderRow({
      label: 'Net liquid working capital', value: fmtMoney(calculated?.net_liquid_working_capital),
      valueClass: 'calculated', source: 'Cash + debtors − creditors', status: 'calculated',
    });
    $('row-wc-ratio').innerHTML = renderRow({
      label: 'Liquid working capital ratio', value: fmtRatio(calculated?.liquid_working_capital_ratio),
      valueClass: 'calculated', source: '(Cash + debtors) ÷ creditors', status: 'calculated',
    });

    // Rebuilt fully each render (not appended-to) so repeated renders --
    // on load, after refresh polling, after a save -- never stack up
    // duplicate Edit buttons.
    const commentaryText = (manual_inputs?.commentary || 'No commentary yet.')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;');
    $('row-commentary').innerHTML = `
      <div class="metric-row">
        <div class="metric-label" style="white-space:pre-wrap; flex:1;">${commentaryText}</div>
        ${state.reportUser?.can_edit ? '<button class="edit-inline-btn" data-open-edit="1">Edit</button>' : ''}
      </div>`;

    document.querySelectorAll('[data-open-edit]').forEach((btn) => btn.addEventListener('click', openEditModal));
    updateRefreshButton(refresh_state);
  }

  function updateRefreshButton(refreshState) {
    const btn = $('refresh-btn');
    if (refreshState?.status === 'running') {
      btn.disabled = true;
      btn.innerHTML = '<span class="spin">↻</span> Refreshing…';
    } else {
      btn.disabled = false;
      btn.innerHTML = '↻ Refresh';
    }
  }

  async function loadDashboard() {
    try {
      const data = await api('/reporting/snapshot');
      renderDashboard(data);
      if (data.refresh_state?.status === 'running') pollUntilIdle();
    } catch (e) {
      if (e.message !== 'Unauthorized') $('dashboard-error').textContent = e.message;
    }
  }

  let pollTimer = null;
  function pollUntilIdle() {
    if (pollTimer) return;
    pollTimer = setInterval(async () => {
      try {
        const data = await api('/reporting/snapshot');
        renderDashboard(data);
        if (data.refresh_state?.status !== 'running') {
          clearInterval(pollTimer);
          pollTimer = null;
        }
      } catch (e) { /* keep trying */ }
    }, 4000);
  }

  // ---------------------------------------------------------------
  // Edit manual inputs modal
  // ---------------------------------------------------------------
  function openEditModal() {
    const mi = state.snapshotData?.manual_inputs || {};
    $('edit-projected').value = mi.projected_sales ?? '';
    $('edit-breakeven').value = mi.monthly_breakeven ?? '';
    $('edit-commentary').value = mi.commentary ?? '';
    $('edit-error').textContent = '';
    $('edit-modal-backdrop').classList.remove('hidden');
  }
  $('edit-cancel').addEventListener('click', () => $('edit-modal-backdrop').classList.add('hidden'));
  $('edit-save').addEventListener('click', async () => {
    const parseNum = (v) => (v.trim() === '' ? null : Number(v.replace(/,/g, '')));
    const projected_sales = parseNum($('edit-projected').value);
    const monthly_breakeven = parseNum($('edit-breakeven').value);
    const commentary = $('edit-commentary').value;
    if ((projected_sales !== null && Number.isNaN(projected_sales)) || (monthly_breakeven !== null && Number.isNaN(monthly_breakeven))) {
      $('edit-error').textContent = 'Enter valid numbers.';
      return;
    }
    try {
      await api('/reporting/manual-inputs', { method: 'PUT', body: JSON.stringify({ projected_sales, monthly_breakeven, commentary }) });
      $('edit-modal-backdrop').classList.add('hidden');
      loadDashboard();
    } catch (e) {
      $('edit-error').textContent = e.message;
    }
  });

  // ---------------------------------------------------------------
  // Refresh button
  // ---------------------------------------------------------------
  $('refresh-btn').addEventListener('click', async () => {
    try {
      await api('/reporting/refresh', { method: 'POST' });
      updateRefreshButton({ status: 'running' });
      pollUntilIdle();
    } catch (e) {
      $('dashboard-error').textContent = e.details?.retryAfterSeconds
        ? `${e.message}` : e.message;
    }
  });

  // ---------------------------------------------------------------
  // Login
  // ---------------------------------------------------------------
  $('login-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const btn = $('login-submit');
    $('login-error').textContent = '';
    btn.disabled = true;
    try {
      await supabaseSignIn($('login-email').value.trim(), $('login-password').value);
      // On by default (first time this device has ever seen this
      // setting); an explicit past "off" from the dashboard toggle is
      // respected and left alone rather than silently re-enabled.
      if (remember.isNeverSet() || remember.isEnabled()) remember.enable();
      await afterSignIn();
    } catch (e) {
      $('login-error').textContent = e.message;
    } finally {
      btn.disabled = false;
    }
  });

  // "Use PIN instead" on the login screen -- explicit, one-shot attempt
  // to resume this device's remembered session. Unlike the old
  // auto-resume-on-boot approach, a failure here (network blip, Render
  // cold start, genuinely expired session) just shows an error and
  // leaves the password field sitting right there ready to use --
  // it does NOT silently disable the feature, so a one-off hiccup
  // doesn't quietly break it for next time.
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

  // startLocked: true when resuming a remembered session via "Use PIN
  // instead" -- the dashboard loads underneath, but the PIN lock shows
  // immediately on top of it, so nothing is visible until the PIN is
  // entered.
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
      loadDashboard();
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
  // Forgot password / set new password (from emailed recovery link)
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
      // The recovery link's tokens are now a normal valid session -- use
      // them directly instead of asking for the new password again.
      session.set({ access_token: recoveryAccessToken, refresh_token: recoveryRefreshToken, expires_in: 3600 });
      await afterSignIn();
    } catch (e) {
      err.textContent = e.message;
    }
  });

  let recoveryRefreshToken = null;
  function checkForRecoveryLink() {
    const hash = window.location.hash.startsWith('#') ? window.location.hash.slice(1) : '';
    const params = new URLSearchParams(hash);
    if (params.get('type') === 'recovery' && params.get('access_token')) {
      recoveryAccessToken = params.get('access_token');
      recoveryRefreshToken = params.get('refresh_token');
      history.replaceState(null, '', window.location.pathname); // scrub tokens from the visible URL
      showView('setPassword');
      return true;
    }
    return false;
  }

  // ---------------------------------------------------------------
  // PIN setup
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
      loadDashboard();
    } catch (e) {
      err.textContent = e.message;
    }
  });

  // ---------------------------------------------------------------
  // Idle watch + lock screen
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
        loadDashboard();
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

  // ---------------------------------------------------------------
  // Lock now / sign out buttons
  // ---------------------------------------------------------------
  $('lock-now-btn').addEventListener('click', lockScreen);
  $('sign-out-btn').addEventListener('click', () => fullSignOut());

  // ---------------------------------------------------------------
  // Boot: recovery link > this tab's own sessionStorage session (e.g. a
  // plain page refresh mid-session) > login screen, with "Use PIN
  // instead" shown there if this device has something remembered.
  // Deliberately no automatic vault resume here -- see the "Use PIN
  // instead" handler above for why.
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
