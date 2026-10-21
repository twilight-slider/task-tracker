// Opt-in Train Driver helper launched once by Docker Exec as UID 0.
import fs from 'node:fs';
import net from 'node:net';
import { createHash } from 'node:crypto';
import path from 'node:path';

const socketPath = '/run/td/helper.sock';
const taskFolder = '/root/task';
const maxBytes = 2 * 1024 * 1024;
const allowed = new Set(['resolve_local_catalog', 'get_train_run', 'begin_execution',
  'prepare_current_action', 'save_train_run', 'switch_train_run',
  'manual_unblock_train_run', 'manual_transfer_train_run', 'manual_restart_train_run']);
const pluginRoot = '/opt/agent-bundle/marketplaces/ai-marketplace/plugins/train-driver';
const pluginFile = path.join(pluginRoot, 'scripts/train-driver-mcp.js');
const pinnedTreeSha256 = 'f82c47c4954e6b8bcac8955e425ade4d71007c6f5f481ffa122179d9d073a356';
function treeDigest(root) {
  const digest = createHash('sha256');
  function walk(directory) {
    for (const item of fs.readdirSync(directory, {withFileTypes:true}).sort((a,b) => a.name.localeCompare(b.name))) {
      const full = path.join(directory, item.name);
      if (path.relative(root, full) === 'node_modules/.bin') continue; // CLI links are never loaded by handle().
      if (item.isSymbolicLink()) throw new Error('Linked plugin code is forbidden');
      if (item.isDirectory()) { walk(full); continue; }
      if (!item.isFile()) throw new Error('Non-file plugin entry is forbidden');
      digest.update(path.relative(root, full) + '\0');
      digest.update(createHash('sha256').update(fs.readFileSync(full)).digest('hex') + '\n');
    }
  }
  walk(root);
  return digest.digest('hex');
}
if (process.argv[2] === '--print-plugin-digest') {
  console.log(treeDigest(pluginRoot));
  process.exit(0);
}
if (process.getuid() !== 0 || fs.statSync('/root').uid !== 0 ||
    (fs.statSync('/root').mode & 0o777) !== 0o700 || !fs.statSync(taskFolder).isDirectory()) {
  throw new Error('Private task parent is not root-owned 0700');
}
if (!fs.statSync('/workspace/task/update').isDirectory()) throw new Error('Safe update mount is missing');
if (treeDigest(pluginRoot) !== pinnedTreeSha256) {
  throw new Error('Pinned Train Driver bundle changed');
}
const { handle } = await import(pluginFile);
function checkTaskKey(run) {
  const expected = process.env.TRACKER_TASK_KEY;
  if (!expected) return;
  const keys = [run?.input?.key, run?.input?.task?.key, run?.case?.task?.key].filter(Boolean);
  if (!keys.length || keys.some(key => key !== expected)) throw new Error('Foreign TrainRun task key');
}
let busy = false;
const server = net.createServer(connection => {
  let buffer = Buffer.alloc(0);
  connection.setTimeout(10000, () => connection.destroy());
  connection.on('data', async chunk => {
    buffer = Buffer.concat([buffer, chunk]);
    if (buffer.length > maxBytes) return connection.destroy();
    const newline = buffer.indexOf(10);
    if (newline < 0) return;
    const line = buffer.subarray(0, newline).toString('utf8');
    buffer = Buffer.alloc(0);
    if (busy) return connection.end(JSON.stringify({jsonrpc:'2.0',id:null,error:{code:-32001,message:'busy'}}) + '\n');
    busy = true;
    try {
      const request = JSON.parse(line);
      if (request?.jsonrpc === '2.0' && request.method === 'notifications/initialized' &&
          request.id === undefined) { connection.end(); return; }
      if (!request || request.jsonrpc !== '2.0' ||
          !(Number.isSafeInteger(request.id) || (typeof request.id === 'string' && request.id.length <= 64))) {
        throw new Error('Invalid request ID');
      }
      let result;
      if (request.method === 'initialize') result = await handle(request);
      else if (request.method === 'ping') result = {};
      else if (request.method === 'tools/list') {
        const listed = await handle(request);
        result = {tools:listed.tools.filter(tool => allowed.has(tool.name))};
      } else if (request.method === 'tools/call') {
        if (!allowed.has(request.params?.name) || !request.params.arguments ||
            typeof request.params.arguments !== 'object' || Array.isArray(request.params.arguments)) {
          throw new Error('Unsupported request');
        }
        const args = request.params.arguments;
        if (args.task_folder !== undefined && args.task_folder !== taskFolder) throw new Error('Foreign task folder');
        if (request.params.name === 'save_train_run') {
          if (!args.train_run || typeof args.train_run !== 'object' || Array.isArray(args.train_run))
            throw new Error('Invalid TrainRun input');
          checkTaskKey(args.train_run);
        }
        result = await handle({...request, params:{...request.params,
          arguments:{...args, task_folder:taskFolder}}});
        if (!result?.isError && request.params.name === 'get_train_run') checkTaskKey(result.structuredContent);
      } else {
        throw new Error('Unsupported request');
      }
      const output = JSON.stringify({jsonrpc:'2.0',id:request.id,result}) + '\n';
      if (Buffer.byteLength(output) > maxBytes) throw new Error('Response too large');
      connection.end(output);
    } catch (error) {
      connection.end(JSON.stringify({jsonrpc:'2.0',id:null,error:{code:-32602,message:error.message}}) + '\n');
    } finally { busy = false; }
  });
});
server.maxConnections = 4;
fs.mkdirSync('/run/td', {recursive:true});
server.listen(socketPath, () => {
  // The root-owned 0710 parent admits only GID 1000; CAP_CHOWN is unavailable.
  fs.chmodSync(socketPath, 0o666);
});
