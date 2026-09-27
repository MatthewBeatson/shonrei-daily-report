# Household Budget

A private web app for the household's money: it pulls transactions from
**ANZ and ASB** automatically, **codes them to budget categories as they
arrive**, tracks spending against budgets in **monthly periods** (calendar
months, or payday-to-payday), and has a **mortgage section** for splits,
fixed-rate expiries, interest and payoff projections.

> This lives in the `shonrei-daily-report` repo for now but shares nothing
> with the Shonrei reporting app -- its own server, its own database, no
> Supabase, no business credentials. It can be moved to its own (private)
> repo at any time by copying this folder.

## How the bank connection works

NZ banks don't let arbitrary apps log in to them, and you should never give
an app your internet-banking password. Instead this uses **[Akahu](https://www.akahu.nz)**,
the NZ open-banking provider that ANZ, ASB, BNZ, Westpac, Kiwibank etc.
all work with:

1. You connect ANZ and ASB **inside Akahu's own site** (my.akahu.nz), using
   the banks' official consent flows. Your bank passwords go to the bank /
   Akahu, never to this app.
2. You create a free **Personal App** in Akahu, which gives two tokens.
3. This app uses those tokens for **read-only** access -- it can see
   accounts, balances and transactions. It cannot move money.

The app syncs every 30 minutes (configurable) and on the **Sync** button,
which also asks Akahu to refresh from the banks. New transactions appear
once the bank has made them available to Akahu -- usually within a few
hours of the purchase; pending card transactions aren't included until they
post.

### Setting up Akahu (one-off, ~10 minutes)

1. Sign up at <https://my.akahu.nz> and connect your ANZ and ASB logins
   (and add your partner's accounts too if they're separate logins).
2. Go to <https://my.akahu.nz/developers> -> create a **Personal App**.
3. Copy the **App Token** (`app_token_...`) and **User Token**
   (`user_token_...`) straight into your host's environment settings as
   `AKAHU_APP_TOKEN` / `AKAHU_USER_TOKEN`. Don't paste them into chat,
   email or a file in the repo.

No Akahu? The app still works: create accounts under **Settings -> Accounts**
and import CSV statements exported from ANZ / ASB internet banking. CSV
import is also the way to load history from before the feed was connected
(duplicates of feed transactions are skipped).

## Auto-coding

Every new transaction is coded in this order -- first match wins:

| # | Source | What it does |
|---|--------|--------------|
| 1 | **You** | Anything you code by hand is never changed automatically. |
| 2 | **Rules** | Your rules, in order: "description contains `mercury`, money out -> Power & Gas". Can restrict by account, direction and amount range; regex supported. |
| 3 | **Transfers** | Same amount out of one of your accounts and into another within 3 days -> *Transfer* (excluded from budgets). Money into an account flagged **Mortgage** codes the outgoing side as **Mortgage Repayment**. |
| 4 | **Learned** | When you code a transaction by hand the app remembers that merchant and codes the next one the same way. |
| 5 | **Bank** | Akahu's own merchant category (e.g. "Supermarkets and grocery stores" -> Groceries). |
| 6 | -- | Otherwise it goes in the **To review** queue (red badge on Transactions). |

When you code something by hand the app offers **"Make a rule"** with a
suggested pattern and a live preview of which existing transactions it
would catch. Creating or editing a rule re-codes history too (manual codes
excepted).

## Monthly periods and budgets

**Settings -> Budget period** sets the day each period starts: `1` for
calendar months, or your payday (e.g. `15` gives 15 Sep - 14 Oct). Budgets
are set once as a standing amount for every period, with optional one-off
overrides for a single period (Christmas, a holiday month). The overview
shows each category's spend vs budget with a pace marker, what's left to
spend per day, and the 3-period average to help set realistic budgets.

Accounts flagged "not in budget" (loans, KiwiSaver, investments -- the
default for those account types) don't count towards spending.

## Mortgage

Add each split of the loan (e.g. 1-year fixed, 2-year fixed, revolving).
Link a split to the synced loan account and its balance updates
automatically; rate, fixed-until date and repayment are prefilled where
Akahu provides the bank's loan details. The page shows:

- total owing, balance-weighted rate, interest per month, principal per month
- fixed-term expiry countdown per split (amber under 90 days, red under 30)
- projected mortgage-free date and interest remaining
- "what if we paid $X extra a month" -- new payoff date and interest saved
- loan balance history, and interest charged vs repaid for each period

## Security

- **Sign-in needs a password (12+ chars, scrypt-hashed) *and* a 6-digit
  authenticator code** (Google Authenticator, 1Password, Authy...). Codes
  can't be replayed.
- There is **no sign-up page**. Users are created from the server shell
  with the CLI below.
- 5 failed sign-ins per username or IP -> locked for 15 minutes. Failures
  and other sensitive actions go to an `audit_log` table.
- Sessions: random 256-bit token in an `HttpOnly; Secure; SameSite=Strict`
  cookie; only its SHA-256 is stored. Signed out after 60 min idle / 7 days
  max. Changing password signs out every session.
- CSRF: SameSite=Strict plus a required custom header on every write.
- Strict Content-Security-Policy (no inline scripts or styles, no third-party
  anything), `frame-ancestors 'none'`, HSTS, no-referrer, `noindex`.
- The **bank tokens live only in environment variables** -- never in the
  database, never sent to the browser -- and are read-only.
- The database file is created with `0600` permissions. It holds
  transaction history (no passwords to banks), so keep it on an encrypted
  disk and don't copy it around casually.
- Only one runtime dependency (Express). SQLite is Node's built-in driver.

## Running it

Needs **Node 22.13+**.

```bash
cd household-budget
npm install
cp .env.example .env        # set COOKIE_SECURE=false for http://localhost
npm run user -- add matthew # prompts for a password, prints the authenticator secret
npm start                   # http://localhost:3100
npm test
```

User admin (run on the server):

```bash
npm run user -- add <name>        # new user (each person gets their own login)
npm run user -- reset-2fa <name>  # lost phone -> new authenticator secret
npm run user -- password <name>   # reset a password
npm run user -- list | remove <name>
```

## Deploying

It's a single always-on Node process with a SQLite file, so it needs a host
with a **persistent disk** and **HTTPS**. Kept separate from the Shonrei
`render.yaml` on purpose.

**Render** (simplest, ~US$7/month -- disks need a paid plan):
1. New -> **Web Service** -> this repo. Root directory `household-budget`,
   build `npm install`, start `npm start`, plan Starter, health check `/health`.
2. **Disks** -> add a 1 GB disk mounted at `/var/data`.
3. Environment: `DB_PATH=/var/data/budget.db`, `AKAHU_APP_TOKEN`,
   `AKAHU_USER_TOKEN` (and any of the other settings in `.env.example`).
4. Deploy, then open the service's **Shell** and run
   `npm run user -- add <name>` for each person.

Any VPS, Fly.io (with a volume) or a home server behind Cloudflare Tunnel
works the same way. Back up the database file (e.g. nightly copy of
`budget.db` to encrypted storage) -- it's the only state.

## Layout

```
src/server.js        entry point: opens DB, starts HTTP + background sync
src/app.js           Express app, security headers, auth wiring
src/auth.js          passwords, TOTP, sessions, lockout, CSRF
src/db.js            SQLite schema + default NZ household categories
src/akahu.js         Akahu API client (read-only)
src/ledger.js        sync, upserts, CSV import, re-coding
src/categorise.js    the auto-coding engine (pure functions)
src/periods.js       monthly period maths (NZ timezone)
src/mortgage.js      amortisation / payoff projections
src/csv-import.js    ANZ + ASB CSV parsing
src/routes/api.js    REST API
src/cli.js           user admin
public/              the web app (plain HTML/CSS/JS, no build step)
test/                node:test unit + end-to-end API tests
```
