'use strict';

const path = require('node:path');
const readline = require('node:readline');
const fs = require('node:fs/promises');

const configPath = process.argv[2];
if (!configPath || !path.isAbsolute(configPath)) throw new Error('Installed config path is required');
process.env.TASK_FOLDER_MCP_CONFIG = configPath;
const folder = require('./task-folder');
const snapshots = require('./result-snapshot-store');

async function snapshotTask(key) {
  const task = await folder.resolveTaskFolder(key);
  if (!task.taskFolder) throw new folder.TaskFolderError('TASK_PATH_INVALID', `Task ${key} has no folder`);
  const volume = path.parse(task.tasksFolder).root;
  let current = volume;
  for (const part of task.tasksFolder.slice(volume.length).split(path.sep).filter(Boolean)) {
    current = path.join(current, part);
    const stat = await fs.lstat(current);
    if (!stat.isDirectory() || stat.isSymbolicLink()) {
      throw new folder.TaskFolderError('TASK_PATH_INVALID', 'Tracker tasks path must contain only plain directories');
    }
  }
  for (const part of [task.year, key, '.protected', 'snapshots']) {
    current = path.join(current, part);
    const stat = await fs.lstat(current);
    if (!stat.isDirectory() || stat.isSymbolicLink()) {
      throw new folder.TaskFolderError('TASK_PATH_INVALID', 'Snapshot task path must contain only plain directories');
    }
  }
  return task.taskFolder;
}

function bounded(data) {
  if (Buffer.byteLength(JSON.stringify(data)) > 800 * 1024) {
    throw new folder.TaskFolderError('SNAPSHOT_TOO_LARGE', 'Snapshot response exceeds service limit');
  }
  return data;
}

const methods = {
  get_tasks_folder: () => folder.getTasksFolder(),
  get_task_projects: () => folder.getTaskProjects(),
  register_task_project: (args) => folder.registerTaskProject(args),
  create_task_folder: ({ key }) => folder.createTaskFolder(key),
  create_local_task_folder: (args) => folder.createLocalTaskFolder(args),
  create_task_subdirectory: (args) => folder.createTaskSubdirectory(args),
  create_result_snapshot: async ({ key, project_root: root }) => {
    const task = await snapshotTask(key);
    const project = await folder.assertSnapshotRoot(root);
    const { snapshot } = await snapshots.createSnapshot(task, project);
    return { status: 'ok', snapshot_id: snapshot.snapshot_id, fingerprint: snapshot.fingerprint,
      files_count: snapshot.files_count, mode: snapshot.project.mode };
  },
  get_result_snapshot: async ({ key, snapshot_id: id }) => bounded(await snapshots.getSnapshot(await snapshotTask(key), id)),
  compare_result_snapshot: async ({ key, snapshot_id: id }) => bounded(await snapshots.compareSnapshot(
    await snapshotTask(key), id, folder.assertSnapshotRoot))
};

readline.createInterface({ input: process.stdin, crlfDelay: Infinity }).once('line', async (line) => {
  let response;
  try {
    if (Buffer.byteLength(line) > 1024 * 1024) throw Object.assign(new Error('Request too large'), { code: 'REQUEST_TOO_LARGE' });
    const request = JSON.parse(line);
    if (!request || typeof request !== 'object' || typeof request.method !== 'string' || !Object.hasOwn(methods, request.method) ||
        !request.arguments || typeof request.arguments !== 'object' || Array.isArray(request.arguments)) {
      throw Object.assign(new Error('Unknown method or invalid arguments'), { code: 'INVALID_REQUEST' });
    }
    response = { ok: true, data: await methods[request.method](request.arguments) };
  } catch (error) {
    response = { ok: false, code: error.code || 'INTERNAL_ERROR', message: error.message };
  }
  process.stdout.write(`${JSON.stringify(response)}\n`);
  process.exitCode = 0;
});
