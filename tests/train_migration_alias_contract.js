'use strict';

const assert = require('node:assert/strict');
const alias = 'D:\\Projects\\Tracker\\tasks\\2026\\AIDEV-79';
const task = '/home/test/data/tasks/2026/AIDEV-79';
process.env.TT_PATH_MAP_JSON = JSON.stringify([
  { disk: task, win: alias, runtime: '/root/task' },
  { disk: task + '/update', win: alias + '\\update', runtime: '/workspace/task/update' },
]);
process.env.TRACKER_TASK_KEY = 'AIDEV-79';

const view = require('/opt/agent-bundle/marketplaces/ai-marketplace/plugins/train-driver/lib/train-path-view.js');
const source = { input: { key: 'AIDEV-79' }, task_folder: alias,
  approval: { path: alias + '\\ai_actions\\train-run.yaml' } };
const runtime = view.toRuntime(source);
assert.equal(runtime.task_folder, '/root/task');
assert.equal(runtime.approval.path, '/root/task/ai_actions/train-run.yaml');
assert.deepEqual(view.toDisk(runtime), source);
assert.deepEqual(view.assertBoundKey(source), source);
assert.throws(() => view.toRuntime({ task_folder: alias + '\\..\\AIDEV-57' }));
assert.throws(() => view.toRuntime({ task_folder: 'D:\\Projects\\Tracker\\tasks\\2026\\AIDEV-57' }));
console.log(JSON.stringify({ exactAliasRoundtrip: true, pathTraversalDenied: true,
  foreignTaskDenied: true, controllerWiringStillRequired: true }));
