'use strict';

// Runs only in the pinned, root-owned Train Driver helper. The Tracker supplies
// these roots at Docker Exec creation; the Codex process cannot alter them.
const path = require('node:path');
const FIELD_NAMES = new Set(['path', 'from_path', 'task_folder', 'train_repository',
  'repository', 'project_root', 'root']);
const RUNTIME_ROOT = /^(?:\/root\/task|\/workspace\/task\/update|\/opt\/train-catalog|\/workspace\/repos\/[A-Za-z0-9][A-Za-z0-9_.-]{0,63})$/;

function mappings() {
  let rows;
  try { rows = JSON.parse(process.env.TT_PATH_MAP_JSON || ''); }
  catch { throw new Error('Trusted Train Driver path map is missing'); }
  if (!Array.isArray(rows) || !rows.length || rows.length > 66) throw new Error('Invalid Train Driver path map');
  for (const row of rows) {
    if (!row || typeof row.disk !== 'string' || !path.posix.isAbsolute(row.disk) ||
        path.posix.normalize(row.disk) !== row.disk || row.disk.includes('/../') ||
        typeof row.runtime !== 'string' || !RUNTIME_ROOT.test(row.runtime) ||
        (row.win !== null && row.win !== undefined &&
         (!/^[A-Za-z]:\\/.test(row.win) || path.win32.normalize(row.win) !== row.win))) {
      throw new Error('Invalid Train Driver path map');
    }
  }
  if (new Set(rows.map(row => row.runtime)).size !== rows.length) throw new Error('Duplicate Train Driver mount');
  return rows.sort((a, b) => b.runtime.length - a.runtime.length);
}

function within(value, root, separator, folded = false) {
  const v = folded ? value.toLowerCase() : value;
  const r = folded ? root.toLowerCase() : root;
  return v === r || v.startsWith(r + separator);
}

function translate(value, direction, rows) {
  if (typeof value !== 'string') return value;
  if (/^[A-Za-z]:\\/.test(value)) {
    if (path.win32.normalize(value) !== value || value.split('\\').some(part => part === '.' || part === '..'))
      throw new Error('Noncanonical Train Driver path');
  } else if (path.posix.isAbsolute(value)) {
    if (path.posix.normalize(value) !== value || value.split('/').some(part => part === '.' || part === '..'))
      throw new Error('Noncanonical Train Driver path');
  }
  if (direction === 'runtime') {
    for (const row of rows) {
      if (row.win && within(value, row.win, '\\', true))
        return row.runtime + value.slice(row.win.length).replaceAll('\\', '/');
      if (within(value, row.disk, '/')) return row.runtime + value.slice(row.disk.length);
      if (within(value, row.runtime, '/')) return value;
    }
    if (/^[A-Za-z]:\\/.test(value) || path.posix.isAbsolute(value))
      throw new Error('Train Driver path is outside assigned mounts');
  } else {
    for (const row of rows) {
      if (within(value, row.runtime, '/')) {
        const suffix = value.slice(row.runtime.length);
        return row.win ? row.win + suffix.replaceAll('/', '\\') : row.disk + suffix;
      }
    }
    if (path.posix.isAbsolute(value) || /^[A-Za-z]:\\/.test(value))
      throw new Error('Train Driver path is outside assigned mounts');
  }
  return value;
}

function transform(value, direction, rows = mappings()) {
  if (Array.isArray(value)) return value.map(item => transform(item, direction, rows));
  if (!value || typeof value !== 'object') return value;
  const output = {};
  for (const [key, item] of Object.entries(value)) {
    output[key] = FIELD_NAMES.has(key) && typeof item === 'string'
      ? translate(item, direction, rows)
      : (item && typeof item === 'object' ? transform(item, direction, rows) : item);
  }
  return output;
}

function assertBoundKey(run) {
  const expected = process.env.TRACKER_TASK_KEY;
  if (!expected) return run;
  const keys = [run?.input?.key, run?.input?.task?.key, run?.case?.task?.key].filter(Boolean);
  if (!keys.length || keys.some(key => key !== expected)) throw new Error('Foreign TrainRun task key');
  return run;
}

module.exports = { toRuntime: value => transform(value, 'runtime'),
  toDisk: value => transform(value, 'disk'), assertBoundKey };
