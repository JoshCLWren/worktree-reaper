# worktree-reaper

A pressure-gated cleanup tool for idle Git worktrees and stale Cargo `target/`
directories. It previews by default; nothing is removed without `--apply`.

The policy is evaluated **per filesystem**, so an unpressured disk never pays
the rebuild cost of deleting its build cache.

## Policy

| Filesystem usage | Worktrees | Cargo `target/` dirs |
| --- | --- | --- |
| below `--pressure-threshold` (85%) | reaped after 24h idle | left alone |
| at or above the threshold | reaped after 60 min idle | reaped after 60 min idle on that filesystem |

"Idle" means the newest source file in the worktree is older than the limit.
Build output, dependency and VCS directories are excluded from that scan.

Git lock labels and GitHub ownership claims are **ignored by default** — a
stale claim should not pin an abandoned checkout forever. Pass
`--respect-leases` to restore GitHub-based protection.

## Safety

- Preview is the default action.
- A single-process lock prevents concurrent runs.
- Live processes (cwd, executable, open files, arguments) and in-flight Git
  operations always protect a worktree or target directory.
- Worktrees are backed up before removal: HEAD ref, binary staged/unstaged
  patches, and a verified tarball of untracked files.
- Source state is snapshotted again after backup; any concurrent edit cancels
  the removal.
- The filesystem managed by a separate cache cleaner is never touched.

## Configuration

No machine-specific values are stored in this repository. They are read from
the environment, conventionally from `~/.config/comic-worktree-cleanup/env`:

| Variable | Meaning | Default |
| --- | --- | --- |
| `CWC_REPO` | main Git checkout containing the worktrees | `~/code/my-project` |
| `CWC_TARGET_ROOTS` | colon-separated roots to search for Cargo targets | `~/code:/tmp` |
| `CWC_MANAGED_CACHE` | cache root owned by another cleaner; skipped here | `~/.cache/rust-build-cache` |
| `CWC_MOUNTS` | colon-separated mount points to check for pressure | `/:/tmp` |
| `CWC_REMOTE` | `owner/repo` used by `--respect-leases`; unset disables GitHub calls | empty |
| `CWC_TIMEZONE` | IANA timezone for log timestamps | `UTC` |
| `CWC_STATE` | lock/state directory | `~/.local/state/comic-worktree-cleanup` |
| `CWC_BACKUPS` | worktree backup directory | `~/.local/share/worktree-cleanup-backups` |

See `config.example.env` for a template. Keep the real file mode `600`.

## Usage

```sh
comic-worktree-cleanup                              # preview
comic-worktree-cleanup --apply                      # run the policy
comic-worktree-cleanup --pressure-threshold 95 --apply
comic-worktree-cleanup --max-idle-minutes 30 --apply
comic-worktree-cleanup --gentle-idle-minutes 720 --apply
comic-worktree-cleanup --respect-leases --apply
comic-worktree-cleanup --skip-targets --apply
```

Every run writes one JSON object per event to stdout, suitable for a journal.

## Installation

1. Put `cleanup.py` somewhere stable and point `bin/comic-worktree-cleanup`
   (`CWC_SCRIPT`) at it.
2. Copy `config.example.env` to `~/.config/comic-worktree-cleanup/env` and fill
   in your paths.
3. Install the wrapper in `~/.local/bin/` and the two systemd units in
   `~/.config/systemd/user/`.
4. `systemctl --user daemon-reload && systemctl --user enable --now worktree-reaper.timer`

The service uses an optional `EnvironmentFile`, so the timer works even before
you create the config file (the defaults apply).
