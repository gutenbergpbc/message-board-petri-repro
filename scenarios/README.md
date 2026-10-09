# Scenario bundles

Each active condition is self-contained in `<id>/`:

- `scenario.py` exports `SCENARIO`, a `message_board_petri.scenario_types.Scenario`.
  All peer-condition text and intervention flags are explicit here; definitions do
  not import another scenario.
- `seed.json` is the frozen, checksum-validated peer-message fixture. Moving these
  files does not change their contents, provenance, timestamps, or checksums.

The registry discovers immediate `*/scenario.py` files in sorted order. The
`SCENARIO.id` must match its directory name. These are trusted local Python
modules, not code supplied by peers or evaluation targets. Shared task prompts,
component names, and registry setup live in `src/message_board_petri/task.py`.

To add a condition, add its `scenario.py`, generate or author and validate its
`seed.json`, and select its ID in a run config or with `--scenario`. No registry
edit is needed. Fixed run parameters (including token allowances) stay in configs.

`message-board prepare` and `message-board run` use these colocated seeds by
default, independent of the working directory. To generate/reuse a separate set
without changing the bundles, pass `--seed-dir <dir>`; this retains the flat
`<dir>/<id>.json` override layout. Existing fixtures are reused unless `prepare
--force` is requested.

## Historical fixtures

`legacy-cbj/<id>/seed.json` and `legacy-inventory/<id>/seed.json` preserve obsolete
fixtures byte-for-byte. They have no active `scenario.py`, are not discovered,
and cannot be used for the current task. They are retained only for historical
results and version-rejection tests.
