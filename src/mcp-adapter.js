'use strict';

const net = require('node:net');
const readline = require('node:readline');
const fs = require('node:fs/promises');
const path = require('node:path');

const pipe = `\\\\.\\pipe\\${process.env.TASK_FOLDER_MCP_PIPE || 'task-folder-mcp-v1'}`;
const tools = [
  { name: 'get_tasks_folder', inputSchema: { type: 'object', properties: {}, additionalProperties: false } },
  { name: 'get_task_projects', inputSchema: { type: 'object', properties: {}, additionalProperties: false } },
  { name: 'create_task_folder', inputSchema: { type: 'object', properties: { key: { type: 'string' } }, required: ['key'], additionalProperties: false } },
  { name: 'set_task_jira_host', inputSchema: { type: 'object', properties: {
    key: { type: 'string' }, jira_host: { type: 'string' }
  }, required: ['key', 'jira_host'], additionalProperties: false } },
  { name: 'resolve_task_folder', inputSchema: { type: 'object', properties: { key: { type: 'string' } }, required: ['key'], additionalProperties: false } },
  { name: 'create_local_task_folder', inputSchema: { type: 'object', properties: {
    project_key: { type: 'string' }, title: { type: 'string' }, statement: { type: 'string' }, request_id: { type: 'string' }
  }, required: ['project_key', 'title', 'statement', 'request_id'], additionalProperties: false } }
];
const allowed = new Set(tools.map((tool) => tool.name));

function callOnce(method, args) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection(pipe);
    let response = '';
    socket.setTimeout(30000);
    socket.on('connect', () => socket.write(`${JSON.stringify({ method, arguments: args })}\n`));
    socket.on('data', (chunk) => {
      response += chunk;
      if (response.length > 1024 * 1024) { socket.destroy(); reject(new Error('Service response too large')); }
      if (response.includes('\n')) { socket.end(); try { resolve(JSON.parse(response.split('\n')[0])); } catch (error) { reject(error); } }
    });
    socket.on('timeout', () => socket.destroy(new Error('Service timeout')));
    socket.on('error', reject);
    socket.on('end', () => { if (!response.includes('\n')) reject(new Error('Service closed without response')); });
  });
}

async function callService(method, args) {
  const deadline = Date.now() + 30000;
  for (;;) {
    try { return await callOnce(method, args); }
    catch (error) {
      if (!['ENOENT', 'EBUSY'].includes(error.code) || Date.now() >= deadline) throw error;
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
  }
}

function toolResult(data) {
  const result = data.ok ? data.data : { code: data.code, message: data.message };
  return { content: [{ type: 'text', text: JSON.stringify(result) }], structuredContent: result,
    ...(!data.ok ? { isError: true } : {}) };
}

async function plainDirectory(dir) {
  try {
    const item = await fs.lstat(dir);
    if (!item.isDirectory() || item.isSymbolicLink()) throw new Error(`Invalid task directory: ${dir}`);
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
    await fs.mkdir(dir);
  }
}

async function makeOrdinaryFolder(prepared, names) {
  await plainDirectory(prepared.tasksFolder);
  await plainDirectory(path.join(prepared.tasksFolder, prepared.year));
  await plainDirectory(prepared.taskFolder);
  for (const name of names) await plainDirectory(path.join(prepared.taskFolder, name));
}

function jiraOrigin(value) {
  if (typeof value !== 'string' || /[\r\n]/.test(value)) throw new Error('jira_host must be one HTTPS origin');
  const url = new URL(value);
  if (url.protocol !== 'https:' || url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
    throw new Error('jira_host must be one HTTPS origin');
  }
  return url.origin;
}

async function storedJiraHost(taskFolder) {
  try { return jiraOrigin((await fs.readFile(path.join(taskFolder, '.jira-host'), 'utf8')).trim()); }
  catch (error) { if (error.code === 'ENOENT') return undefined; throw error; }
}

async function isFile(file) {
  try { return (await fs.stat(file)).isFile(); }
  catch (error) { if (error.code === 'ENOENT') return false; throw error; }
}

async function callTool(method, args) {
  if (method === 'create_task_folder') {
    const result = await callService(method, args);
    if (!result.ok) return result;
    const names = ['origin', 'update', 'ai_actions', 'retro'];
    const existed = await Promise.all([result.data.taskFolder, ...names.map((name) => path.join(result.data.taskFolder, name))]
      .map(async (dir) => { try { return (await fs.stat(dir)).isDirectory(); } catch (error) {
        if (error.code === 'ENOENT') return false; throw error;
      } }));
    await makeOrdinaryFolder(result.data, names);
    return { ok: true, data: { ...result.data,
      status: existed.every(Boolean) ? 'already_exists' : 'created' } };
  }
  if (method === 'set_task_jira_host') {
    let host;
    try { host = jiraOrigin(args.jira_host); }
    catch { return { ok: false, code: 'JIRA_HOST_INVALID' }; }
    const result = await callTool('create_task_folder', { key: args.key });
    if (!result.ok) return result;
    if (host !== result.data.jiraHost) return { ok: false, code: 'JIRA_HOST_CONFLICT' };
    const taskFolder = result.data.taskFolder;
    const current = await storedJiraHost(taskFolder);
    if (current === host) return { ok: true, data: { key: args.key, taskFolder, jiraHost: host, status: 'already_set' } };
    await fs.writeFile(path.join(taskFolder, '.jira-host'), `${host}\n`, 'utf8');
    return { ok: true, data: { key: args.key, taskFolder, jiraHost: host, status: current ? 'updated' : 'created' } };
  }
  if (method === 'create_local_task_folder') {
    const result = await callService(method, args);
    if (!result.ok) return result;
    const task = result.data;
    await makeOrdinaryFolder(task, ['input', path.join('input', 'materials'), 'update', 'ai_actions', 'retro']);
    const body = `# ${args.title.trim()}\n\n${args.statement.trim()}\n`;
    try {
      await fs.writeFile(task.sourceReference, body, { flag: 'wx' });
    } catch (error) {
      if (error.code !== 'EEXIST' || await fs.readFile(task.sourceReference, 'utf8') !== body) throw error;
    }
    return { ok: true, data: { ...task, status: 'created' } };
  }
  if (method === 'resolve_task_folder') {
    const key = args.key;
    if (typeof key !== 'string' || !/^([A-Z][A-Z0-9_-]*)-([1-9][0-9]*)$/.test(key)) return { ok: false, code: 'TASK_PATH_INVALID' };
    const projects = await callService('get_task_projects', {});
    if (!projects.ok) return projects;
    const project = projects.data.manifest?.projects.find((p) => key.startsWith(`${p.project_key}-`));
    if (!project) return { ok: false, code: 'PROJECT_NOT_FOUND' };
    const root = projects.data.tasksFolder;
    const years = await fs.readdir(root, { withFileTypes: true });
    const matches = [];
    for (const year of years.filter((entry) => entry.isDirectory() && /^\d{4}$/.test(entry.name))) {
      const taskFolder = path.join(root, year.name, key);
      try { if ((await fs.stat(taskFolder)).isDirectory()) matches.push({ year: year.name, taskFolder }); }
      catch (error) { if (error.code !== 'ENOENT') throw error; }
    }
    if (matches.length > 1) return { ok: false, code: 'LOCAL_TASK_DUPLICATE' };
    const match = matches[0];
    if (project.source_type === 'NO_JIRA' && !match) return { ok: false, code: 'LOCAL_TASK_NOT_FOUND' };
    const sourceReference = project.source_type === 'NO_JIRA'
      ? path.join(match.taskFolder, 'input', 'task.md') : `${project.jira_host}/browse/${encodeURIComponent(key)}`;
    if (project.source_type === 'NO_JIRA') {
      try { await fs.access(sourceReference); } catch { return { ok: false, code: 'LOCAL_TASK_INPUT_MISSING' }; }
    }
    return { ok: true, data: { tasksFolder: root, taskFolder: match?.taskFolder, year: match?.year,
      key, projectKey: project.project_key, sourceType: project.source_type,
      jiraHost: project.jira_host, sourceReference,
      hasTrainRun: match ? await isFile(path.join(match.taskFolder, 'ai_actions', 'train-run.yaml')) : false } };
  }
  if (method === 'get_tasks_folder') {
    const result = await callService(method, args);
    return result.ok ? { ok: true, data: { tasksFolder: result.data } } : result;
  }
  return callService(method, args);
}

async function handle(request) {
  if (request.method === 'initialize') return { protocolVersion: request.params?.protocolVersion || '2024-11-05', capabilities: { tools: {} }, serverInfo: { name: 'task-folder-mcp', version: '0.1.0' } };
  if (request.method === 'tools/list') return { tools };
  if (request.method !== 'tools/call') return undefined;
  const method = request.params?.name;
  if (!allowed.has(method)) return toolResult({ ok: false, code: 'UNKNOWN_TOOL' });
  try { return toolResult(await callTool(method, request.params?.arguments || {})); }
  catch (error) { return toolResult({ ok: false, code: 'SERVICE_UNAVAILABLE', message: error.message }); }
}

readline.createInterface({ input: process.stdin, crlfDelay: Infinity }).on('line', async (line) => {
  let request;
  try {
    request = JSON.parse(line);
    const result = await handle(request);
    if (request.id !== undefined && result !== undefined) process.stdout.write(`${JSON.stringify({ jsonrpc: '2.0', id: request.id, result })}\n`);
  } catch (error) {
    if (request?.id !== undefined) process.stdout.write(`${JSON.stringify({ jsonrpc: '2.0', id: request.id, error: { code: -32603, message: error.message } })}\n`);
  }
});
