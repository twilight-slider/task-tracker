import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import assert from 'node:assert/strict';

const plugin = '/opt/agent-bundle/marketplaces/ai-marketplace/plugins/jira';
const child = spawn(process.execPath,
  ['--import', '/opt/jira-test/jira_mock_fetch.mjs', `${plugin}/scripts/server.mjs`],
  { cwd: plugin, env: process.env, stdio: ['pipe', 'pipe', 'pipe'] });
const pending = new Map();
let nextId = 0;
const lines = createInterface({ input: child.stdout });
lines.on('line', line => {
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
    new Promise((_, reject) => setTimeout(() => reject(new Error(`timeout: ${method}`)), 10000))
  ]);
}
try {
  const init = await call('initialize', { protocolVersion: '2025-06-18', capabilities: {},
    clientInfo: { name: 'jira-smoke', version: '1' } });
  assert.ok(init.result);
  child.stdin.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) + '\n');
  const listed = await call('tools/list');
  assert.deepEqual(listed.result.tools.map(item => item.name).sort(),
    ['jira_get_issue', 'jira_search_issues']);
  const tool = (name, args) => call('tools/call', { name, arguments: args });
  const found = await tool('jira_get_issue', { key: 'ABC-1' });
  assert.equal(JSON.parse(found.result.content[0].text).status, 'found');
  const missing = await tool('jira_get_issue', { key: 'ABC-404' });
  assert.equal(JSON.parse(missing.result.content[0].text).status, 'not_found');
  const search = await tool('jira_search_issues', { jira_host: 'https://jira.example.test', jql: 'project = ABC' });
  assert.equal(JSON.parse(search.result.content[0].text).total, 1);
  const failed = await tool('jira_search_issues', { jira_host: 'https://jira.example.test', jql: 'fail' });
  assert.equal(failed.result.isError, true);
  const wrongHost = await tool('jira_get_issue', { key: 'ABC-1', jira_host: 'https://foreign.example.test' });
  assert.equal(wrongHost.result.isError, true);
  console.log(JSON.stringify({ tools: 2, positive: 2, negative: 3, tokenInDockerEnv: false }));
} finally {
  child.kill('SIGTERM');
}
