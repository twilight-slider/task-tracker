import { spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import net from 'node:net';

const file = join(process.env.CODEX_HOME, 'plugin-env.json');
let values = {};
try {
  values = JSON.parse(readFileSync(file, 'utf8'));
} catch (error) {
  if (error.code !== 'ENOENT') throw error;
}
if (!values || Array.isArray(values) || typeof values !== 'object') {
  throw new Error('Invalid agent environment file');
}
for (const [name, value] of Object.entries(values)) {
  if (!/^[A-Z][A-Z0-9_]{0,63}$/.test(name) || typeof value !== 'string' || !value || value.includes('\0') ||
      ['HOME', 'CODEX_HOME', 'PATH', 'LD_PRELOAD', 'NODE_OPTIONS', 'DOCKER_HOST'].includes(name)) {
    throw new Error('Invalid agent environment variable');
  }
}
if (process.argv.length < 3) throw new Error('Agent command is required');
const child = spawn(process.argv[2], process.argv.slice(3), {
  env: { ...process.env, ...values }, stdio: 'inherit',
});
if (process.env.TT_TRAIN_HELPER === '1') {
  let misses = 0;
  let checking = false;
  const watchdog = setInterval(() => {
    if (checking) return;
    checking = true;
    const socket = net.createConnection('/run/td/helper.sock');
    let settled = false;
    const finish = ok => {
      if (settled) return;
      settled = true;
      socket.destroy();
      checking = false;
      misses = ok ? 0 : misses + 1;
      if (misses >= 3) {
        clearInterval(watchdog);
        console.error('Train Driver helper unavailable');
        child.kill('SIGTERM');
        process.exit(70);
      }
    };
    socket.setTimeout(750, () => finish(false));
    socket.once('connect', () => finish(true));
    socket.once('error', () => finish(false));
  }, 1000);
  child.once('exit', () => clearInterval(watchdog));
}
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => child.kill(signal));
child.on('error', error => { console.error(error.message); process.exit(127); });
child.on('exit', (code, signal) => process.exit(code ?? (signal === 'SIGTERM' ? 143 : 130)));
