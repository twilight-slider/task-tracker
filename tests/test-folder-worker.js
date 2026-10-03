'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync, execFileSync } = require('node:child_process');

const testRoot = path.join(__dirname, '..', '.runtime', 'tests', 'test-folder-worker');
fs.mkdirSync(testRoot, { recursive: true });
const root = fs.mkdtempSync(path.join(testRoot, 'run-'));
const tasksRoot = path.join(root, 'tasks');
const protectedRoot = path.join(root, 'protected');
const configPath = path.join(root, 'config.json');
fs.mkdirSync(tasksRoot);
fs.mkdirSync(protectedRoot);
const sid = execFileSync('whoami.exe', ['/user', '/fo', 'csv', '/nh'], { encoding: 'utf8' }).match(/S-1-\d+(?:-\d+)*/)?.[0];
assert.ok(sid, 'Windows user SID is required');
fs.writeFileSync(configPath, JSON.stringify({ schemaVersion: 1, trackerRoot: root, tasksRoot, protectedRoot,
  serviceAccountSid: sid, agentSid: 'S-1-5-32-545' }));

function call(method, args = {}) {
  const worker = path.join(__dirname, '..', 'src', 'folder-worker.js');
  const process = spawnSync(processExec(), [worker, configPath], {
    input: `${JSON.stringify({ method, arguments: args })}\n`, encoding: 'utf8', timeout: 10000
  });
  assert.equal(process.status, 0, process.stderr);
  return JSON.parse(process.stdout.trim());
}
function processExec() { return process.execPath; }

try {
  assert.equal(call('register_task_project', { project_key: 'TEST', source_type: 'NO_JIRA' }).data.status, 'created');
  assert.equal(fs.existsSync(path.join(root, 'projects.json')), true);
  assert.equal(fs.existsSync(path.join(protectedRoot, 'projects.json')), false);
  assert.equal(call('register_task_project', { source_type: 'NO_JIRA', project_key: 'TEST' }).data.status, 'already_exists');
  assert.equal(call('register_task_project', { project_key: 'TEST', source_type: 'JIRA_SERVER', jira_host: 'https://jira.example.test' }).code, 'PROJECT_CONFLICT');
  const input = { project_key: 'TEST', title: 'One', statement: 'Test statement' };
  const first = call('create_local_task_folder', input);
  assert.equal(first.ok, true);
  assert.equal(first.data.key, 'TEST-1');
  assert.equal(call('create_local_task_folder', input).data.key, 'TEST-2');
  assert.equal(fs.existsSync(path.join(first.data.taskFolder, 'input', 'materials')), true, 'service must create ordinary folders');
  assert.equal(fs.existsSync(path.join(first.data.taskFolder, '.protected', 'snapshots')), true);
  assert.equal(fs.existsSync(path.join(first.data.taskFolder, 'ai_actions', 'result-snapshots')), false);
  const aclCommand = '$ErrorActionPreference="Stop";$p=$env:A60_ACL_PATH;$sid=[Security.Principal.SecurityIdentifier]"S-1-5-32-545";' +
    '$a=Get-Acl -LiteralPath $p;' +
    '$a.Access | Where-Object { $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -eq $sid.Value } | ' +
    'Select-Object @{N="rights";E={$_.FileSystemRights.ToString()}},@{N="inherit";E={$_.InheritanceFlags.ToString()}},@{N="propagate";E={$_.PropagationFlags.ToString()}} | ConvertTo-Json -Compress';
  const taskAcl = execFileSync('C:\\Program Files\\PowerShell\\7\\pwsh.exe',
    ['-NoProfile', '-Command', aclCommand], { encoding: 'utf8', env: { ...process.env, A60_ACL_PATH: first.data.taskFolder } });
  assert.match(taskAcl, /CreateFiles/);
  assert.doesNotMatch(taskAcl, /CreateDirectories|DeleteSubdirectoriesAndFiles|ChangePermissions|TakeOwnership/);
  const protectedAcl = execFileSync('C:\\Program Files\\PowerShell\\7\\pwsh.exe',
    ['-NoProfile', '-Command', aclCommand], { encoding: 'utf8',
      env: { ...process.env, A60_ACL_PATH: path.join(first.data.taskFolder, '.protected') } });
  assert.equal(protectedAcl.trim(), '');
  const ordinaryFile = path.join(first.data.taskFolder, 'input', 'materials', 'sample.txt');
  fs.writeFileSync(ordinaryFile, 'sample');
  const fileAcl = execFileSync('C:\\Program Files\\PowerShell\\7\\pwsh.exe',
    ['-NoProfile', '-Command', aclCommand], { encoding: 'utf8', env: { ...process.env, A60_ACL_PATH: ordinaryFile } });
  assert.match(fileAcl, /FullControl/);
  execFileSync('icacls.exe', [first.data.taskFolder, '/grant', '*S-1-5-32-545:(F)']);
  assert.equal(call('create_task_subdirectory', { key: 'TEST-1', relative_path: 'input/materials' }).ok, true);
  const repairedAcl = execFileSync('C:\\Program Files\\PowerShell\\7\\pwsh.exe',
    ['-NoProfile', '-Command', aclCommand], { encoding: 'utf8', env: { ...process.env, A60_ACL_PATH: first.data.taskFolder } });
  const effectiveRules = JSON.parse(repairedAcl).filter((rule) => rule.propagate !== 'InheritOnly');
  assert.equal(effectiveRules.length, 1);
  assert.doesNotMatch(effectiveRules[0].rights, /FullControl|CreateDirectories|DeleteSubdirectoriesAndFiles|ChangePermissions|TakeOwnership/);
  assert.equal(call('create_task_subdirectory', { key: 'TEST-1', relative_path: 'update/reports' }).ok, true);
  const extra = call('create_task_subdirectory', { key: 'TEST-1', relative_path: 'update/reports/2026' });
  assert.equal(extra.ok, true, JSON.stringify(extra));
  assert.equal(fs.existsSync(path.join(first.data.taskFolder, 'update', 'reports', '2026')), true);
  for (const forbidden of ['reports', 'ai_actions/result-snapshots', 'ai_actions/nested/file',
    '.protected/snapshots/extra', 'update/missing/child']) {
    assert.equal(call('create_task_subdirectory', { key: 'TEST-1', relative_path: forbidden }).ok, false, forbidden);
  }
  assert.equal(fs.existsSync(path.join(first.data.taskFolder, 'reports')), false);
  assert.equal(fs.existsSync(path.join(first.data.taskFolder, 'update', 'missing')), false);
  const outside = path.join(root, 'outside');
  fs.mkdirSync(outside);
  fs.symlinkSync(outside, path.join(first.data.taskFolder, 'update', 'link'), 'junction');
  assert.equal(call('create_task_subdirectory', { key: 'TEST-1', relative_path: 'update/link/escape' }).code,
    'TASK_PATH_INVALID');
  assert.equal(fs.existsSync(path.join(outside, 'escape')), false);
  assert.equal(call('create_task_subdirectory', { key: 'TEST-1', relative_path: '../escape' }).code, 'TASK_PATH_INVALID');
  assert.equal(call('create_task_subdirectory', { key: 'TEST-1', relative_path: '.protected/escape' }).code, 'TASK_PATH_INVALID');
  assert.equal(call('create_local_task_folder', input).data.key, 'TEST-3');
  assert.equal(call('register_task_project', { project_key: 'JIRATEST', source_type: 'JIRA_SERVER', jira_host: 'https://jira.example.test' }).ok, true);
  const jira = call('create_task_folder', { key: 'JIRATEST-7' });
  assert.equal(jira.ok, true);
  assert.equal(jira.data.jiraHost, 'https://jira.example.test');
  assert.equal(fs.existsSync(path.join(jira.data.taskFolder, 'origin')), true, 'service must create Jira folders');
  assert.equal(fs.existsSync(path.join(jira.data.taskFolder, '.protected', 'snapshots')), true);
  assert.equal(fs.existsSync(path.join(jira.data.taskFolder, 'ai_actions', 'result-snapshots')), false);
  assert.equal(call('unknown', {}).code, 'INVALID_REQUEST');
  console.log('folder worker tests passed');
} finally {
  fs.rmSync(root, { recursive: true, force: true });
}
