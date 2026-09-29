'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawn, execFileSync } = require('node:child_process');

async function main() {
  const base = path.join(__dirname, '..', '.runtime', 'tests', 'test-local-task-counter');
  fs.mkdirSync(base, { recursive: true });
  const root = fs.mkdtempSync(path.join(base, 'run-'));
  const tasksRoot = path.join(root, 'tasks');
  const protectedRoot = path.join(root, 'protected');
  fs.mkdirSync(tasksRoot);
  fs.mkdirSync(protectedRoot);
  const sid = execFileSync('whoami.exe', ['/user', '/fo', 'csv', '/nh'], { encoding: 'utf8' }).match(/S-1-\d+(?:-\d+)*/)?.[0];
  const configPath = path.join(root, 'service.json');
  fs.writeFileSync(configPath, JSON.stringify({ schemaVersion: 1, trackerRoot: root, tasksRoot, protectedRoot,
    serviceAccountSid: sid, agentSid: 'S-1-5-32-545' }));
  const worker = path.join(__dirname, '..', 'src', 'folder-worker.js');
  function call(method, args) {
    return new Promise((resolve, reject) => {
      const child = spawn(process.execPath, [worker, configPath], { stdio: ['pipe', 'pipe', 'pipe'] });
      let output = '';
      let error = '';
      child.stdout.on('data', (chunk) => { output += chunk; });
      child.stderr.on('data', (chunk) => { error += chunk; });
      child.on('close', (code) => {
        if (code !== 0) return reject(new Error(error || `Worker exit ${code}`));
        try { resolve(JSON.parse(output.trim())); } catch (parseError) { reject(parseError); }
      });
      child.stdin.end(`${JSON.stringify({ method, arguments: args })}\n`);
    });
  }
  try {
    assert.equal((await call('register_task_project', { project_key: 'TEST', source_type: 'NO_JIRA' })).ok, true);
    const requests = Array.from({ length: 3 }, () => call('create_local_task_folder', {
      project_key: 'TEST', title: 'Same title', statement: 'Same statement'
    }));
    const results = await Promise.all(requests);
    assert.ok(results.every((result) => result.ok), JSON.stringify(results));
    assert.deepEqual(results.map((result) => result.data.key).sort(), ['TEST-1', 'TEST-2', 'TEST-3']);
    assert.equal(JSON.parse(fs.readFileSync(path.join(root, 'projects.json'), 'utf8')).projects[0].next_issue_number, 4);
    console.log('Parallel local task counter passed');
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
