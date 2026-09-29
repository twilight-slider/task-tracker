'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const root = fs.mkdtempSync(path.join(os.tmpdir(), 'task-folder-mcp-'));
const tasksRoot = path.join(root, 'tasks');
const protectedRoot = path.join(root, 'protected');
const configPath = path.join(root, 'config.json');
fs.mkdirSync(tasksRoot);
fs.mkdirSync(protectedRoot);
fs.writeFileSync(configPath, JSON.stringify({ schemaVersion: 1, tasksRoot, protectedRoot }));

function call(method, args = {}, admin = false) {
  const worker = path.join(__dirname, '..', 'src', 'folder-worker.js');
  const process = spawnSync(processExec(), [worker, configPath, ...(admin ? ['--admin'] : [])], {
    input: `${JSON.stringify({ method, arguments: args })}\n`, encoding: 'utf8', timeout: 10000
  });
  assert.equal(process.status, 0, process.stderr);
  return JSON.parse(process.stdout.trim());
}
function processExec() { return process.execPath; }

try {
  assert.equal(call('register_task_project', { project_key: 'TEST', source_type: 'NO_JIRA', next_issue_number: 1 }).code, 'INVALID_REQUEST');
  assert.equal(call('register_task_project', { project_key: 'TEST', source_type: 'NO_JIRA', next_issue_number: 1 }, true).ok, true);
  const input = { project_key: 'TEST', title: 'One', statement: 'Test statement', request_id: 'test-request-0001' };
  const first = call('create_local_task_folder', input);
  assert.equal(first.ok, true);
  assert.equal(first.data.key, 'TEST-1');
  assert.equal(call('create_local_task_folder', input).data.key, 'TEST-1');
  assert.equal(call('create_local_task_folder', { ...input, title: 'Changed' }).code, 'REQUEST_ID_CONFLICT');
  assert.equal(fs.existsSync(first.data.taskFolder), false, 'privileged worker must not write ordinary folders');
  assert.equal(fs.existsSync(path.join(protectedRoot, 'TEST-1', 'train-run')), true);
  assert.equal(call('create_local_task_folder', { ...input, request_id: 'test-request-0002' }).data.key, 'TEST-2');
  assert.equal(call('register_task_project', { project_key: 'JIRATEST', source_type: 'JIRA_SERVER', jira_host: 'https://jira.example.test' }, true).ok, true);
  const jira = call('create_task_folder', { key: 'JIRATEST-7' });
  assert.equal(jira.ok, true);
  assert.equal(jira.data.jiraHost, 'https://jira.example.test');
  assert.equal(fs.existsSync(jira.data.taskFolder), false, 'privileged worker must not write Jira folders');
  assert.equal(fs.existsSync(path.join(protectedRoot, 'JIRATEST-7', 'snapshots')), true);
  assert.equal(call('unknown', {}).code, 'INVALID_REQUEST');
  console.log('folder worker tests passed');
} finally {
  fs.rmSync(root, { recursive: true, force: true });
}
