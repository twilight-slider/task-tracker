'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const fsPromises = require('node:fs/promises');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { execFileSync, spawnSync } = require('node:child_process');
const { createSnapshot, getSnapshot, compareSnapshot } = require('../src/result-snapshot-store');

const base = path.join(__dirname, '..', '.runtime', 'tests', 'snapshot-service');
fs.mkdirSync(base, { recursive: true });
const root = fs.mkdtempSync(path.join(base, 'run-'));
process.env.GIT_CEILING_DIRECTORIES = root;
const project = path.join(root, 'project');
const task = path.join(root, 'task');
fs.mkdirSync(project);
fs.mkdirSync(path.join(task, '.protected', 'snapshots'), { recursive: true });
const file = path.join(project, 'note.txt');
fs.writeFileSync(file, Buffer.from('\ufeffone\r\ntwo\r\n'));

(async () => {
  try {
    const created = await createSnapshot(task, project);
    const snapshot = await getSnapshot(task, created.snapshot.snapshot_id);
    const hash = createHash('sha256').update('one\ntwo').digest('hex');
    const fingerprint = createHash('sha256').update(JSON.stringify([
      'result-snapshot-v2', [['note.txt', 'text', hash]]
    ])).digest('hex');
    assert.equal(snapshot.schema_version, 2);
    assert.equal(snapshot.files[0].sha256, hash);
    assert.equal(snapshot.fingerprint.value, fingerprint);
    assert.equal((await compareSnapshot(task, snapshot.snapshot_id)).status, 'current');
    const stored = path.join(task, '.protected', 'snapshots', `${snapshot.snapshot_id}.json`);
    const damaged = { ...snapshot, files: [{ ...snapshot.files[0], sha256: '0'.repeat(64) }] };
    fs.writeFileSync(stored, JSON.stringify(damaged));
    await assert.rejects(compareSnapshot(task, snapshot.snapshot_id), { code: 'INTERNAL_ERROR' });
    fs.writeFileSync(stored, JSON.stringify(snapshot));
    fs.writeFileSync(file, 'changed');
    const stale = await compareSnapshot(task, snapshot.snapshot_id);
    assert.equal(stale.status, 'stale');
    assert.deepEqual(stale.changes.modified, ['note.txt']);

    const outside = path.join(root, 'outside');
    fs.mkdirSync(outside);
    const junction = path.join(project, 'junction');
    fs.symlinkSync(outside, junction, 'junction');
    await assert.rejects(createSnapshot(task, project), { code: 'UNSUPPORTED_FILE_TYPE' });
    fs.unlinkSync(junction);

    const raceProject = path.join(root, 'race-project');
    fs.mkdirSync(raceProject);
    const raceFile = path.join(raceProject, 'race.txt');
    fs.writeFileSync(raceFile, 'before');
    const originalLstat = fsPromises.lstat;
    let fileStats = 0;
    fsPromises.lstat = async (...args) => {
      if (args[0] === raceFile && ++fileStats === 2) fs.writeFileSync(raceFile, 'after!');
      return originalLstat(...args);
    };
    try {
      await assert.rejects(createSnapshot(task, raceProject), { code: 'CONCURRENT_CHANGE' });
    } finally {
      fsPromises.lstat = originalLstat;
    }

    const gitProject = path.join(root, 'git-project');
    fs.mkdirSync(gitProject);
    execFileSync('git', ['init', '-q'], { cwd: gitProject });
    fs.writeFileSync(path.join(gitProject, 'tracked.txt'), 'tracked\n');
    execFileSync('git', ['add', 'tracked.txt'], { cwd: gitProject });
    fs.writeFileSync(path.join(gitProject, 'untracked.bin'), Buffer.from([0, 1, 2]));
    const git = (await createSnapshot(task, gitProject)).snapshot;
    assert.equal(git.project.mode, 'git');
    assert.deepEqual(git.files.map((entry) => entry.path), ['tracked.txt', 'untracked.bin']);
    assert.equal(git.files[1].kind, 'binary');

    const trackerRoot = path.join(root, 'tracker');
    const taskFolder = path.join(trackerRoot, 'tasks', '2026', 'TEST-1');
    fs.mkdirSync(path.join(taskFolder, '.protected', 'snapshots'), { recursive: true });
    fs.writeFileSync(path.join(trackerRoot, 'projects.json'), JSON.stringify({ schema_version: 1,
      projects: [{ project_key: 'TEST', source_type: 'NO_JIRA', next_issue_number: 2 }] }));
    fs.mkdirSync(path.join(taskFolder, 'input'));
    fs.writeFileSync(path.join(taskFolder, 'input', 'task.md'), '# test\n');
    const config = path.join(root, 'config.json');
    fs.writeFileSync(config, JSON.stringify({ schemaVersion: 1, trackerRoot,
      tasksRoot: path.join(trackerRoot, 'tasks'), protectedRoot: path.join(trackerRoot, '.protected'),
      snapshotRoots: [gitProject] }));
    const worker = (method, args) => {
      const result = spawnSync(process.execPath, [path.join(__dirname, '..', 'src', 'folder-worker.js'), config],
        { input: `${JSON.stringify({ method, arguments: args })}\n`, encoding: 'utf8' });
      assert.equal(result.status, 0, result.stderr);
      return JSON.parse(result.stdout.trim());
    };
    const createdByWorker = worker('create_result_snapshot', { key: 'TEST-1', project_root: gitProject });
    assert.equal(createdByWorker.ok, true, JSON.stringify(createdByWorker));
    assert.equal(worker('get_result_snapshot', { key: 'TEST-1', snapshot_id: createdByWorker.data.snapshot_id }).data.project.mode, 'git');
    const summary = worker('get_result_snapshot', { key: 'TEST-1',
      snapshot_id: createdByWorker.data.snapshot_id, summary_only: true });
    assert.equal(summary.ok, true, JSON.stringify(summary));
    assert.equal(summary.data.snapshot_id, createdByWorker.data.snapshot_id);
    assert.equal(summary.data.fingerprint.value, createdByWorker.data.fingerprint.value);
    assert.equal(summary.data.files_count, 2);
    assert.equal(Object.hasOwn(summary.data, 'files'), false);
    assert.equal(worker('get_result_snapshot', { key: 'TEST-1',
      snapshot_id: createdByWorker.data.snapshot_id, summary_only: 'true' }).code, 'INVALID_REQUEST');
    const largeId = 'rs_00000000-0000-0000-0000-000000000002';
    const largeFiles = Array.from({ length: 9000 }, (_, index) => ({
      path: `file-${String(index).padStart(5, '0')}.txt`, kind: 'text', sha256: 'a'.repeat(64)
    }));
    const largeFingerprint = createHash('sha256').update(JSON.stringify([
      'result-snapshot-v2', largeFiles.map((entry) => [entry.path, entry.kind, entry.sha256])
    ])).digest('hex');
    fs.writeFileSync(path.join(taskFolder, '.protected', 'snapshots', `${largeId}.json`), JSON.stringify({
      schema_version: 2, snapshot_id: largeId, algorithm_version: 'result-snapshot-v2',
      fingerprint: { algorithm: 'sha256', value: largeFingerprint },
      project: { root: gitProject, mode: 'git' }, files_count: largeFiles.length,
      files: largeFiles, created_at: new Date().toISOString()
    }));
    assert.equal(worker('get_result_snapshot', { key: 'TEST-1', snapshot_id: largeId }).code, 'SNAPSHOT_TOO_LARGE');
    const compactLarge = worker('get_result_snapshot', { key: 'TEST-1', snapshot_id: largeId, summary_only: true });
    assert.equal(compactLarge.ok, true, JSON.stringify(compactLarge));
    assert.equal(compactLarge.data.files_count, largeFiles.length);
    assert.equal(compactLarge.data.fingerprint.value, largeFingerprint);
    assert.equal(Object.hasOwn(compactLarge.data, 'files'), false);
    assert.equal(worker('compare_result_snapshot', { key: 'TEST-1', snapshot_id: createdByWorker.data.snapshot_id }).data.status, 'current');
    assert.equal(worker('create_result_snapshot', { key: 'TEST-1', project_root: project }).code, 'PROJECT_ROOT_FORBIDDEN');
    assert.equal(worker('get_result_snapshot', { key: 'TEST-2', snapshot_id: createdByWorker.data.snapshot_id }).ok, false);
    console.log('snapshot service checks passed');
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
