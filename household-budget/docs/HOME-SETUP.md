# Running Household Budget on a home computer

This sets the app up on a computer at home that stays on, so you and your
partner can open it on your phones from anywhere. Nothing is exposed to the
internet: only devices signed in to **your** Tailscale network can reach
it. Cost: $0.

Allow about 45 minutes the first time. Steps 1-6 are on the home computer.

---

## 1. Get the app onto the computer

Easiest: on GitHub, open the repo, switch to the
`claude/household-budget-bank-app-yuf6zz` branch (or `main` once it's
merged), click **Code -> Download ZIP**, and unzip it somewhere permanent
such as `Documents`. You only need the `household-budget` folder out of it.
Moving that folder somewhere simple, e.g. `C:\household-budget` or
`~/household-budget`, makes the later steps easier.

(If you use Git: `git clone` the repo instead. Then updating later is just
`git pull`.)

## 2. Install Node.js

Download the **LTS** version from <https://nodejs.org> and install it with
the default options. It needs version 22.13 or newer.

Check it worked -- open **Terminal** (Mac) or **Command Prompt** (Windows)
and run `node --version`.

## 3. Install and configure

In Terminal / Command Prompt, go into the folder and install:

```bash
cd C:\household-budget          # Windows (or wherever you put it)
cd ~/household-budget           # Mac
npm install
```

Create the settings file by copying `.env.example` to a new file called
`.env` in the same folder (on Windows: `copy .env.example .env`; on Mac:
`cp .env.example .env`). Open `.env` in Notepad / TextEdit and fill in:

```
AKAHU_APP_TOKEN=app_token_...      # from my.akahu.nz/developers
AKAHU_USER_TOKEN=user_token_...
```

Leave everything else as it is. `.env` is private: don't email it, don't
put it in the repo.

## 4. Create your logins

One per person:

```bash
npm run user -- add matthew
```

It asks for a password (12+ characters), then shows an **authenticator
secret**. On your phone, open Google Authenticator / Microsoft
Authenticator / 1Password, choose "Add account -> Enter a setup key", and
type the secret in. Do the same for your partner with their own username,
on their phone.

## 5. Start it and check it works

```bash
npm start
```

Open <http://localhost:3100> on that computer and sign in (password + the
6-digit code from your authenticator app). The first bank sync starts about
5 seconds after launch and pulls the last 6 months, so give it a minute
and press **Sync** if the accounts aren't there yet.

> If the browser won't keep you signed in at `http://localhost`, stop the app
> (Ctrl+C), add `COOKIE_SECURE=false` to `.env`, and start it again. Take that
> line back out once you're using the Tailscale address in step 7, which is
> proper HTTPS.

Press **Ctrl+C** to stop it once you've seen it working. The next step makes
it start by itself.

## 6. Make it start automatically, and keep the computer awake

**Windows**
1. Press **Win+R**, type `shell:startup`, press Enter. A folder opens.
2. In another window, open the app folder -> `scripts`. Right-click
   `start-windows.cmd` -> **Show more options -> Create shortcut**.
3. Move the new shortcut into the Startup folder.
4. Right-click the shortcut -> **Properties** -> set **Run** to
   **Minimized** -> OK.
5. Double-click the shortcut to start it now. A minimised window appears in
   the taskbar. That's the app, so leave it running. If it stops, it
   restarts itself after 10 seconds.
6. **Settings -> System -> Power** -> set "When plugged in, put my device to
   sleep after" to **Never**. (The screen can still turn off.)

**Mac**
1. In Terminal, in the app folder: `bash scripts/install-mac.sh`
2. **System Settings -> Energy** (or **Battery -> Options** on a laptop) ->
   turn on **Prevent automatic sleeping when the display is off** (when on
   power).

Log messages go to `data/server.log` if you ever need to check on it.

## 7. Reach it from your phones with Tailscale

Tailscale creates a private network between your own devices. The app
stays invisible to everyone else on the internet.

1. Sign up at <https://tailscale.com> (free Personal plan). Use a personal
   account, not a work one.
2. Install Tailscale on the **home computer** and sign in.
3. In the Tailscale admin console (<https://login.tailscale.com/admin/dns>):
   make sure **MagicDNS** is on, then turn on **HTTPS Certificates**.
4. On the home computer, in Terminal / Command Prompt (on Windows, run it
   as Administrator):
   ```bash
   tailscale serve --bg 3100
   ```
   It prints an address like `https://home-pc.tail1234.ts.net`. That's
   your app's address from now on. It survives restarts.
5. Install the **Tailscale app on your phone**, sign in with the same
   account, and open that address. In Safari/Chrome use **Share -> Add to
   Home Screen** so it opens like an app.
6. **Your partner:** in the admin console, **Users -> Invite users**. They
   install Tailscale on their phone, accept the invite, and use the same
   address.

The Tailscale app needs to be switched on on the phone to reach the
budget. It's fine to leave it on all the time, since it only routes traffic
for your own devices.

## 8. Backups

The app saves a copy of its database once a day to `data/backups/`,
keeping the last 14 days. That protects against mistakes, not against the
computer dying. For that, occasionally copy the newest file in
`data/backups/` somewhere else: a USB stick, or a **personal** cloud
folder (iCloud / personal OneDrive / Google Drive with 2-step sign-in on).
Don't use a work drive. You can also point the daily backup straight at
such a folder by adding `BACKUP_DIR=` with that folder's path to `.env`.

To **restore**: stop the app, copy a backup file to `data/budget.db`
(replacing the old one), start the app.

## Moving from another computer

If you've been running it somewhere else (e.g. temporarily on a laptop):
1. Stop the app on the old computer.
2. Copy its `data/budget.db` into the new computer's `household-budget/data/`
   folder (create `data` if needed) **before** step 5, and skip step 4. Your
   logins, authenticator codes, rules, budgets and history all come across.
3. Delete the folder from the old computer afterwards, including its `.env`.

## Updating to a new version

Stop the app, replace the files (download the ZIP again and copy it over
the old folder, **keeping** your `.env` and `data` folder, or `git pull`),
run `npm install`, and start it again (restart the computer, or re-run the
shortcut / `bash scripts/install-mac.sh`).

## If something's wrong

| Problem | Try |
|---|---|
| Phone can't open the address | Is the Tailscale app switched on on the phone? Is the home computer on, awake and connected? |
| "Bank feed not connected" | The Akahu tokens in `.env` are missing or mistyped. Fix them and restart the app. |
| Sync failed | Look at **Settings -> Bank feed** for the message. Tokens may have been revoked in Akahu, or a bank connection needs re-authorising at my.akahu.nz. |
| Lost phone / authenticator | On the home computer: `npm run user -- reset-2fa <name>` |
| Forgot password | On the home computer: `npm run user -- password <name>` |
| Anything else | `data/server.log` has the details. |
