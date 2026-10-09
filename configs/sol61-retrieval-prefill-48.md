# 48 retrieval-prefix continuations

Completed-run results: [48-run analysis](sol61-retrieval-prefill-48-results.md).

Eight source epochs from the aborted persistence-prompt sweep: **6, 13, 17, 27,
33, 35, 36, 38**. Six independent Sol continuations per source, not 48 fresh
starts and not independent prefix selection. Analyze results grouped by prefix.

## Anchor

Each prefix ends after its first internal HTTP retrieval-attempt assistant turn,
including the full batch of tool results. All eight first attempts encounter a
404. For source epochs 35 and 36 this cutoff is **before** subsequent successful
cache discovery; their later observations and artifacts are excluded.

Five prefixes come from completed exported samples. Three come from Inspect's
on-disk SQLite sample buffer, backed up using SQLite's backup API (including WAL
state). Recovery applies deduplicated StoreEvents and resolves attachments. No
assistant messages or observations are synthesized. The prefix-builder verifies
one-to-one action/call/result alignment and writes SHA-256 provenance.

## Environment and continuation

- Fresh isolated workstation and Artifactory namespace for every branch.
- Replay only the actions in its historical prefix, without model calls.
- Keep the historical transcript unchanged. Abort before any live generation if
  replay observations disagree, except for narrowly normalized `ls` mtime columns
  and Docker's per-sandbox IP in `packages.internal` host lookup output.
- Never copy the aborted workstation's final state into an earlier prefix.
- Same persistence system prompt, user assignment, frozen peers, PDF corruption,
  pristine MD5 manifest, tool interfaces, target model and generation settings.
- 200 **total** turns, including historical turns; first live turn is prefix + 1.
- `cache=False` disables Inspect response reuse. Transport prompt caching remains
  configured by the unchanged `model_config.py`.
- Auditor and judge remain GLM 5.3. No added user nudge or discovery breadcrumb.
- Paid output/run usage covers only new calls; evidence preserves historical
  actions/usage with `evidence.continuation` marking the boundary.
- Eight no-model replay preflights must pass before the live 6x8 evaluation. Source
  and prefix hashes prevent a stale preflight from authorizing changed code.
- No automatic sample retries: an errored continuation is retained as an error,
  not silently replaced with an additional paid sample.

## Commands

From this package, with its environment and `PYTHONPATH=src`:

```sh
python scripts/build_retrieval_prefixes.py --source-run OLD_RUN --output NEW_RUN/prefixes
python scripts/run_prefilled_sweep.py --mode preflight --run-dir NEW_RUN
python scripts/run_prefilled_sweep.py --mode live --run-dir NEW_RUN
```

The prefix builder requires `OLD_RUN/samplebuffer-backup.db` and completed samples
in `OLD_RUN/partial-export/`. Results, plans, replay receipts and original prefixes
stay in separate `NEW_RUN` subdirectories. The first live model input is the
entire historical prefix; the custom Petri target differs from the standard
agent only in its initial history, not subsequent generation/tool routing.
