'use strict';

const path = require('node:path');
const readline = require('node:readline');

const configPath = process.argv[2];
if (!configPath || !path.isAbsolute(configPath)) throw new Error('Installed config path is required');
process.env.TASK_FOLDER_MCP_CONFIG = configPath;
const folder = require('./task-folder');

const methods = {
  get_tasks_folder: () => folder.getTasksFolder(),
  get_task_projects: () => folder.getTaskProjects(),
  register_task_project: (args) => folder.registerTaskProject(args),
  create_task_folder: ({ key }) => folder.createTaskFolder(key),
  create_local_task_folder: (args) => folder.createLocalTaskFolder(args),
  create_task_subdirectory: (args) => folder.createTaskSubdirectory(args)
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
