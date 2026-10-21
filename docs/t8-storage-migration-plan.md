# T8 storage migration plan (not executed)

Status: design and synthetic proof only, 2026-10-21. No working Tracker was switched and no existing task was copied or modified.

## Observed source

- Current `/mnt/d/Projects/Tracker/tasks` is drvfs (`9p`): 59 task folders, 535 files, 9,549,957 bytes. The scan found no symlink or shared hardlink at that instant. Per-task `.protected` accounts for 63 files / 867,790 bytes; `ai_actions` for 244 files / 6,559,357 bytes. See `.runtime/tests/t8-preinstall/storage-summary.json`.
- AIDEV-79 alone has 30 files / 701,092 bytes. Its `ai_actions/train-run.yaml` SHA-256 is `0bbe31a82dcc3ec03988bc8aeaeaed11bb8049192d68775ca48eb56d08ada7f2`; `projects.json` SHA-256 is `f651bb1a11577f7392820b9b972df0cda720bdd8e608daad758c83bd7696c1a4`. The full per-file baseline is `.runtime/tests/t8-preinstall/source-manifest.json`.
- The global Windows `.protected` holds the Windows service, runtime, token and rollback records. It must not be copied into the new WSL protected root. WSL creates its own protected storage and SQLite.

## Recommended copy-only path

1. Stop both writers before a final baseline. Record a fresh per-file SHA-256 manifest for **all** tasks and `projects.json`; reject symlinks, nonregular files, hardlinks and any source change during hashing. Keep the D: original unchanged and available for rollback.
2. Copy the whole tasks tree and `projects.json` into a new private ext4 staging root, with 0700 directories and 0600 files. Verify both the original and copy against the same manifest before making either authoritative. The current 59-folder inventory is small enough for a bounded copy, but timing must be measured at cutover.
3. Use `tasksRoot=<ext4 trackerRoot>/tasks` and `protectedRoot=<ext4 trackerRoot>/.protected`; create fresh WSL protected data. Check that all 59 task folders resolve and that prior per-task snapshots and TrainRun bytes remain unchanged. A fixed `agentTaskUpdate` can serve only its matching bound task; general multi-task agent starts remain a separate capability gap.
4. Extend the trusted Train Driver path map for a selected task with an exact Windows alias `D:\Projects\Tracker\tasks\<year>\<key>` alongside its ext4 disk root. The existing path-view adapter then maps persisted Windows paths to `/root/task` before validation and back to the original Windows spelling before persistence. Reject `..`, foreign task keys and paths outside approved mounts. The synthetic round-trip and rejection contract passed in `tests/train_migration_alias_contract.js`; DockerController wiring and a real copied TrainRun read/save test are still required. Do not rewrite the on-disk TrainRun during migration.
5. Only after T8.7 live gates and a reviewed final configuration, install the WSL Tracker from the checked image and retain its rollback record plus a consistent SQLite backup. Do not run the Windows and WSL Trackers as concurrent writers.

## Rollback and conflict handling

- Stop WSL agents and Tracker first. Verify D: still matches the frozen source manifest. If any file differs, abort reverse sync and preserve both trees for review. Build a candidate tree on D: from ext4, verify its hashes, verify the D: baseline again, rename original to a retained backup, then rename candidate into place. Verify the retained backup still matches baseline and the new D: tree matches ext4. Never overwrite a changed original automatically.
- This sequence requires all external writers to remain stopped throughout final verification and rename; a new host write after the last check is otherwise a race. Keep the original backup until data, snapshots and TrainRun are reviewed. Restore the pre-cutover Windows service only after choosing which tree is authoritative.
- `tests/wsl_t8_migration_dry_run.py` proved on **synthetic** drvfs and ext4 trees that copy hashes match, a changed D: original blocks reverse sync, and a clean reverse sync preserves the old tree as a backup. Evidence: `.runtime/tests/t8-migration-dry-run/result.json`. It did not touch real tasks.

## Alternative: split task storage

Keeping `tasksRoot` on drvfs while `trackerRoot/.protected` and sessions stay on ext4 requires changes to both `FolderStore.config` and `wsl_install.check_layout`, plus independent task and protected mounts. Per-task `.protected` snapshots and `ai_actions` would remain on drvfs unless their storage is redesigned. An external host can create a hardlink into an agent-writable drvfs mount after the pre-start scan; the synthetic T8.7 probe showed that the running agent can read such an alias. This alternative needs a new protected per-task storage boundary and wider tests before it is safe. The copy-only path is the smaller change, subject to final migration and rollback review.
