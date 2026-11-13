---
name: codex-context-provider
description: Repair local Codex history after provider switching, including modelProviderMismatch and missing-provider errors. Preview and migrate provider metadata while preserving thread IDs and conversation bodies, with backups and offline recovery.
---

# Codex Context Provider

Use this skill when a user wants existing local Codex chats to remain accessible after switching model providers. The bundled tool aligns `threads.model_provider` with the first `session_meta.payload.model_provider` in each indexed rollout.

## Inspect and choose the scope

Resolve the user's actual `CODEX_HOME`, launch profile, and effective provider ID. Read only the relevant configuration fields. Configuration may contain credentials, so keep full configuration contents out of tool output and published artifacts.

Use the current effective provider as the default target. Honor a user-requested stable provider ID. CLI and profile overrides require explicit `--target`. `openai` was used in the original repair, but the target depends on the user's current setup.

Run the bundled script using Python 3.11+ from this skill's directory. Use `py -3.14` on Windows when available, or the environment's Python interpreter. Python 3.14 supports Zstandard rollouts directly. Earlier Python versions need `zstandard` for `.jsonl.zst` files.

```sh
python scripts/provider_history.py --all
python scripts/provider_history.py --thread THREAD_ID --target PROVIDER_ID
```

Both commands preview changes. `--all` includes archived indexed threads. `--home PATH` selects another Codex home. Multiple discovered state databases require an explicit `--db PATH`. A stale `.jsonl` index path falls back to the matching `.jsonl.zst` file when it exists; applying the migration corrects the index path to the `.zst` file, and recovery restores the original path. A rollout missing in both forms still needs separate path repair before this migration can proceed.

## Apply an authorized migration offline

Apply after all Codex desktop, CLI, and app-server processes writing to this home have stopped. The lock excludes other copies of this script. Codex itself does not honor that lock. The script also refuses to apply while it detects a running Codex writer process, and there is no flag to bypass that check; close the listed writers and retry. When writer detection is unavailable on the system, `--offline` remains the confirmation that writers have stopped. When operating from the affected Codex session, prepare the preview and terminal command, then hand execution to the user after they close Codex. Use an independent runtime only when the affected writers are stopped.

For an authorized repair, use the chosen scope and target with the write flags.

```sh
python scripts/provider_history.py --all --target PROVIDER_ID --apply --offline
```

The script backs up the committed SQLite database and every affected rollout under the local Codex home. It records a manifest before replacing files, preserves the conversation body bytes and header length, and updates provider columns inside a database transaction. If a new provider ID cannot fit the original header, choose a shorter ID compatible with the intended configuration.

Follow the output's backup path for rollback or crash recovery.

```sh
python scripts/provider_history.py --restore /path/to/manifest.json --apply --offline
```

Recovery preflights recorded file hashes and database providers. A crash during restoration can be recovered by rerunning the same command. A rollout with newer edits requires individual inspection before recovery to preserve those edits. Recovery also restores any index path the migration corrected. Backups contain private conversations and stay local.

## Verify and report

Repeat the preview after applying and confirm the chosen scope reports zero changes. Reopen Codex and load an affected chat. Report metadata migration and chat-loading verification separately. Sending a test prompt uses the configured provider and needs authorization if the user only requested metadata repair.

For repeated switching, preserve one provider ID across endpoint configurations where the user's Codex build supports that arrangement. This script leaves endpoint and authentication settings to the user's provider configuration. Switching the ID again requires another migration. Read [README.md](README.md) for installation, limitations, and the stable-ID configuration example.
