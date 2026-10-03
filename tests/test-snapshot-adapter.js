'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const base = path.join(__dirname, '..', '.runtime', 'tests', 'snapshot-adapter');
fs.mkdirSync(base, { recursive: true });
const root = fs.mkdtempSync(path.join(base, 'run-'));
try {
  fs.copyFileSync(path.join(__dirname, '..', 'src', 'mcp-adapter.js'), path.join(root, 'mcp-adapter.js'));
  fs.writeFileSync(path.join(root, 'tracker-client.json'), '{"pipeName":"test-snapshot-adapter"}');
  const request = { jsonrpc: '2.0', id: 1, method: 'tools/list' };
  const list = (scope) => {
    const child = spawnSync(process.execPath, [path.join(root, 'mcp-adapter.js')], {
      input: `${JSON.stringify(request)}\n`, encoding: 'utf8',
      env: { ...process.env, ...(scope ? { TASK_TRACKER_MCP_SCOPE: scope } : {}) }
    });
    assert.equal(child.status, 0, child.stderr);
    return JSON.parse(child.stdout.trim()).result.tools.map((tool) => tool.name);
  };
  assert.deepEqual(list('snapshots'), ['create_result_snapshot', 'get_result_snapshot', 'compare_result_snapshot']);
  assert.equal(list().includes('create_result_snapshot'), false);
  console.log('snapshot adapter checks passed');
} finally {
  fs.rmSync(root, { recursive: true, force: true });
}
