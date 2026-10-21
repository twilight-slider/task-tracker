"""Pinned Train Driver adaptations for the isolated Linux helper only."""

from hashlib import sha256
from pathlib import Path

STORE_SHA = "583ce27c813db9c635eb557f7a4099deda9afa6a791b0bc158117d2104de9a68"
MCP_SHA = "5c5976e48e5682ede01a3fd08036f19e6757d5593f181586c05bfd3f8553375b"


def replace_one(source, old, new):
    if source.count(old) != 1:
        raise ValueError("Pinned Train Driver anchor changed")
    return source.replace(old, new, 1)


def adapt(context, root):
    plugin = Path(context) / "agent_bundle/marketplaces/ai-marketplace/plugins/train-driver"
    store = plugin / "lib/train-run-store.js"
    mcp = plugin / "scripts/train-driver-mcp.js"
    for target, expected in ((store, STORE_SHA), (mcp, MCP_SHA)):
        if sha256(target.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Pinned Train Driver bytes changed: {target.name}")
    source = store.read_text(encoding="utf-8").replace("\r\n", "\n")
    source = replace_one(source,
        "const { samePath, trackerSnapshot } = require('./tracker-snapshot-client');",
        "const { samePath, trackerSnapshot } = require('./tracker-snapshot-client');\n"
        "const { toRuntime, toDisk, assertBoundKey } = require('./train-path-view');")
    source = replace_one(source,
        "const run = validate(YAML.parse(text, { maxAliasCount: 100 }), path.dirname(path.dirname(file)));",
        "const run = assertBoundKey(validate(toRuntime(YAML.parse(text, { maxAliasCount: 100 })), path.dirname(path.dirname(file))));")
    source = replace_one(source, "YAML.stringify(run)", "YAML.stringify(toDisk(run))")
    source = replace_one(source,
        "const incoming = validate({ ...trainRun }, taskFolder);",
        "const incoming = assertBoundKey(validate(toRuntime(trainRun), taskFolder));")
    source = replace_one(source,
        "  const summary = await trackerSnapshot(taskFolder, snapshotKey(run), 'get_result_snapshot',\n"
        "    { snapshot_id: snapshotId, summary_only: true });",
        "  const summary = toRuntime(await trackerSnapshot(taskFolder, snapshotKey(run), 'get_result_snapshot',\n"
        "    { snapshot_id: snapshotId, summary_only: true }));")
    source = replace_one(source,
        "async function switchTrainRun(taskFolder, expectedVersion, selection) {",
        "async function switchTrainRun(taskFolder, expectedVersion, selection) {\n  selection = toRuntime(selection);")
    source = replace_one(source,
        "async function manualRestart(taskFolder, expectedVersion, request) {",
        "async function manualRestart(taskFolder, expectedVersion, request) {\n"
        "  request = { ...request, case: toRuntime(request?.case) };")
    source = replace_one(source,
        "const case_sha256 = createHash('sha256').update(JSON.stringify(request.case)).digest('hex');",
        "const case_sha256 = createHash('sha256').update(JSON.stringify(toDisk(request.case))).digest('hex');")
    store.write_text(source, encoding="utf-8")

    source = mcp.read_text(encoding="utf-8").replace("\r\n", "\n")
    old = """  const file = path.join(homeDir, '.env', 'env.txt');
  const requiredLine = 'GIT_FOLDER=<absolute path to Git repositories>';
  let lines;
  try { lines = (await fs.readFile(file, 'utf8')).split(/\\r?\\n/); }
  catch (error) {
    if (error.code === 'ENOENT') throw new TrainRunError('CATALOG_CONFIG_REQUIRED', 'Local catalog configuration is missing', { file, required_line: requiredLine });
    throw error;
  }
  const values = lines.filter((line) => /^\\s*GIT_FOLDER\\s*=/.test(line))
    .map((line) => line.slice(line.indexOf('=') + 1).trim());
  if (values.length !== 1 || !values[0]) {
    throw new TrainRunError('CATALOG_CONFIG_REQUIRED', 'Set one non-empty GIT_FOLDER', { file, required_line: requiredLine });
  }
  if (!path.isAbsolute(values[0]) || /[\\r\\n]/.test(values[0])) {
    throw new TrainRunError('CATALOG_UNAVAILABLE', 'GIT_FOLDER must be absolute', { file });
  }
  let repository;
  try {
    repository = await fs.realpath(path.join(values[0], 'ai-rails', 'ai-rails-wagons'));"""
    new = """  const helperCatalog = process.env.TT_TRAIN_CATALOG;
  let repository;
  let file;
  if (helperCatalog !== undefined) {
    if (helperCatalog !== '/opt/train-catalog') throw new TrainRunError('CATALOG_UNAVAILABLE', 'Invalid helper catalog');
    file = helperCatalog;
  } else {
    if (process.platform !== 'win32') throw new TrainRunError('CATALOG_CONFIG_REQUIRED', 'Helper catalog is required');
    file = path.join(homeDir, '.env', 'env.txt');
    const requiredLine = 'GIT_FOLDER=<absolute path to Git repositories>';
    let lines;
    try { lines = (await fs.readFile(file, 'utf8')).split(/\\r?\\n/); }
    catch (error) {
      if (error.code === 'ENOENT') throw new TrainRunError('CATALOG_CONFIG_REQUIRED', 'Local catalog configuration is missing', { file, required_line: requiredLine });
      throw error;
    }
    const values = lines.filter((line) => /^\\s*GIT_FOLDER\\s*=/.test(line))
      .map((line) => line.slice(line.indexOf('=') + 1).trim());
    if (values.length !== 1 || !values[0]) throw new TrainRunError('CATALOG_CONFIG_REQUIRED', 'Set one non-empty GIT_FOLDER', { file, required_line: requiredLine });
    if (!path.isAbsolute(values[0]) || /[\\r\\n]/.test(values[0])) throw new TrainRunError('CATALOG_UNAVAILABLE', 'GIT_FOLDER must be absolute', { file });
    file = path.join(values[0], 'ai-rails', 'ai-rails-wagons');
  }
  try {
    repository = await fs.realpath(file);"""
    source = replace_one(source, old, new)
    source = replace_one(source,
        "if (path.basename(repository) !== 'ai-rails-wagons') throw new Error('repository name');",
        "if (helperCatalog === undefined && path.basename(repository) !== 'ai-rails-wagons') throw new Error('repository name');")
    mcp.write_text(source, encoding="utf-8")
    view = plugin / "lib/train-path-view.js"
    view.write_bytes((Path(root) / "agent_bundle/train-path-view.js").read_bytes())
    return (store, mcp, view)
