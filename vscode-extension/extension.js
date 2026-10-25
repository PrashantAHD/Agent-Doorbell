// Companion for bridge.py: long-polls the local bridge and runs the VS Code
// commands that answer a Copilot prompt you replied to from your phone.
const vscode = require('vscode');
const http = require('http');
const fs = require('fs');

const CONFIG_PATH = process.env.AGENT_DOORBELL_CONFIG || __CONFIG_PATH__;
const ALLOWED = new Set([
  'workbench.action.chat.acceptTool',
  'workbench.action.chat.skipTool',
  'workbench.action.chat.submit',
]);
const INSTANCE = require('crypto').randomBytes(8).toString('hex'); // one per VS Code window
const crypto = require('crypto');
const EXT_VERSION = '10'; // must match EXT_VERSION in bridge.py
const BRIDGE_GONE_MS = 2 * 60 * 1000; // helper window closes itself if the bridge is gone this long
let stopped = false;
let isHelper = false; // set by the bridge when it opened this window itself
let lastOk = Date.now();

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// A window the user works in (folder or files open) is never closed automatically.
function isEmptyWindow() {
  const folders = vscode.workspace.workspaceFolders;
  if (folders && folders.length) return false;
  return !vscode.workspace.textDocuments.some(
    (d) => d.uri.scheme === 'file' || (d.uri.scheme === 'untitled' && d.isDirty));
}

async function closeThisWindow(reason) {
  stopped = true;
  console.log(`[agent-doorbell] closing helper window: ${reason}`);
  await vscode.commands.executeCommand('workbench.action.closeWindow');
}

const mac = (token, ...parts) => crypto.createHmac('sha256', token).update(parts.join('\n')).digest('hex');
const digest = (buf) => crypto.createHash('sha256').update(buf || Buffer.alloc(0)).digest('hex');

// The token is never sent: each request is signed with it, and an answer is used only if the
// bridge signed it back. Another program on the port can't learn the token or hand out jobs.
function call(method, path, body, timeoutMs) {
  return new Promise((resolve, reject) => {
    let cfg;
    try {
      cfg = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8'));
    } catch (e) {
      return reject(e);
    }
    const token = String(cfg.token || '');
    if (!token) return reject(new Error('no token in config'));
    const data = body ? Buffer.from(JSON.stringify(body)) : Buffer.alloc(0);
    const nonce = crypto.randomBytes(16).toString('hex');
    const ts = String(Math.floor(Date.now() / 1000));
    const headers = {
      'Content-Type': 'application/json', 'X-Ext-Version': EXT_VERSION,
      'X-Doorbell-Nonce': nonce, 'X-Doorbell-Time': ts,
      'X-Doorbell-Auth': mac(token, 'req', method, path, ts, nonce, digest(data)),
    };
    if (body) headers['Content-Length'] = data.length;
    const req = http.request(
      {
        host: '127.0.0.1', port: cfg.port || 47615, path, method, timeout: timeoutMs,
        headers,
      },
      (res) => {
        const chunks = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () => {
          const buf = Buffer.concat(chunks);
          const proof = String(res.headers['x-doorbell-proof'] || '');
          const want = mac(token, 'resp', nonce, digest(buf));
          if (!proof || proof.length !== want.length || !crypto.timingSafeEqual(Buffer.from(proof), Buffer.from(want))) {
            return reject(new Error('answer not signed by the bridge'));
          }
          try { resolve(JSON.parse(buf.toString('utf8') || '{}')); } catch { resolve({}); }
        });
      }
    );
    req.on('timeout', () => req.destroy(new Error('timeout')));
    req.on('error', reject);
    if (body) req.write(data);
    req.end();
  });
}

function activeTabInfo() {
  const tab = vscode.window.tabGroups.activeTabGroup && vscode.window.tabGroups.activeTabGroup.activeTab;
  const input = tab && tab.input;
  const uri = input && (input.uri || (input.modified && input.modified.uri));
  return { tab: tab ? tab.label : null, uri: uri ? uri.toString() : null, focused: vscode.window.state.focused };
}

async function runJob(job) {
  const errors = [];
  const info = {};
  if (!job.probe && !ALLOWED.has(job.command)) return { errors: ['command not allowed: ' + job.command], info };
  if (job.open && /^agent-host-session:\/\//.test(job.open)) {
    info.before = activeTabInfo();
    try {
      await vscode.commands.executeCommand('vscode.open', vscode.Uri.parse(job.open));
    } catch (e) {
      errors.push('open: ' + e.message);
    }
    // A session opened for the first time in this window needs a while to load before it accepts input.
    await sleep(Math.min(Number(job.openWait) || 2500, 30000));
    info.after = activeTabInfo();
  }
  if (job.probe) return { errors, info };
  if (job.command === 'workbench.action.chat.submit') {
    // Phone prompt: just send it. Answer to an open question form: stop the waiting turn first
    // (a message sent while the form is open cancels the question and gets lost).
    try {
      if (job.cancel !== false) {
        await vscode.commands.executeCommand('workbench.action.chat.cancel');
        await sleep(1500);
      }
      await vscode.commands.executeCommand(job.command, { inputValue: String(job.text || '') });
    } catch (e) {
      errors.push('submit: ' + e.message);
    }
    return { errors, info };
  }
  for (const r of job.resources || []) {
    if (job.guarded) {
      // Tied to one prompt: press only if the bridge confirms, right now, that exactly that prompt
      // is still the one waiting. Once it's answered, the remaining URI forms are not tried.
      let g;
      try {
        g = await call('POST', '/vscode/guard', { id: job.id }, 5000);
      } catch (e) {
        g = { ok: false, reason: e.message };
      }
      if (!g || g.ok !== true) {
        info.guard = (g && g.reason) || 'refused';
        break;
      }
    }
    try {
      // A session without a waiting prompt ignores this, so trying every URI form is safe.
      await vscode.commands.executeCommand(job.command, { sessionResource: vscode.Uri.parse(r) });
    } catch (e) {
      errors.push(r + ': ' + e.message);
    }
  }
  return { errors, info };
}

async function loop() {
  let after = -1;
  while (!stopped) {
    try {
      const empty = isEmptyWindow() ? 1 : 0;
      const res = await call('GET', `/vscode/jobs?after=${after}&instance=${INSTANCE}&empty=${empty}`, undefined, 40000);
      lastOk = Date.now();
      if (res.helper) isHelper = true;
      if (res.close) {
        if (!res.helper) {
          // A newer copy of this extension is running elsewhere: close this stale helper window,
          // but never a window you work in.
          if (isEmptyWindow()) {
            await closeThisWindow('outdated copy');
            return;
          }
          await sleep(25000);
          continue;
        }
        if (isEmptyWindow()) {
          await closeThisWindow('idle');
          return;
        }
        isHelper = false; // you started working in it, so it's yours now
      }
      if (after < 0) {
        after = typeof res.last === 'number' ? res.last : 0; // ignore jobs from before we connected
        continue;
      }
      for (const job of res.jobs || []) {
        if (job.id <= after) continue;
        after = job.id;
        const { errors, info } = await runJob(job);
        call('POST', '/vscode/result', { id: job.id, errors, info }, 5000).catch(() => {});
      }
    } catch {
      // Bridge not running. The helper window closes itself after a while; other windows just wait.
      if (isHelper && Date.now() - lastOk > BRIDGE_GONE_MS && isEmptyWindow()) {
        await closeThisWindow('bridge stopped');
        return;
      }
      await sleep(5000);
    }
  }
}

function activate(context) {
  loop();
  context.subscriptions.push({ dispose: () => { stopped = true; } });
}

function deactivate() {
  stopped = true;
}

module.exports = { activate, deactivate };
