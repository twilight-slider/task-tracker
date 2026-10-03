'use strict';

const fs = require('node:fs/promises');
const path = require('node:path');
const { execFile } = require('node:child_process');
const { createHash, randomUUID } = require('node:crypto');
const { promisify } = require('node:util');

const execute = promisify(execFile);
const SNAPSHOT_ID = /^rs_[0-9a-f-]{36}$/;
const ALGORITHM_VERSION = 'result-snapshot-v2';

class ResultSnapshotError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = 'ResultSnapshotError';
    this.code = code;
    this.details = details;
  }
}

function fail(code, message, details, cause) {
  const error = new ResultSnapshotError(code, message, details);
  if (cause) error.cause = cause;
  return error;
}

function requireAbsolute(value, label) {
  if (typeof value !== 'string' || !path.isAbsolute(value) || /[\r\n]/.test(value)) {
    throw fail(label === 'project_root' ? 'PROJECT_ROOT_INVALID' : 'SCOPE_INVALID', `${label} must be one absolute path`);
  }
  return path.resolve(value);
}

async function canonicalProjectRoot(projectRoot) {
  const requested = requireAbsolute(projectRoot, 'project_root');
  let root;
  let stat;
  try {
    root = await fs.realpath(requested);
    stat = await fs.lstat(requested);
  } catch (error) {
    if (error.code === 'ENOENT') throw fail('PROJECT_NOT_FOUND', 'project_root does not exist', { project_root: requested });
    throw fail('PROJECT_ROOT_INVALID', 'project_root cannot be resolved', { project_root: requested }, error);
  }
  if (!stat.isDirectory() || stat.isSymbolicLink()) {
    throw fail('PROJECT_ROOT_INVALID', 'project_root must be a real directory', { project_root: requested });
  }
  return root;
}

async function runGit(cwd, args, allowFailure = false) {
  try {
    return await execute('git', args, { cwd, windowsHide: true, encoding: 'buffer', maxBuffer: 64 * 1024 * 1024 });
  } catch (error) {
    if (allowFailure) return null;
    throw fail('INTERNAL_ERROR', 'Git command failed', { command: ['git', ...args], exit_code: error.code }, error);
  }
}

function zeroSeparated(buffer) {
  return buffer.toString('utf8').split('\0').filter(Boolean).map((item) => item.replaceAll('\\', '/'));
}

function relativePath(root, file) {
  const relative = path.relative(root, file).replaceAll('\\', '/');
  if (!relative || relative === '..' || relative.startsWith('../') || path.isAbsolute(relative)) {
    throw fail('SCOPE_INVALID', 'File escapes project_root', { file });
  }
  return relative;
}

async function findGitContext(projectRoot) {
  const result = await runGit(projectRoot, ['rev-parse', '--show-toplevel'], true);
  if (!result) return null;
  const repositoryRoot = await fs.realpath(result.stdout.toString('utf8').trim());
  try {
    relativePath(repositoryRoot, projectRoot);
  } catch (error) {
    if (path.normalize(repositoryRoot) !== path.normalize(projectRoot)) throw error;
  }
  const scope = path.relative(repositoryRoot, projectRoot).replaceAll('\\', '/') || '.';
  return { repositoryRoot, scope };
}

async function walk(root, directory = root, files = []) {
  let entries;
  try {
    entries = await fs.readdir(directory, { withFileTypes: true });
  } catch (error) {
    throw fail('FILE_UNREADABLE', 'Directory cannot be read', { path: relativePath(root, directory) }, error);
  }
  entries.sort((left, right) => left.name.localeCompare(right.name, 'en'));
  for (const entry of entries) {
    const file = path.join(directory, entry.name);
    if (entry.isSymbolicLink()) throw fail('UNSUPPORTED_FILE_TYPE', 'Symbolic links and junctions are not supported', { path: relativePath(root, file) });
    if (entry.isDirectory()) await walk(root, file, files);
    else if (entry.isFile()) files.push(file);
    else throw fail('UNSUPPORTED_FILE_TYPE', 'Only regular files are supported', { path: relativePath(root, file) });
  }
  return files;
}

async function symlinks(root, context, tracked, directory = root, found = []) {
  const entries = await fs.readdir(directory, { withFileTypes: true });
  for (const entry of entries) {
    if (directory === root && entry.name === '.git') continue;
    if (!entry.isSymbolicLink() && !entry.isDirectory()) continue;
    const file = path.join(directory, entry.name);
    const repositoryRelative = relativePath(context.repositoryRoot, file);
    const listed = zeroSeparated((await runGit(context.repositoryRoot, [
      'ls-files', '-z', '--others', '--directory', '--exclude-per-directory=.gitignore', '--', repositoryRelative
    ])).stdout);
    const included = tracked.some((name) => name === repositoryRelative || name.startsWith(`${repositoryRelative}/`)) || listed.length > 0;
    if (!included) continue;
    const stat = await fs.lstat(file);
    if (stat.isSymbolicLink()) found.push(file);
    else if (stat.isDirectory()) await symlinks(root, context, tracked, file, found);
  }
  return found;
}

async function assertStableIgnoreRules(repositoryRoot) {
  const trackedResult = await runGit(repositoryRoot, ['ls-files', '-z', '--cached', '--', '*.gitignore']);
  const tracked = new Set(zeroSeparated(trackedResult.stdout));
  const untracked = zeroSeparated((await runGit(repositoryRoot, [
    'ls-files', '-z', '--others', '--exclude-per-directory=.gitignore', '--', '*.gitignore'
  ])).stdout);
  if (untracked.length) {
    throw fail('UNSTABLE_IGNORE_RULES', 'Untracked repository .gitignore files make scope unstable', { files: untracked.sort() });
  }

  const head = await runGit(repositoryRoot, ['rev-parse', '--verify', 'HEAD'], true);
  if (!head) {
    if (tracked.size) throw fail('UNSTABLE_IGNORE_RULES', 'Repository ignore rules must be committed', { files: [...tracked].sort() });
    return;
  }
  const changed = zeroSeparated((await runGit(repositoryRoot, ['diff', '--name-only', '-z', 'HEAD', '--', '*.gitignore'])).stdout);
  if (changed.length) throw fail('UNSTABLE_IGNORE_RULES', 'Modified repository .gitignore files make scope unstable', { files: changed.sort() });
}

async function gitFiles(projectRoot, context) {
  await assertStableIgnoreRules(context.repositoryRoot);
  const suffix = context.scope === '.' ? [] : ['--', context.scope];
  const tracked = zeroSeparated((await runGit(context.repositoryRoot, ['ls-files', '-z', '--cached', ...suffix])).stdout);
  const untracked = zeroSeparated((await runGit(context.repositoryRoot, ['ls-files', '-z', '--others', '--exclude-per-directory=.gitignore', ...suffix])).stdout);
  for (const file of await symlinks(projectRoot, context, tracked)) {
    throw fail('UNSUPPORTED_FILE_TYPE', 'Symbolic links are not supported', { path: relativePath(projectRoot, file) });
  }
  const files = [];
  for (const repositoryRelative of new Set([...tracked, ...untracked])) {
    const file = path.join(context.repositoryRoot, ...repositoryRelative.split('/'));
    let stat;
    try { stat = await fs.lstat(file, { bigint: true }); }
    catch (error) {
      if (error.code === 'ENOENT') continue;
      throw fail('FILE_UNREADABLE', 'Path cannot be inspected', { path: repositoryRelative }, error);
    }
    if (stat.isSymbolicLink()) throw fail('UNSUPPORTED_FILE_TYPE', 'Symbolic links are not supported', { path: relativePath(projectRoot, file) });
    if (!stat.isFile()) throw fail('UNSUPPORTED_FILE_TYPE', 'Only regular files are supported', { path: relativePath(projectRoot, file) });
    files.push(file);
  }
  return files;
}

async function hashFile(projectRoot, file) {
  let before;
  let after;
  const relative = relativePath(projectRoot, file);
  const raw = createHash('sha256');
  const normalized = createHash('sha256');
  const decoder = new TextDecoder('utf-8', { fatal: true });
  let binary = false;
  let pendingCR = false;
  let pendingLF = false;
  function updateText(text) {
    if (pendingCR) {
      text = `\n${text.startsWith('\n') ? text.slice(1) : text}`;
      pendingCR = false;
    }
    if (text.endsWith('\r')) {
      pendingCR = true;
      text = text.slice(0, -1);
    }
    text = text.replace(/\r\n?/g, '\n');
    if (text && pendingLF) normalized.update('\n');
    if (text) pendingLF = false;
    if (text.endsWith('\n')) {
      pendingLF = true;
      text = text.slice(0, -1);
    }
    if (text) normalized.update(text);
  }
  try {
    before = await fs.lstat(file, { bigint: true });
    if (!before.isFile()) throw fail('UNSUPPORTED_FILE_TYPE', 'Only regular files are supported', { path: relative });
    const handle = await fs.open(file, 'r');
    try {
      const buffer = Buffer.allocUnsafe(256 * 1024);
      for (;;) {
        const { bytesRead } = await handle.read(buffer, 0, buffer.length, null);
        if (!bytesRead) break;
        const chunk = buffer.subarray(0, bytesRead);
        raw.update(chunk);
        if (!binary) {
          if (chunk.includes(0)) binary = true;
          else {
            try { updateText(decoder.decode(chunk, { stream: true })); }
            catch { binary = true; }
          }
        }
      }
    } finally {
      await handle.close();
    }
    after = await fs.lstat(file, { bigint: true });
  } catch (error) {
    if (error instanceof ResultSnapshotError) throw error;
    if (error.code === 'ENOENT') throw fail('CONCURRENT_CHANGE', 'A file changed while the snapshot was read', { path: relative }, error);
    throw fail('FILE_UNREADABLE', 'File cannot be read', { path: relative }, error);
  }
  if (!after.isFile()) throw fail('UNSUPPORTED_FILE_TYPE', 'Only regular files are supported', { path: relative });
  for (const key of ['dev', 'ino', 'size', 'mtimeNs', 'ctimeNs']) {
    if (before[key] !== after[key]) throw fail('CONCURRENT_CHANGE', 'A file changed while the snapshot was read', { path: relative });
  }
  if (!binary) {
    try { updateText(decoder.decode()); }
    catch { binary = true; }
  }
  if (!binary && pendingCR) {
    pendingCR = false;
    updateText('\n');
  }
  return { path: relative, kind: binary ? 'binary' : 'text', sha256: (binary ? raw : normalized).digest('hex'), metadata: after };
}

async function currentSnapshot(projectRoot) {
  const root = await canonicalProjectRoot(projectRoot);
  const gitContext = await findGitContext(root);
  const mode = gitContext ? 'git' : 'filesystem';
  const list = () => mode === 'git' ? gitFiles(root, gitContext) : walk(root);
  const paths = await list();
  const hashed = [];
  // ponytail: 32 concurrent streams bound memory; tune only if measured throughput changes.
  for (let index = 0; index < paths.length; index += 32) {
    hashed.push(...await Promise.all(paths.slice(index, index + 32).map((file) => hashFile(root, file))));
  }
  const finalPaths = await list();
  const sorted = (items) => items.map((file) => relativePath(root, file)).sort();
  if (JSON.stringify(sorted(paths)) !== JSON.stringify(sorted(finalPaths))) {
    throw fail('CONCURRENT_CHANGE', 'Project file set changed while the snapshot was calculated');
  }
  await Promise.all(paths.map(async (file, index) => {
    let stat;
    try { stat = await fs.lstat(file, { bigint: true }); }
    catch (error) { throw fail('CONCURRENT_CHANGE', 'A file changed while the snapshot was calculated', { path: relativePath(root, file) }, error); }
    if (!stat.isFile() || ['dev', 'ino', 'size', 'mtimeNs', 'ctimeNs'].some((key) => stat[key] !== hashed[index].metadata[key])) {
      throw fail('CONCURRENT_CHANGE', 'A file changed while the snapshot was calculated', { path: relativePath(root, file) });
    }
  }));
  const files = hashed.map(({ metadata, ...record }) => record);
  files.sort((left, right) => Buffer.compare(Buffer.from(left.path), Buffer.from(right.path)));
  const canonical = JSON.stringify([ALGORITHM_VERSION, files.map(({ path: file, kind, sha256 }) => [file, kind, sha256])]);
  return {
    algorithm_version: ALGORITHM_VERSION,
    fingerprint: { algorithm: 'sha256', value: createHash('sha256').update(canonical).digest('hex') },
    project: { root, mode },
    files_count: files.length,
    files
  };
}

function snapshotDirectory(taskFolder) {
  return path.join(requireAbsolute(taskFolder, 'task_folder'), '.protected', 'snapshots');
}

function snapshotPath(taskFolder, snapshotId) {
  if (typeof snapshotId !== 'string' || !SNAPSHOT_ID.test(snapshotId)) {
    throw fail('SNAPSHOT_NOT_FOUND', 'Snapshot does not exist', { snapshot_id: snapshotId });
  }
  return path.join(snapshotDirectory(taskFolder), `${snapshotId}.json`);
}

async function createSnapshot(taskFolder, projectRoot) {
  const current = await currentSnapshot(projectRoot);
  const directory = snapshotDirectory(taskFolder);
  const directoryStat = await fs.lstat(directory);
  if (!directoryStat.isDirectory() || directoryStat.isSymbolicLink()) {
    throw fail('SCOPE_INVALID', 'Snapshot directory must be a plain directory');
  }
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const snapshot = {
      schema_version: 2,
      snapshot_id: `rs_${randomUUID()}`,
      ...current,
      created_at: new Date().toISOString()
    };
    try {
      await fs.writeFile(path.join(directory, `${snapshot.snapshot_id}.json`), `${JSON.stringify(snapshot, null, 2)}\n`, { encoding: 'utf8', flag: 'wx' });
      return { status: 'ok', snapshot };
    } catch (error) {
      if (error.code !== 'EEXIST') throw fail('INTERNAL_ERROR', 'Snapshot cannot be stored', {}, error);
    }
  }
  throw fail('INTERNAL_ERROR', 'A unique snapshot id could not be allocated');
}

async function getSnapshot(taskFolder, snapshotId) {
  const file = snapshotPath(taskFolder, snapshotId);
  try {
    const stat = await fs.lstat(file);
    if (!stat.isFile() || stat.isSymbolicLink()) throw new Error('invalid snapshot file');
    const snapshot = JSON.parse(await fs.readFile(file, 'utf8'));
    if (!snapshot || snapshot.snapshot_id !== snapshotId || snapshot.schema_version !== 2 ||
        snapshot.algorithm_version !== ALGORITHM_VERSION ||
        typeof snapshot.project?.root !== 'string' || !path.isAbsolute(snapshot.project.root) ||
        !['git', 'filesystem'].includes(snapshot.project.mode) ||
        !Array.isArray(snapshot.files) || snapshot.files_count !== snapshot.files.length ||
        snapshot.fingerprint?.algorithm !== 'sha256' || !/^[0-9a-f]{64}$/.test(snapshot.fingerprint.value) ||
        snapshot.files.some((entry) => typeof entry.path !== 'string' || !entry.path ||
          entry.path.startsWith('/') || entry.path.split('/').includes('..') ||
          !['text', 'binary'].includes(entry.kind) || !/^[0-9a-f]{64}$/.test(entry.sha256))) {
      throw new Error('invalid snapshot document');
    }
    const files = [...snapshot.files].sort((left, right) => Buffer.compare(Buffer.from(left.path), Buffer.from(right.path)));
    if (files.some((entry, index) => entry !== snapshot.files[index] ||
        (index > 0 && files[index - 1].path === entry.path))) throw new Error('invalid snapshot order');
    const canonical = JSON.stringify([ALGORITHM_VERSION, files.map(({ path: name, kind, sha256 }) => [name, kind, sha256])]);
    if (createHash('sha256').update(canonical).digest('hex') !== snapshot.fingerprint.value) {
      throw new Error('invalid snapshot fingerprint');
    }
    return snapshot;
  } catch (error) {
    if (error instanceof ResultSnapshotError) throw error;
    if (error.code === 'ENOENT') throw fail('SNAPSHOT_NOT_FOUND', 'Snapshot does not exist', { snapshot_id: snapshotId });
    throw fail('INTERNAL_ERROR', 'Snapshot cannot be read', { snapshot_id: snapshotId }, error);
  }
}

function changes(reviewed, current) {
  const before = new Map(reviewed.files.map((file) => [file.path, file.sha256]));
  const after = new Map(current.files.map((file) => [file.path, file.sha256]));
  return {
    added: [...after.keys()].filter((file) => !before.has(file)).sort(),
    modified: [...after.keys()].filter((file) => before.has(file) && before.get(file) !== after.get(file)).sort(),
    deleted: [...before.keys()].filter((file) => !after.has(file)).sort()
  };
}

async function compareSnapshot(taskFolder, snapshotId, validateRoot = async (root) => root) {
  const reviewed = await getSnapshot(taskFolder, snapshotId);
  if (reviewed.schema_version !== 2 || reviewed.algorithm_version !== ALGORITHM_VERSION) {
    throw fail('SNAPSHOT_VERSION_UNSUPPORTED', 'Snapshot uses an older hash algorithm; create and validate a new snapshot', { snapshot_id: snapshotId });
  }
  const current = await currentSnapshot(await validateRoot(reviewed.project.root));
  if (current.fingerprint.value === reviewed.fingerprint.value) {
    return { status: 'current', snapshot_id: snapshotId, fingerprint: current.fingerprint };
  }
  return {
    status: 'stale',
    snapshot_id: snapshotId,
    reviewed_fingerprint: reviewed.fingerprint,
    current_fingerprint: current.fingerprint,
    changes: changes(reviewed, current)
  };
}

module.exports = {
  ResultSnapshotError,
  compareSnapshot,
  createSnapshot,
  getSnapshot
};
