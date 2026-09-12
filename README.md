# teille-sync

`teille-sync` moves a corpus of documents through conversion, five at a
time. Each batch claims cards from a GitHub Projects v2 board, fetches
the matching archive from a NAS share reached over VPN, runs the batch
through the `teille-douce` converter in one invocation, judges what came
back, publishes the successes (TEI and any entity CSVs) back to the
share, and writes the verdict onto the card it started from. Two
machines pointed at the same board can run at once: a claim is an
optimistic write followed by a read-back, and whichever machine loses a
race skips the card instead of converting it twice.

`teille-sync run` does one batch by default. `--until-done` keeps
running batches until the board has nothing left `À traiter`.

See `docs/superpowers/specs/` for the design.

## Requirements

- Python 3.12+, and `teille-douce` (the converter) installed and on
  `PATH`.
- A `GITHUB_TOKEN` environment variable with access to the project
  board (a classic personal access token needs the `project` scope; a
  fine-grained token needs read/write access to Projects). It is never
  printed or logged — a refusal from the board names what to fix, never
  the token itself.
- PyHellen and VieuxParler reachable, and `teille-douce`'s NER
  dependencies installed. See *Annotation phases are mandatory* below —
  this project does not treat any of the three as optional.

## Configuration

Every setting resolves through four layers, in this order: a
command-line flag, a `TDSYNC_*` environment variable, a key in
`config.local.toml`, then a built-in default. A bad value from the
environment or the config file produces a refusal that says what the
run uses instead; the same bad value typed as a flag is a usage error,
on the theory that a person who typed it wants to know they got it
wrong.

Copy `config.example.toml` to `config.local.toml` — the latter is
gitignored, so the real address never lands in version control — and
fill in real values. The two settings every install needs first:

| Setting | Environment variable | Example (fictional) |
|---|---|---|
| Local mount point for the share | `TDSYNC_NAS_ROOT` | `/mnt/example` |
| NAS host, for the VPN reachability check | `TDSYNC_NAS_HOST` | `nas.example.invalid` |

`config.example.toml` documents the rest: `batch_size` (default 5),
`reclaim_after` (default `6h` — how long a card can sit `En cours`
before a later run takes the claim back), `project_url`
(`https://github.com/orgs/<org>/projects/<n>`), `ids_file` (default
`project-board-ids.json`), `work_dir`, and the three catalogue paths
`metadata_csv`, `persons_csv` and `entities_dir`. Those three are passed
to `teille-douce` explicitly on every invocation rather than left for
its own config discovery, which walks up the parent directories from
wherever it runs and could otherwise pick up someone else's files
without a word.

## Mounting the share

`nas_root` must be a local path with `OCR/zip_reconciliate` underneath
it — `teille-sync check` verifies exactly this and refuses rather than
guess that a wrong root is the right one.

- **Linux**: mount the share with your VPN client or `mount.cifs`
  wherever suits you, and point `nas_root` there.
- **macOS**: `/Volumes/<share-name>`, once mounted from Finder or
  `mount_smbfs`.
- **Windows**: a mapped drive letter (e.g. `Y:`) works as-is.
- **WSL, with the share mapped from Windows — the one that costs an
  afternoon**: mapping a drive to `Y:` in Windows does not make it
  appear at `/mnt/y` in WSL. It has to be mounted there separately:

  ```bash
  sudo mount -t drvfs Y: /mnt/y
  ```

  `teille-sync check` recognizes this exact situation — an `/mnt/<letter>`
  root that does not exist, on a kernel reporting `microsoft` in
  `/proc/version` — and prints this command as the fix, rather than a
  generic "check your mount point."

## Commands

```bash
teille-sync run [--batches N | --until-done] [--batch-size N]
                [--republish] [--keep] [--reclaim-after 6h]
                [--dry-run] [-v | -q] [--plain]
teille-sync check          # preflight only: eight checks, writes nothing
teille-sync status         # counts per statut, read from the live board
teille-sync release DOC…   # hand one or more stuck cards back to À traiter
teille-sync ids refresh    # rebuild project-board-ids.json from the live board
```

- **`run`** claims up to `--batch-size` (default 5) cards `À traiter`,
  converts them in a single `teille-douce` invocation, judges each
  result, publishes what succeeded, and writes the verdict back to the
  board. `--batches N` runs exactly N batches (default 1); `--until-done`
  keeps going until nothing is left to claim. `--dry-run` prints which
  documents would be claimed without claiming any of them.
  `--republish` overwrites a document already on the share; `--keep`
  never deletes a local copy, converted or not. `--reclaim-after`
  overrides how long a claim can sit `En cours` before this run takes it
  back. `-v` always prints the preflight table; `-q` prints only the
  closing summary, not the per-batch table.
- **`check`** runs the same eight checks `run` runs before touching
  anything (VPN, NAS root, Destination, Board, Converter, Services,
  Metadata, Disk), and writes nothing — safe to run at any time,
  including straight against the real board.
- **`status`** prints how many cards currently sit in each statut.
- **`release`** is the recovery command — see the next section.
- **`ids refresh`** re-reads the live board (its fields, their
  single-select options, and every card's id) and rewrites
  `project-board-ids.json`. Run it after renaming or recreating any
  field or option in the GitHub UI: that mints a new id, and every
  lookup in this tool is by name against whatever
  `project-board-ids.json` last recorded — a stale file means a write
  that raises rather than lands nowhere.

## When a batch dies halfway

This is the paragraph worth finding at 18:00, with five cards stuck on
`En cours` and a `teille-douce` process that just fell over. Claiming a
card and writing its verdict are two separate calls to the board; a
crash, a killed process, or a service dying mid-batch between the two
leaves a card claimed but never judged. Left alone it self-heals after
`reclaim_after` (default 6h), once a later run reclaims it automatically
— but there is no reason to wait that long:

```bash
teille-sync release LIV0044 LIV0045
teille-sync run
```

`release` puts the named cards straight back to `À traiter` and clears
their claim stamp. It refuses — releasing none of them — if even one of
the identifiers given is not on the board at all, rather than release
part of the list. The next `run` picks a released card up exactly like
any other pending document; nothing on the card marks it as having
failed once already.

## Annotation phases are mandatory

Every `run` invokes the converter with `--phases all --require-services`.
Enrichment (PyHellen), modernization (VieuxParler) and named-entity
recognition all run on every document, or none of them do:
`--require-services` makes the converter refuse before writing anything
if a phase's service is unreachable, rather than quietly produce a
document that claims more annotation than it actually got. In practice
that means PyHellen and VieuxParler both have to be running and
reachable, and `teille-douce`'s NER dependencies have to be installed,
before this tool will claim a single card. `teille-sync check` runs the
converter's own `check --strict`, which probes all three, and reports
exactly what it finds — a failing Services check there is a reason not
to run, not a warning to note and proceed past anyway.
