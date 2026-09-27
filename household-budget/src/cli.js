// Admin CLI -- run on the server itself (e.g. Render Shell). There is
// deliberately no sign-up page: only someone with shell access can add users.
//
//   npm run user -- add <username>        create a user (prompts for password)
//   npm run user -- reset-2fa <username>  issue a new authenticator secret
//   npm run user -- password <username>   set a new password
//   npm run user -- list
//   npm run user -- remove <username>
const readline = require('readline');
const config = require('./config');
const db = require('./db');
const auth = require('./auth');

// One shared reader with a line queue: when stdin is piped, lines can arrive
// before the next prompt is asked and would otherwise be lost.
let rl = null;
let muted = false;
const lines = [];
const waiting = [];
function ask(question, { hidden = false } = {}) {
  if (!rl) {
    rl = readline.createInterface({ input: process.stdin, output: process.stdout, terminal: Boolean(process.stdin.isTTY) });
    const write = rl._writeToOutput.bind(rl);
    rl._writeToOutput = (s) => { if (!muted) write(s); };
    rl.on('line', (line) => {
      if (muted) { muted = false; process.stdout.write('\n'); }
      if (waiting.length) waiting.shift()(line); else lines.push(line);
    });
  }
  process.stdout.write(question);
  muted = hidden && Boolean(process.stdin.isTTY);
  if (lines.length) { muted = false; return Promise.resolve(lines.shift()); }
  return new Promise((resolve) => waiting.push(resolve));
}

async function askPassword() {
  for (;;) {
    const pw = await ask('Password (12+ characters): ', { hidden: true });
    const problem = auth.passwordProblem(pw);
    if (problem) { console.log(problem); continue; }
    const again = await ask('Repeat password: ', { hidden: true });
    if (again !== pw) { console.log('Passwords did not match'); continue; }
    return pw;
  }
}

function showTotp(username, secret) {
  console.log('\nAdd this to an authenticator app (Google Authenticator, 1Password, Authy...):');
  console.log(`  Account: Household Budget (${username})`);
  console.log(`  Secret key: ${secret.match(/.{1,4}/g).join(' ')}`);
  console.log(`  Or open this link on the phone: ${auth.otpauthUrl(username, secret)}\n`);
  console.log('This secret is shown once. Keep it out of chat/email.');
}

async function main() {
  db.open(config.dbPath);
  const d = db.get();
  const [cmd, username] = process.argv.slice(2);
  const user = username ? d.prepare('SELECT * FROM users WHERE username = ?').get(username) : null;

  if (cmd === 'list') {
    for (const u of d.prepare('SELECT username, created_at FROM users').all()) console.log(`${u.username}\t${u.created_at}`);
  } else if (cmd === 'add' && username) {
    if (user) throw new Error('User already exists');
    if (!/^[a-zA-Z0-9._-]{2,40}$/.test(username)) throw new Error('Username: 2-40 letters, numbers, . _ -');
    const pw = await askPassword();
    const secret = auth.newTotpSecret();
    d.prepare('INSERT INTO users (username, password_hash, totp_secret) VALUES (?, ?, ?)').run(username, auth.hashPassword(pw), secret);
    db.audit('user_created', { detail: username });
    console.log(`Created ${username}.`);
    showTotp(username, secret);
  } else if (cmd === 'reset-2fa' && user) {
    const secret = auth.newTotpSecret();
    d.prepare('UPDATE users SET totp_secret = ? WHERE id = ?').run(secret, user.id);
    d.prepare('DELETE FROM sessions WHERE user_id = ?').run(user.id);
    db.audit('totp_reset', { userId: user.id });
    showTotp(username, secret);
  } else if (cmd === 'password' && user) {
    const pw = await askPassword();
    d.prepare('UPDATE users SET password_hash = ? WHERE id = ?').run(auth.hashPassword(pw), user.id);
    d.prepare('DELETE FROM sessions WHERE user_id = ?').run(user.id);
    db.audit('password_reset_cli', { userId: user.id });
    console.log('Password updated; existing sessions signed out.');
  } else if (cmd === 'remove' && user) {
    d.prepare('DELETE FROM users WHERE id = ?').run(user.id);
    db.audit('user_removed', { detail: username });
    console.log(`Removed ${username}.`);
  } else {
    console.log('Usage: npm run user -- add|reset-2fa|password|remove <username>   |   npm run user -- list');
    if (username && !user) console.log(`No such user: ${username}`);
    process.exitCode = 1;
  }
}

main()
  .catch((e) => { console.error(e.message); process.exitCode = 1; })
  .finally(() => rl?.close());
