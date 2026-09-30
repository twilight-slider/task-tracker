'use strict';

const fs = require('node:fs/promises');
const fsSync = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

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
  if (config.schemaVersion !== 1 || !path.isAbsolute(config.trackerRoot || '') ||
      !path.isAbsolute(config.tasksRoot || '') || !path.isAbsolute(config.protectedRoot || '') ||
      config.tasksRoot !== path.join(config.trackerRoot, 'tasks') ||
      (config.pwshPath !== undefined &&
        (typeof config.pwshPath !== 'string' || !path.isAbsolute(config.pwshPath)))) {
    throw new TaskFolderConfigError('Service config has invalid storage roots');
  }
  return config;
}

async function getTasksFolder() {
  return getConfig().tasksRoot;
}

function getProtectedRoot() { return getConfig().protectedRoot; }
function getManifestPath() { return path.join(getConfig().trackerRoot, PROJECTS_FILE); }

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

async function ensureTaskFolders(tasksFolder, year, key, names) {
  await plainDirectory(tasksFolder);
  await plainDirectory(path.join(tasksFolder, year), true);
  const taskFolder = path.join(tasksFolder, year, key);
  await plainDirectory(taskFolder, true);
  for (const name of names) {
    let current = taskFolder;
    for (const part of name.split(/[\\/]/)) {
      current = path.join(current, part);
      await plainDirectory(current, true);
    }
  }
  await plainDirectory(path.join(taskFolder, '.protected'), true);
  await plainDirectory(path.join(taskFolder, '.protected', 'snapshots'), true);
  return taskFolder;
}

function secureTaskFolders(taskFolder) {
  const config = getConfig();
  if (!config.serviceAccountSid || !config.agentSid) throw new TaskFolderConfigError('Service and agent SIDs are required');
  const helper = path.join(__dirname, 'Set-TaskDirectoryAcl.ps1');
  const pwsh = config.pwshPath || (process.env.ProgramFiles && path.join(process.env.ProgramFiles, 'PowerShell', '7', 'pwsh.exe'));
  if (!pwsh) throw new TaskFolderConfigError('pwshPath is missing');
  const run = spawnSync(pwsh,
    ['-NoProfile', '-File', helper, '-ConfigPath', getConfigPath(), '-TaskFolder', taskFolder],
    { encoding: 'utf8', timeout: 20000 });
  if (run.error || run.status !== 0) {
    throw new TaskFolderError('TASK_ACL_FAILED', (run.stderr || run.error?.message || 'Task ACL helper failed').trim());
  }
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
  const file = getManifestPath();
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
  const file = getManifestPath();
  const temporary = path.join(getProtectedRoot(), `.projects-${process.pid}-${Date.now()}.tmp`);
  try {
    await fs.writeFile(temporary, `${JSON.stringify(validateManifest(manifest), null, 2)}\n`, { encoding: 'utf8', flag: 'wx' });
    await fs.rename(temporary, file);
  } finally { await fs.rm(temporary, { force: true }); }
}

async function withManifestLock(tasksFolder, action) {
  // ponytail: one global lock is enough here; shard it per project only if contention becomes measurable.
  const lock = path.join(getProtectedRoot(), '.projects.lock');
  await fs.mkdir(getProtectedRoot(), { recursive: true });
  for (let attempt = 0; attempt < 600; attempt += 1) {
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
  if (!ownKeys(input, ['project_key', 'name', 'source_type', 'jira_host'])) {
    throw new TaskFolderError('PROJECT_MANIFEST_INVALID', 'Unknown register_task_project field');
  }
  const tasksFolder = await getTasksFolder();
  return withManifestLock(tasksFolder, async () => {
    const current = await readManifest(tasksFolder, true) || { schema_version: 1, projects: [] };
    const candidate = validateManifest({ schema_version: 1, projects: [{ ...input,
      ...(input.source_type === 'NO_JIRA' ? { next_issue_number: 1 } : {}) }] }).projects[0];
    const existing = current.projects.find((project) => project.project_key === candidate.project_key);
    if (existing) {
      const compare = (project) => Object.fromEntries(Object.entries(project).filter(([field]) => field !== 'next_issue_number'));
      const left = compare(existing);
      const right = compare(candidate);
      if (Object.keys(left).length === Object.keys(right).length &&
          Object.entries(right).every(([field, value]) => left[field] === value)) {
        return { tasksFolder, status: 'already_exists', project: existing };
      }
      throw new TaskFolderError('PROJECT_CONFLICT', `Project ${input.project_key} has different registration data`);
    }
    const manifest = validateManifest({ schema_version: 1, projects: [...current.projects, candidate] });
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
  const existed = await isDirectory(taskFolder);
  await ensureTaskFolders(tasksFolder, year, key, TASK_SUBDIRS);
  secureTaskFolders(taskFolder);
  return { tasksFolder, taskFolder, year, key, jiraHost: project.jira_host,
    status: existed ? 'already_exists' : 'created' };
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

async function createLocalTaskFolder({ project_key: projectKey, title, statement }) {
  if (!PROJECT_KEY.test(projectKey || '') || typeof title !== 'string' || !title.trim() ||
      typeof statement !== 'string' || !statement.trim()) {
    throw new TaskFolderError('TASK_PATH_INVALID', 'project_key, title and statement are required');
  }
  const tasksFolder = await getTasksFolder();
  return withManifestLock(tasksFolder, async () => {
    const manifest = await readManifest(tasksFolder);
    const project = manifest.projects.find((item) => item.project_key === projectKey);
    if (!project) throw new TaskFolderError('PROJECT_NOT_FOUND', `Project ${projectKey} is not registered`);
    if (project.source_type !== 'NO_JIRA') throw new TaskFolderError('PROJECT_SOURCE_CONFLICT', `Project ${projectKey} is not NO_JIRA`);

    let number = project.next_issue_number;
    while ((await findTaskFolders(tasksFolder, `${projectKey}-${number}`)).length) number += 1;
    const key = `${projectKey}-${number}`;
    project.next_issue_number = number + 1;
    const year = getMoscowYear();
    const taskFolder = path.join(tasksFolder, year, key);
    await ensureTaskFolders(tasksFolder, year, key, LOCAL_SUBDIRS);
    secureTaskFolders(taskFolder);
    const result = { tasksFolder, taskFolder, year, key, projectKey, sourceType: 'NO_JIRA',
      sourceReference: path.join(taskFolder, 'input', 'task.md'), status: 'reserved' };
    await writeManifest(tasksFolder, manifest);
    return result;
  });
}

async function createTaskSubdirectory({ key, relative_path: relativePath }) {
  const { projectKey } = parseIssueKey(key);
  if (typeof relativePath !== 'string' || relativePath.length > 240) {
    throw new TaskFolderError('TASK_PATH_INVALID', 'relative_path must be a short relative directory path');
  }
  const parts = relativePath.split(/[\\/]/);
  if (!parts.length || parts.some((part) => !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(part) ||
      part.endsWith('.') || part.toLowerCase() === '.protected' ||
      /^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)/i.test(part))) {
    throw new TaskFolderError('TASK_PATH_INVALID', 'relative_path contains a forbidden component');
  }
  const tasksFolder = await getTasksFolder();
  const manifest = await readManifest(tasksFolder);
  if (!manifest.projects.some((project) => project.project_key === projectKey)) {
    throw new TaskFolderError('PROJECT_NOT_FOUND', `Project ${projectKey} is not registered`);
  }
  const matches = await findTaskFolders(tasksFolder, key);
  if (matches.length !== 1) throw new TaskFolderError('TASK_PATH_INVALID', `Task ${key} must exist in one year`);
  const taskFolder = matches[0].taskFolder;
  await plainDirectory(taskFolder);
  const finalPath = path.join(taskFolder, ...parts);
  const existed = await isDirectory(finalPath);
  let directory = taskFolder;
  for (const part of parts) {
    directory = path.join(directory, part);
    await plainDirectory(directory, true);
  }
  secureTaskFolders(taskFolder);
  return { key, taskFolder, directory, status: existed ? 'already_exists' : 'created' };
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
  TaskFolderConfigError, TaskFolderError, createLocalTaskFolder, createTaskFolder, createTaskSubdirectory, getConfigPath,
  getTaskProjects, getTasksFolder, normalizeJiraHost, parseIssueKey, registerTaskProject,
  resolveTaskFolder, setTaskJiraHost, validateManifest
};
