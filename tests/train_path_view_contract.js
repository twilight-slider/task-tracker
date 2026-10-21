'use strict';

const assert = require('node:assert/strict');
const view = require('/opt/agent-bundle/marketplaces/ai-marketplace/plugins/train-driver/lib/train-path-view');
process.env.TT_PATH_MAP_JSON = JSON.stringify([
  {disk:'/mnt/d/stage/task', win:'D:\\stage\\task', runtime:'/root/task'},
  {disk:'/mnt/d/stage/catalog', win:'D:\\stage\\catalog', runtime:'/opt/train-catalog'},
  {disk:'/mnt/d/stage/repo', win:'D:\\stage\\repo', runtime:'/workspace/repos/assigned'},
]);
process.env.TRACKER_TASK_KEY = 'AIDEV-79';
const disk = {input:{key:'AIDEV-79', task:{task_folder:'D:\\stage\\task'}},
  train_repository:'D:\\stage\\catalog',
  case:{target:{repository:'D:\\stage\\repo'}, iterations:[{snapshot:{project_root:'D:\\stage\\repo'}}]},
  design:{tz:{acceptance:{path:'D:\\stage\\task\\update\\tz.md', sha256:'a'.repeat(64)}}},
  history:[{snapshot:{path:'D:\\stage\\task\\update\\tz.md'},
    note:'D:\\stage\\task\\unchanged', sha256:'b'.repeat(64)}]};
const runtime = view.assertBoundKey(view.toRuntime(disk));
assert.equal(runtime.input.task.task_folder, '/root/task');
assert.equal(runtime.train_repository, '/opt/train-catalog');
assert.equal(runtime.case.target.repository, '/workspace/repos/assigned');
assert.equal(runtime.design.tz.acceptance.path, '/root/task/update/tz.md');
assert.equal(runtime.history[0].note, disk.history[0].note);
assert.equal(runtime.history[0].sha256, disk.history[0].sha256);
assert.deepEqual(view.toDisk(runtime), disk);
assert.throws(() => view.toRuntime({path:'D:\\other\\secret'}), /outside assigned mounts/);
assert.throws(() => view.toRuntime({path:'/etc/passwd'}), /outside assigned mounts/);
assert.throws(() => view.toRuntime({train_repository:'D:\\stage\\catalog\\..\\..\\other'}), /Noncanonical/);
assert.throws(() => view.toRuntime({path:'/root/task/../../etc/passwd'}), /Noncanonical/);
assert.throws(() => view.toDisk({path:'/root/task/../other'}), /Noncanonical/);
assert.throws(() => view.assertBoundKey({input:{key:'AIDEV-57'}}), /Foreign TrainRun task key/);
console.log('train-path-view-contract PASS');
