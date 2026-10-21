"""Fail-closed Linux transport adapters for three pinned user plugin files.

Only exported copies are changed. The installed Windows cache is read-only.
"""

from hashlib import sha256
from pathlib import Path


EXPECTED = {
    "task-folder-workflow/scripts/task-folder-mcp.js": "ee8a104308b2e3daee9e4c41d871453d66d69b9e204fb8ff7bc876ba42bdbaf2",
    "task-tracker-mcp/scripts/task-tracker-mcp.js": "0949c6eb3429c7a7c16fbdb01e939262b62c3bb5311cfdf437d761e480f08983",
    "train-driver/lib/tracker-snapshot-client.js": "1920dfbef2ff6f203fdee324dbef1d502582117c3cf12ba8ef00f456bdba2d55",
}


def replace_one(source, old, new):
    if source.count(old) != 1:
        raise ValueError("Pinned plugin adapter anchor changed")
    return source.replace(old, new, 1)


def forwarder(source):
    old = """const envPath = path.join(os.homedir(), '.env', 'env.txt');
if (!fs.statSync(envPath).isFile()) throw new Error(`User env.txt is not a file: ${envPath}`);
const lines = fs.readFileSync(envPath, 'utf8').replace(/^\\uFEFF/, '').split(/\\r?\\n/);
const ports = lines.filter(line => /^\\s*TRACKER_MCP_PORT\\s*=/i.test(line));
if (ports.length !== 1) throw new Error(`Expected one TRACKER_MCP_PORT in ${envPath}`);
const port = ports[0].slice(ports[0].indexOf('=') + 1).trim();
if (!/^[1-9][0-9]{0,4}$/.test(port) || Number(port) > 65535) throw new Error(`Invalid TRACKER_MCP_PORT in ${envPath}`);
const url = new URL(`http://127.0.0.1:${port}/mcp/${scope}`);
const matches = lines
  .filter(line => /^\\s*TRACKER_MCP_TOKEN\\s*=/i.test(line));
if (matches.length !== 1) throw new Error(`Expected one TRACKER_MCP_TOKEN in ${envPath}`);
const token = matches[0].slice(matches[0].indexOf('=') + 1).trim();
if (!token || /[\\r\\n]/.test(token)) throw new Error(`Invalid TRACKER_MCP_TOKEN in ${envPath}`);"""
    new = """let url;
let token;
const runtimeBase = process.env.TRACKER_MCP_BASE_URL;
if (runtimeBase !== undefined) {
  const match = /^http:\\/\\/tracker-mcp:([1-9][0-9]{3,4})$/.exec(runtimeBase);
  if (!match || Number(match[1]) > 65535) throw new Error('Invalid scoped Tracker URL');
  url = new URL(`${runtimeBase}/mcp/${scope}`);
  token = process.env.TRACKER_MCP_TOKEN;
  if (!/^[A-Za-z0-9_-]{32,256}$/.test(token || '')) throw new Error('Invalid scoped Tracker token');
} else {
  if (process.platform !== 'win32') throw new Error('Scoped Tracker URL is required');
  const envPath = path.join(os.homedir(), '.env', 'env.txt');
  if (!fs.statSync(envPath).isFile()) throw new Error(`User env.txt is not a file: ${envPath}`);
  const lines = fs.readFileSync(envPath, 'utf8').replace(/^\\uFEFF/, '').split(/\\r?\\n/);
  const ports = lines.filter(line => /^\\s*TRACKER_MCP_PORT\\s*=/i.test(line));
  if (ports.length !== 1) throw new Error(`Expected one TRACKER_MCP_PORT in ${envPath}`);
  const port = ports[0].slice(ports[0].indexOf('=') + 1).trim();
  if (!/^[1-9][0-9]{0,4}$/.test(port) || Number(port) > 65535) throw new Error(`Invalid TRACKER_MCP_PORT in ${envPath}`);
  url = new URL(`http://127.0.0.1:${port}/mcp/${scope}`);
  const matches = lines.filter(line => /^\\s*TRACKER_MCP_TOKEN\\s*=/i.test(line));
  if (matches.length !== 1) throw new Error(`Expected one TRACKER_MCP_TOKEN in ${envPath}`);
  token = matches[0].slice(matches[0].indexOf('=') + 1).trim();
  if (!token || /[\\r\\n]/.test(token)) throw new Error(`Invalid TRACKER_MCP_TOKEN in ${envPath}`);
}"""
    source = replace_one(source, old, new)
    return replace_one(source, "const response = await fetch(url, { method: 'POST', headers: {\n      'Content-Type': 'application/json', Accept: 'application/json', Authorization: `Bearer ${token}`",
                       "const response = await fetch(url, { method: 'POST', redirect: 'error', headers: {\n      'Content-Type': 'application/json', Accept: 'application/json', Origin: url.origin, Authorization: `Bearer ${token}`")


def train_snapshot(source):
    source = replace_one(source,
        "const path = require('node:path');",
        "const path = require('node:path');\nconst { toRuntime } = require('./train-path-view');")
    source = replace_one(source,
        "const ENDPOINT = 'http://127.0.0.1:38772/mcp/snapshots';",
        """const ENDPOINT = 'http://127.0.0.1:38772/mcp/snapshots';
function scopedBase() {
  const raw = process.env.TRACKER_MCP_BASE_URL;
  if (raw === undefined) {
    if (process.platform !== 'win32') fail('TRACKER_UNAVAILABLE', 'Scoped Tracker URL is required');
    return null;
  }
  const match = /^http:\\/\\/tracker-mcp:([1-9][0-9]{3,4})$/.exec(raw);
  if (!match || Number(match[1]) > 65535) fail('TRACKER_UNAVAILABLE', 'Scoped Tracker URL is invalid');
  return new URL(raw);
}""")
    source = replace_one(source,
        "  const envFile = path.join(homeDir, '.env', 'env.txt');",
        """  const base = scopedBase();
  if (base) {
    const token = process.env.TRACKER_MCP_TOKEN;
    if (!/^[A-Za-z0-9_-]{32,256}$/.test(token || '') || key !== process.env.TRACKER_TASK_KEY) {
      fail('TRACKER_SCOPE_INVALID', 'Scoped Tracker task or token is invalid');
    }
    let folder;
    try {
      const response = await fetch(new URL('/mcp/folders', base), {
        method: 'POST', redirect: 'error', signal: AbortSignal.timeout(35000),
        headers: { 'Content-Type': 'application/json', Origin: base.origin,
          Authorization: `Bearer ${token}` },
        body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/call',
          params: { name: 'resolve_task_folder', arguments: { key } } })
      });
      if (!response.ok) fail('TRACKER_UNAVAILABLE', `Tracker HTTP ${response.status}`);
      const data = await response.json();
      folder = data?.result?.structuredContent?.taskFolder;
    } catch (error) {
      if (error instanceof TrackerSnapshotError) throw error;
      fail('TRACKER_UNAVAILABLE', 'Scoped task lookup failed');
    }
    if (toRuntime({ task_folder: folder }).task_folder !== taskFolder) fail('TRACKER_SCOPE_INVALID', 'Task folder does not match assigned key');
    return { token, endpoint: new URL('/mcp/snapshots', base).href, origin: base.origin };
  }
  const envFile = path.join(homeDir, '.env', 'env.txt');""")
    source = replace_one(source, "  return { token };", "  return { token, endpoint: ENDPOINT, origin: new URL(ENDPOINT).origin };")
    source = replace_one(source, "  const { token } = await trackerContext(taskFolder, key, homeDir);",
                         "  const { token, endpoint, origin } = await trackerContext(taskFolder, key, homeDir);")
    source = replace_one(source,
        "const response = await fetch(ENDPOINT, { method: 'POST', signal: AbortSignal.timeout(35000),\n      headers: { 'Content-Type': 'application/json', Accept: 'application/json', Authorization: `Bearer ${token}` },",
        "const response = await fetch(endpoint, { method: 'POST', redirect: 'error', signal: AbortSignal.timeout(35000),\n      headers: { 'Content-Type': 'application/json', Accept: 'application/json', Origin: origin, Authorization: `Bearer ${token}` },")
    return source


def adapt(context):
    base = Path(context) / "agent_bundle" / "marketplaces" / "ai-marketplace" / "plugins"
    for relative, expected in EXPECTED.items():
        target = base / relative
        original = target.read_bytes()
        if sha256(original).hexdigest() != expected:
            raise ValueError(f"Pinned plugin bytes changed: {relative}")
        source = original.decode("utf-8").replace("\r\n", "\n")
        adapted = train_snapshot(source) if relative.startswith("train-driver/") else forwarder(source)
        target.write_text(adapted, encoding="utf-8")
