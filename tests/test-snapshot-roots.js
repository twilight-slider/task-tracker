'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { assertSnapshotRoot } = require('../src/task-folder');

const base = path.join(__dirname, '..', '.runtime', 'tests', 'snapshot-roots');
fs.mkdirSync(base, { recursive: true });
const root = fs.mkdtempSync(path.join(base, 'run-'));
const trackerRoot = path.join(root, 'tracker');
const allowed = path.join(root, 'projects');
const project = path.join(allowed, 'one');
const outside = path.join(root, 'outside');
for (const dir of [trackerRoot, project, outside]) fs.mkdirSync(dir, { recursive: true });
const configPath = path.join(root, 'config.json');
fs.writeFileSync(configPath, JSON.stringify({ schemaVersion: 1, trackerRoot,
  tasksRoot: path.join(trackerRoot, 'tasks'), protectedRoot: path.join(trackerRoot, '.protected'),
  snapshotRoots: [allowed] }));
process.env.TASK_FOLDER_MCP_CONFIG = configPath;

(async () => {
  try {
    assert.equal(await assertSnapshotRoot(project), await fs.promises.realpath(project));
    await assert.rejects(assertSnapshotRoot(outside), { code: 'PROJECT_ROOT_FORBIDDEN' });
    await assert.rejects(assertSnapshotRoot(trackerRoot), { code: 'PROJECT_ROOT_FORBIDDEN' });
    await assert.rejects(assertSnapshotRoot(path.join(root, 'missing')), { code: 'PROJECT_NOT_FOUND' });
    console.log('snapshot root checks passed');
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
