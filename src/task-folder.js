'use strict';

const fs = require('node:fs/promises');
const fsSync = require('node:fs');
const path = require('node:path');

const PROJECT_KEY = /^[A-Z][A-Z0-9_-]*$/;
const ISSUE_KEY = /^([A-Z][A-Z0-9_-]*)-(\d+)$/;
const JIRA_HOST_FILE = '.jira-host';
const PROJECTS_FILE = 'projects.json';
const TASK_SUBDIRS = ['origin', 'update', 'ai_actions', 'retro'];
const LOCAL_SUBDIRS = ['input', path.join('input', 'materials'), 'update', 'ai_actions', 'retro'];

class TaskFolderError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = 'TaskFolderError';
    this.code = code;
    this.details = details;
  }
}

class TaskFolderConfigError extends TaskFolderError {
  constructor(message) {
    super('TASK_FOLDER_CONFIG_INVALID', message);
    this.name = 'TaskFolderConfigError';
  }
}

function getConfigPath() {
  const configPath = process.env.TASK_FOLDER_MCP_CONFIG;
  if (!configPath || !path.isAbsolute(configPath)) throw new TaskFolderConfigError('TASK_FOLDER_MCP_CONFIG must be an absolute path');
  return configPath;
}

function getConfig() {
  let config;
  try { config = JSON.parse(fsSync.readFileSync(getConfigPath(), 'utf8')); }
  catch (error) { throw new TaskFolderConfigError(`Cannot read service config: ${error.message}`); }
  if (config.schemaVersion !== 1 || !path.isAbsolute(config.tasksRoot || '') || !path.isAbsolute(config.protectedRoot || '')) {
    throw new TaskFolderConfigError('Service config has invalid storage roots');
  }
  return config;
}

async function getTasksFolder() {
  return getConfig().tasksRoot;
}

function getProtectedRoot() { return getConfig().protectedRoot; }

function getMoscowYear() {
  return new Intl.DateTimeFormat('en', { calendar: 'gregory', timeZone: 'Europe/Moscow', year: 'numeric' }).format(new Date());
}

async function isDirectory(target) {
  try { return (await fs.stat(target)).isDirectory(); }
  catch (error) { if (error.code === 'ENOENT') return false; throw error; }
}

async function isFile(target) {
  try { return (await fs.stat(target)).isFile(); }
  catch (error) { if (error.code === 'ENOENT') return false; throw error; }
}

async function plainDirectory(target, create = false) {
  try {
    const item = await fs.lstat(target);
    if (!item.isDirectory() || item.isSymbolicLink()) throw new TaskFolderError('TASK_PATH_INVALID', `Not a plain directory: ${target}`);
  } catch (error) {
    if (error.code !== 'ENOENT' || !create) throw error;
    await fs.mkdir(target);
    const item = await fs.lstat(target);
    if (!item.isDirectory() || item.isSymbolicLink()) throw new TaskFolderError('TASK_PATH_INVALID', `Not a plain directory: ${target}`);
  }
}

async function ensureProtectedTask(key) {
  const root = getProtectedRoot();
  await plainDirectory(root);
  const folder = path.join(root, key);
  await plainDirectory(folder, true);
  for (const name of ['snapshots', 'train-run']) await plainDirectory(path.join(folder, name), true);
}

function normalizeJiraHost(value) {
  if (typeof value !== 'string' || /[\r\n]/.test(value)) throw new Error('jira_host must be one HTTPS origin without line breaks');
  let url;
  try { url = new URL(value); }
  catch { throw new Error('jira_host must be one HTTPS origin without line breaks'); }
  if (url.protocol !== 'https:' || url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
    throw new Error('jira_host must be one HTTPS origin without line breaks');
  }
  return url.origin;
}

function ownKeys(value, allowed) {
  return value && typeof value === 'object' && !Array.isArray(value) &&
    Object.keys(value).every((key) => allowed.includes(key));
}

function invalidManifest(message) {
  throw new TaskFolderError('PROJECT_MANIFEST_INVALID', message);
}

function validateManifest(value) {
  if (!ownKeys(value, ['schema_version', 'projects']) || value.schema_version !== 1 || !Array.isArray(value.projects)) {
    invalidManifest('projects.json must contain only schema_version: 1 and projects array');
  }
  const seen = new Set();
  const projects = value.projects.map((project) => {
    if (!ownKeys(project, ['project_key', 'name', 'source_type', 'next_issue_number', 'jira_host']) ||
        !PROJECT_KEY.test(project.project_key || '') ||
        (project.name !== undefined && (typeof project.name !== 'string' || !project.name.trim())) ||
        !['NO_JIRA', 'JIRA_SERVER', 'JIRA_CLOUD'].includes(project.source_type)) {
      invalidManifest('projects.json contains an invalid project');
    }
    if (seen.has(project.project_key)) invalidManifest(`Duplicate project_key: ${project.project_key}`);
    seen.add(project.project_key);
    if (project.source_type === 'NO_JIRA') {
      if (!Number.isInteger(project.next_issue_number) || project.next_issue_number < 1 || 'jira_host' in project) {
        invalidManifest(`NO_JIRA project ${project.project_key} requires a positive next_issue_number and forbids jira_host`);
      }
      return { ...project };
    }
    if ('next_issue_number' in project || typeof project.jira_host !== 'string') {
      invalidManifest(`Jira project ${project.project_key} requires jira_host and forbids next_issue_number`);
    }
    try { return { ...project, jira_host: normalizeJiraHost(project.jira_host) }; }
    catch { invalidManifest(`Jira project ${project.project_key} has an invalid jira_host`); }
  });
  return { schema_version: 1, projects };
}

async function readManifest(tasksFolder, missingAllowed = false) {
  const file = path.join(getProtectedRoot(), PROJECTS_FILE);
  let text;
  try { text = await fs.readFile(file, 'utf8'); }
  catch (error) {
    if (error.code === 'ENOENT' && missingAllowed) return null;
    if (error.code === 'ENOENT') throw new TaskFolderError('PROJECT_MANIFEST_MISSING', `Create ${file} by registering a project`);
    throw error;
  }
  let value;
  try { value = JSON.parse(text.replace(/^\uFEFF/, '')); }
  catch { invalidManifest('projects.json is not valid JSON'); }
  return validateManifest(value);
}

async function writeManifest(tasksFolder, manifest) {
  await fs.mkdir(getProtectedRoot(), { recursive: true });
  const file = path.join(getProtectedRoot(), PROJECTS_FILE);
  const temporary = `${file}.${process.pid}.${Date.now()}.tmp`;
  try {
    await fs.writeFile(temporary, `${JSON.stringify(validateManifest(manifest), null, 2)}\n`, { encoding: 'utf8', flag: 'wx' });
    await fs.rename(temporary, file);
  } finally { await fs.rm(temporary, { force: true }); }
}

async function withManifestLock(tasksFolder, action) {
  // ponytail: one global lock is enough here; shard it per project only if contention becomes measurable.
  const lock = path.join(getProtectedRoot(), '.projects.lock');
  await fs.mkdir(getProtectedRoot(), { recursive: true });
  for (let attempt = 0; attempt < 40; attempt += 1) {
    try {
      await fs.mkdir(lock);
      try { return await action(); }
      finally { await fs.rmdir(lock); }
    } catch (error) {
      if (error.code !== 'EEXIST') throw error;
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
  }
  throw new TaskFolderError('PROJECT_MANIFEST_BUSY', 'projects.json is locked by another operation');
}

function parseIssueKey(key) {
  const match = typeof key === 'string' && key.match(ISSUE_KEY);
  if (!match || Number(match[2]) < 1) throw new TaskFolderError('TASK_PATH_INVALID', 'key must be <PROJECT_KEY>-<positive number>');
  return { key, projectKey: match[1], issueNumber: Number(match[2]) };
}

async function findTaskFolders(tasksFolder, key) {
  let entries = [];
  try { entries = await fs.readdir(tasksFolder, { withFileTypes: true }); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  const matches = [];
  for (const entry of entries.filter((item) => item.isDirectory() && /^\d{4}$/.test(item.name))) {
    const taskFolder = path.join(tasksFolder, entry.name, key);
    if (await isDirectory(taskFolder)) matches.push({ year: entry.name, taskFolder });
  }
  return matches;
}

async function getTaskProjects() {
  const tasksFolder = await getTasksFolder();
  const manifest = await readManifest(tasksFolder, true);
  return manifest ? { tasksFolder, status: 'found', manifest } : { tasksFolder, status: 'missing', manifest: null };
}

async function registerTaskProject(input) {
  if (!ownKeys(input, ['project_key', 'name', 'source_type', 'next_issue_number', 'jira_host'])) {
    throw new TaskFolderError('PROJECT_MANIFEST_INVALID', 'Unknown register_task_project field');
  }
  const tasksFolder = await getTasksFolder();
  return withManifestLock(tasksFolder, async () => {
    const current = await readManifest(tasksFolder, true) || { schema_version: 1, projects: [] };
    if (current.projects.some((project) => project.project_key === input.project_key)) {
      throw new TaskFolderError('PROJECT_ALREADY_EXISTS', `Project ${input.project_key} already exists`);
    }
    const manifest = validateManifest({ schema_version: 1, projects: [...current.projects, input] });
    await writeManifest(tasksFolder, manifest);
    return { tasksFolder, status: 'created', project: manifest.projects.at(-1) };
  });
}

async function readJiraHost(taskFolder) {
  try { return normalizeJiraHost((await fs.readFile(path.join(taskFolder, JIRA_HOST_FILE), 'utf8')).trim()); }
  catch (error) { if (error.code === 'ENOENT') return undefined; throw error; }
}

async function createTaskFolder(key) {
  const { projectKey } = parseIssueKey(key);
  const tasksFolder = await getTasksFolder();
  const manifest = await readManifest(tasksFolder);
  const project = manifest.projects.find((item) => item.project_key === projectKey);
  if (!project) throw new TaskFolderError('PROJECT_NOT_FOUND', `Project ${projectKey} is not registered`);
  if (project.source_type === 'NO_JIRA') throw new TaskFolderError('PROJECT_SOURCE_CONFLICT', `Project ${projectKey} is NO_JIRA`);
  const year = getMoscowYear();
  const taskFolder = path.join(tasksFolder, year, key);
  await ensureProtectedTask(key);
  return { tasksFolder, taskFolder, year, key, jiraHost: project.jira_host, status: 'prepared' };
}

async function setTaskJiraHost(key, jiraHost) {
  const { projectKey } = parseIssueKey(key);
  const tasksFolder = await getTasksFolder();
  const manifest = await readManifest(tasksFolder, true);
  const project = manifest?.projects.find((item) => item.project_key === projectKey);
  if (project?.source_type === 'NO_JIRA') {
    throw new TaskFolderError('PROJECT_SOURCE_CONFLICT', `Project ${projectKey} is NO_JIRA`);
  }
  const normalized = normalizeJiraHost(jiraHost);
  const { taskFolder } = await createTaskFolder(key);
  const current = await readJiraHost(taskFolder);
  if (current === normalized) return { key, taskFolder, jiraHost: normalized, status: 'already_set' };
  await fs.writeFile(path.join(taskFolder, JIRA_HOST_FILE), `${normalized}\n`, 'utf8');
  return { key, taskFolder, jiraHost: normalized, status: current ? 'updated' : 'created' };
}

async function createLocalTaskFolder({ project_key: projectKey, title, statement, request_id: requestId }) {
  if (!PROJECT_KEY.test(projectKey || '') || typeof title !== 'string' || !title.trim() ||
      typeof statement !== 'string' || !statement.trim() || typeof requestId !== 'string' ||
      !/^[A-Za-z0-9_-]{8,128}$/.test(requestId)) {
    throw new TaskFolderError('TASK_PATH_INVALID', 'project_key, title, statement and request_id are required');
  }
  const tasksFolder = await getTasksFolder();
  return withManifestLock(tasksFolder, async () => {
    const receiptPath = path.join(getProtectedRoot(), 'requests', `${requestId}.json`);
    if (await isFile(receiptPath)) {
      const receipt = JSON.parse(await fs.readFile(receiptPath, 'utf8'));
      if (receipt.projectKey !== projectKey || receipt.title !== title || receipt.statement !== statement) {
        throw new TaskFolderError('REQUEST_ID_CONFLICT', 'request_id was used with different arguments');
      }
      return receipt.result;
    }
    const manifest = await readManifest(tasksFolder);
    const project = manifest.projects.find((item) => item.project_key === projectKey);
    if (!project) throw new TaskFolderError('PROJECT_NOT_FOUND', `Project ${projectKey} is not registered`);
    if (project.source_type !== 'NO_JIRA') throw new TaskFolderError('PROJECT_SOURCE_CONFLICT', `Project ${projectKey} is not NO_JIRA`);

    let number = project.next_issue_number;
    const receipts = path.dirname(receiptPath);
    for (const name of await fs.readdir(receipts).catch((error) => error.code === 'ENOENT' ? [] : Promise.reject(error))) {
      if (!name.endsWith('.json')) continue;
      const old = JSON.parse(await fs.readFile(path.join(receipts, name), 'utf8'));
      if (old.projectKey === projectKey) number = Math.max(number, Number(old.result.key.split('-').at(-1)) + 1);
    }
    const key = `${projectKey}-${number}`;
    project.next_issue_number = number + 1;
    const year = getMoscowYear();
    const taskFolder = path.join(tasksFolder, year, key);
    await ensureProtectedTask(key);
    const result = { tasksFolder, taskFolder, year, key, projectKey, sourceType: 'NO_JIRA',
      sourceReference: path.join(taskFolder, 'input', 'task.md'), status: 'reserved' };
    await fs.mkdir(path.dirname(receiptPath), { recursive: true });
    await fs.writeFile(receiptPath, JSON.stringify({ projectKey, title, statement, result }), { flag: 'wx' });
    await writeManifest(tasksFolder, manifest);
    return result;
  });
}

async function resolveTaskFolder(key) {
  const { projectKey } = parseIssueKey(key);
  const tasksFolder = await getTasksFolder();
  const manifest = await readManifest(tasksFolder);
  const project = manifest.projects.find((item) => item.project_key === projectKey);
  if (!project) throw new TaskFolderError('PROJECT_NOT_FOUND', `Project ${projectKey} is not registered`);
  const matches = await findTaskFolders(tasksFolder, key);
  if (matches.length > 1) throw new TaskFolderError('LOCAL_TASK_DUPLICATE', `Task ${key} exists in multiple years`);

  if (project.source_type === 'NO_JIRA') {
    if (!matches.length) throw new TaskFolderError('LOCAL_TASK_NOT_FOUND', `Local task ${key} was not found`);
    const { year, taskFolder } = matches[0];
    const sourceReference = path.join(taskFolder, 'input', 'task.md');
    if (!await isFile(sourceReference)) throw new TaskFolderError('LOCAL_TASK_INPUT_MISSING', `${sourceReference} is missing`);
    return { tasksFolder, taskFolder, year, key, projectKey, sourceType: 'NO_JIRA', sourceReference,
      hasTrainRun: await isFile(path.join(taskFolder, 'ai_actions', 'train-run.yaml')) };
  }

  const match = matches[0];
  return { tasksFolder, taskFolder: match?.taskFolder, year: match?.year, key, projectKey,
    sourceType: project.source_type, jiraHost: project.jira_host,
    sourceReference: `${project.jira_host}/browse/${encodeURIComponent(key)}`,
    hasTrainRun: match ? await isFile(path.join(match.taskFolder, 'ai_actions', 'train-run.yaml')) : false };
}

module.exports = {
  TaskFolderConfigError, TaskFolderError, createLocalTaskFolder, createTaskFolder, getConfigPath,
  getTaskProjects, getTasksFolder, normalizeJiraHost, parseIssueKey, registerTaskProject,
  resolveTaskFolder, setTaskJiraHost, validateManifest
};
