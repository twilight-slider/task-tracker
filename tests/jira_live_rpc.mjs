import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';

const plugin = '/opt/agent-bundle/marketplaces/ai-marketplace/plugins/jira';
const child = spawn(process.execPath, ['--use-system-ca', `${plugin}/scripts/server.mjs`],
  { cwd: plugin, env: process.env, stdio: ['pipe', 'pipe', 'ignore'] });
let nextId = 0;
const pending = new Map();
createInterface({ input: child.stdout }).on('line', line => {
  let value;
  try { value = JSON.parse(line); } catch { return; }
  const waiter = pending.get(value.id);
  if (waiter) { pending.delete(value.id); waiter(value); }
});
function call(method, params = {}) {
  const id = ++nextId;
  return Promise.race([
    new Promise(resolve => {
      pending.set(id, resolve);
      child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
    }),
    new Promise((_, reject) => setTimeout(() => reject(new Error('Jira MCP timeout')), 15000))
  ]);
}
try {
  if (!(await call('initialize', { protocolVersion: '2025-06-18', capabilities: {},
    clientInfo: { name: 'jira-live-probe', version: '1' } })).result) throw new Error('MCP initialization failed');
  child.stdin.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) + '\n');
  const response = await call('tools/call', { name: 'jira_get_issue', arguments: { key: 'AIDEV-79' } });
  const data = response.result?.content?.[0]?.text;
  if (response.result?.isError || !data) throw new Error('Jira read failed');
  const status = JSON.parse(data).status;
  if (!['found', 'not_found', 'partial_failure'].includes(status)) throw new Error('Invalid Jira status');
  console.log(JSON.stringify({ liveReadAttempted: true, status }));
} finally {
  child.kill('SIGTERM');
}
