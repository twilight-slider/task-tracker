'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');
const readline = require('node:readline');
const { spawn } = require('node:child_process');

async function main() {
  const testRoot = path.join(__dirname, '..', '.runtime', 'tests', 'test-mcp-adapter');
  fs.mkdirSync(testRoot, { recursive: true });
  const root = fs.mkdtempSync(path.join(testRoot, 'run-'));
  const tasksRoot = path.join(root, 'tasks');
  fs.mkdirSync(tasksRoot);
  const pipeName = `task-folder-mcp-test-${process.pid}`;
  const server = net.createServer((socket) => {
    readline.createInterface({ input: socket }).once('line', (line) => {
      const { method, arguments: args } = JSON.parse(line);
      if (method === 'create_task_folder' && args.key === 'TFMTEST-1') {
        socket.end(`${JSON.stringify({ ok: false, code: 'PROJECT_SOURCE_CONFLICT' })}\n`);
        return;
      }
      let data;
      if (method === 'create_local_task_folder') {
        const taskFolder = path.join(tasksRoot, '2026', 'TFMTEST-1');
        data = { tasksFolder: tasksRoot, taskFolder, year: '2026', key: 'TFMTEST-1',
          projectKey: 'TFMTEST', sourceType: 'NO_JIRA', sourceReference: path.join(taskFolder, 'input', 'task.md'), status: 'reserved' };
      } else if (method === 'create_task_folder') {
        data = { tasksFolder: tasksRoot, taskFolder: path.join(tasksRoot, '2026', 'JIRATEST-7'),
          year: '2026', key: 'JIRATEST-7', jiraHost: 'https://jira.example.test', status: 'prepared' };
      } else if (method === 'get_task_projects') {
        data = { tasksFolder: tasksRoot, status: 'found', manifest: { schema_version: 1,
          projects: [{ project_key: 'TFMTEST', source_type: 'NO_JIRA', next_issue_number: 2 },
            { project_key: 'JIRATEST', source_type: 'JIRA_SERVER', jira_host: 'https://jira.example.test' }] } };
      } else if (method === 'get_tasks_folder') {
        data = tasksRoot;
      } else throw new Error(`Unexpected method ${method}`);
      socket.end(`${JSON.stringify({ ok: true, data })}\n`);
    });
  });
  await new Promise((resolve) => server.listen(`\\\\.\\pipe\\${pipeName}`, resolve));
  const adapter = path.join(root, 'mcp-adapter.js');
  fs.copyFileSync(path.join(__dirname, '..', 'src', 'mcp-adapter.js'), adapter);
  fs.writeFileSync(path.join(root, 'tracker-client.json'), JSON.stringify({ pipeName }));
  const child = spawn(process.execPath, [adapter], {
    env: { ...process.env, TASK_FOLDER_MCP_PIPE: 'wrong-pipe' }, stdio: ['pipe', 'pipe', 'inherit']
  });
  const output = readline.createInterface({ input: child.stdout });
  const pending = new Map();
  output.on('line', (line) => { const message = JSON.parse(line); pending.get(message.id)?.(message); pending.delete(message.id); });
  function request(id, method, params) {
    const done = new Promise((resolve) => pending.set(id, resolve));
    child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id, method, params })}\n`);
    return Promise.race([done, new Promise((_, reject) => setTimeout(() => reject(new Error('Adapter timeout')), 5000))]);
  }
  try {
    const args = { project_key: 'TFMTEST', title: 'One', statement: 'Test statement', request_id: 'test-request-0001' };
    const created = await request(1, 'tools/call', { name: 'create_local_task_folder', arguments: args });
    assert.equal(created.result.structuredContent.key, 'TFMTEST-1');
    assert.equal(fs.readFileSync(path.join(tasksRoot, '2026', 'TFMTEST-1', 'input', 'task.md'), 'utf8'), '# One\n\nTest statement\n');
    const resolved = await request(2, 'tools/call', { name: 'resolve_task_folder', arguments: { key: 'TFMTEST-1' } });
    assert.equal(resolved.result.structuredContent.key, 'TFMTEST-1');
    assert.equal(resolved.result.structuredContent.hasTrainRun, false);
    const jira = await request(3, 'tools/call', { name: 'create_task_folder', arguments: { key: 'JIRATEST-7' } });
    assert.equal(jira.result.structuredContent.status, 'created');
    assert.equal(fs.existsSync(path.join(tasksRoot, '2026', 'JIRATEST-7', 'origin')), true);
    const jiraRepeat = await request(10, 'tools/call', { name: 'create_task_folder', arguments: { key: 'JIRATEST-7' } });
    assert.equal(jiraRepeat.result.structuredContent.status, 'already_exists');
    const host = await request(4, 'tools/call', { name: 'set_task_jira_host', arguments: {
      key: 'JIRATEST-7', jira_host: 'https://jira.example.test'
    } });
    assert.equal(host.result.structuredContent.status, 'created');
    const hostRepeat = await request(5, 'tools/call', { name: 'set_task_jira_host', arguments: {
      key: 'JIRATEST-7', jira_host: 'https://jira.example.test'
    } });
    assert.equal(hostRepeat.result.structuredContent.status, 'already_set');
    const jiraResolved = await request(6, 'tools/call', { name: 'resolve_task_folder', arguments: { key: 'JIRATEST-7' } });
    assert.equal(jiraResolved.result.structuredContent.sourceReference, 'https://jira.example.test/browse/JIRATEST-7');
    const rootResult = await request(8, 'tools/call', { name: 'get_tasks_folder', arguments: {} });
    assert.equal(rootResult.result.structuredContent.tasksFolder, tasksRoot);
    const conflict = await request(9, 'tools/call', { name: 'set_task_jira_host', arguments: {
      key: 'TFMTEST-1', jira_host: 'https://jira.example.test'
    } });
    assert.equal(conflict.result.isError, true);
    assert.equal(conflict.result.structuredContent.code, 'PROJECT_SOURCE_CONFLICT');
    const wrongHost = await request(11, 'tools/call', { name: 'set_task_jira_host', arguments: {
      key: 'JIRATEST-7', jira_host: 'https://other.example.test'
    } });
    assert.equal(wrongHost.result.structuredContent.code, 'JIRA_HOST_CONFLICT');
    const blocked = await request(7, 'tools/call', { name: 'register_task_project', arguments: {} });
    assert.equal(blocked.result.structuredContent.code, 'UNKNOWN_TOOL');
    console.log('MCP adapter tests passed');
  } finally {
    child.kill();
    await new Promise((resolve) => server.close(resolve));
    fs.rmSync(root, { recursive: true, force: true });
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
