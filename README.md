# teille-sync

`teille-sync` moves the corpus through conversion, five documents at a
time. Each batch:

1. claims cards `À traiter` on the GitHub Projects v2 board,
2. copies the matching archives from the NAS share, over VPN,
3. converts them in a single `teille-douce` call, with all three
   annotation phases,
4. judges each result,
5. publishes what succeeded (TEI and entity CSVs) back to the share,
6. writes the verdict onto the card it started from.

Several machines can work on the same board at once: a card one machine
has claimed is skipped by the others.

**Go straight to:**
[Set up a machine](#set-up-a-machine) ·
[Everyday use](#everyday-use) ·
[A run stopped halfway](#a-run-stopped-halfway) ·
[Retry a document](#retry-a-document) ·
[`check` failed](#check-failed) ·
[Other messages](#other-messages) ·
[Reference](#reference) ·
[Maintenance](#maintenance) ·
[Design notes](#design-notes)

---

## Set up a machine

Once per machine, in this order.

> **Always run `teille-sync` from the `teille-sync/` directory.**
> `config.local.toml`, `project-board-ids.json` and `work/` are all looked
> up relative to the current directory — run it from anywhere else and it
> finds none of them.

### 1. Install teille-sync and the converter

Both go into the same virtual environment: `teille-sync` calls
`teille-douce` by name, so the converter has to be on the `PATH` of the
shell that runs `teille-sync`.

```bash
cd teille-sync
./scripts/install.sh
```

That is the whole of it. The script creates `.venv/` (or reuses a
virtual environment already here), installs the converter and
teille-sync into it, and verifies that both answer before it reports
success. It is safe to run again on a machine that is already set up.

**Developing the converter as well?** Then you want it editable, so that
your edits to it count:

```bash
git clone https://github.com/Grand-Siecle/TEIlle-douce.git ../TEIlle-douce
./scripts/install.sh --editable        # default path: ../TEIlle-douce
```

Add `--dev` to either form for pytest and coverage.

- **The first NER run downloads several GB of models** from Hugging Face.
  Expect the first batch to be slow. The install itself also pulls torch,
  which is large.
- **Which converter you get.** Without `--editable`, the converter comes
  from the tag pinned in the `converter` extra of `pyproject.toml` — so
  that the `Version pipeline` each batch writes to the board names
  something that cannot move underneath it. Moving to a newer converter
  is a deliberate edit of that line, and pip caches git URLs, so an
  existing machine needs `--force-reinstall` to pick the new one up.

<details>
<summary>Doing it by hand</summary>

The script exists because each of these steps has a way of failing that
names something other than its cause. If you do it yourself:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[converter]'      # teille-sync AND the converter
```

- **`python -m pip`, not `pip`.** If `~/.local/bin` precedes the virtual
  environment on your `PATH` — the default on many machines — then `pip`
  is some other Python's pip, and both projects land outside the
  environment you just made. `python -m pip` cannot be shadowed.
- **`[ner]` is not optional.** The `converter` extra asks for it already,
  but installing TEIlle-douce by hand without it gives you the core only
  — no torch, transformers, gliner or flair — and the Services check then
  refuses every run. See
  [Annotation phases are mandatory](#annotation-phases-are-mandatory).
- **No GPU?** Install the CPU build of torch *before* anything that asks
  for `[ner]`, or pip pulls the much larger CUDA build:
  `python -m pip install torch --index-url https://download.pytorch.org/whl/cpu`

</details>

### 2. Start PyHellen and VieuxParler

Both have to answer before a single card is claimed. Start them as their
own READMEs describe. The converter looks for them at:

| Service | Default URL | Override |
|---|---|---|
| PyHellen | `http://localhost:8000` | `TDOUCE_PYHELLEN_URL` |
| VieuxParler | `http://localhost:8011` | `TDOUCE_MODERNIZE_URL` |

Set an override in the shell that runs `teille-sync`: the converter
inherits its environment *and* its working directory, so a
`teille-douce.toml` inside `TEIlle-douce/` is **not** read — only one in
`teille-sync/` or in one of its parent directories would be.

### 3. Connect the VPN and mount the share

`nas_root` must be a local path with `OCR/zip_reconciliate` directly
underneath it.

| System | Where the share is |
|---|---|
| Linux | wherever you mount it (VPN client or `mount.cifs`) |
| macOS | `/Volumes/<share-name>`, once mounted from Finder or `mount_smbfs` |
| Windows | the mapped drive letter, e.g. `Y:` |
| WSL | see below |

**WSL, with the share mapped as `Y:` in Windows — the one that costs an
afternoon:** the drive does *not* appear at `/mnt/y` on its own. Mount it,
and again after every WSL restart:

```bash
sudo mkdir -p /mnt/y && sudo mount -t drvfs Y: /mnt/y
```

`teille-sync check` recognizes this situation (an `/mnt/<letter>` root
that does not exist, on WSL) and prints that exact command as the fix.
Both halves matter: WSL does not create `/mnt/<letter>` for a drive
mapped after boot, and mounting onto a directory that is not there fails
with `mount point does not exist`.

### 4. Write `config.local.toml`

```bash
cp config.example.toml config.local.toml
```

Then fill in at least these keys:

```toml
nas_root     = "/mnt/y"                                     # step 3
nas_host     = "<NAS host name>"                            # dialled by the VPN check
project_url  = "https://github.com/orgs/<org>/projects/<n>" # the board
metadata_csv = "/absolute/path/to/metadata_livre.csv"       # catalogue of volumes
persons_csv  = "/absolute/path/to/metadata_personne.csv"    # catalogue of persons
entities_dir = "entities"                                   # where NER entity CSVs are written
```

- `config.local.toml` is gitignored: the real NAS address never reaches
  the repository.
- Relative paths resolve against the current directory. Prefer absolute
  paths for the two catalogues, and point them at the real corpus
  catalogues — not the test fixtures in `TEIlle-douce/tests/fixtures/`.
- `project_url` must be an **organisation** project (`/orgs/…`). A user
  project (`/users/…`) is refused.
- Every other setting has a working default: see [Settings](#settings).

### 5. Give it a GitHub token

```bash
export GITHUB_TOKEN=<token>
```

A classic personal access token needs the `project` scope; a fine-grained
token needs read/write access to Projects on the organisation. Set it in
every shell that runs `teille-sync`. It is never printed or logged.

Already signed in with the `gh` CLI? Its token usually carries `project`
already, so you can hand it over instead of minting a new one — neither
form below puts the token in your scrollback:

```bash
export GITHUB_TOKEN=$(gh auth token)                                    # gh >= 2.16
export GITHUB_TOKEN=$(awk '/oauth_token:/{print $2}' ~/.config/gh/hosts.yml)   # older gh
```

Check which scopes it has with `gh api -i user | grep -i x-oauth-scopes`.

Mind the difference between the two: a command substitution that fails
still exports, with the failed command's own error text as the value. The
first form does exactly that on `gh` older than 2.16, where `gh auth
token` does not exist. You do not have to check for it — step 6 below
refuses a value that cannot be a token and says why — but you do have to
pick the form that matches your `gh`.

### 6. Create the board id file

```bash
teille-sync ids refresh
```

This creates `project-board-ids.json` in the current directory
(gitignored): the project's id, every field with its options, and every
card's id, read from the live board. You never write or copy this file
by hand. See [Refresh the id file](#refresh-the-id-file) for when to run
it again.

### 7. Check, preview, run

```bash
teille-sync check            # all eight rows must read "ok"
teille-sync run --dry-run    # which documents the next batch would take — claims nothing
teille-sync run              # one real batch
```

A `FAILED` row in `check`: see [`check` failed](#check-failed).

---

## Everyday use

Before each session:

```bash
cd teille-sync
source .venv/bin/activate
export GITHUB_TOKEN=<token>
# VPN up · share mounted (WSL: sudo mkdir -p /mnt/y && sudo mount -t drvfs Y: /mnt/y) · PyHellen and VieuxParler running
teille-sync check
```

Then run inside `tmux`, so that closing the window does not kill the run:

```bash
tmux new -s sync
teille-sync run --until-done     # batch after batch, until nothing is left À traiter
```

Detach with `Ctrl-b d`, come back with `tmux attach -t sync`. Closing a
terminal kills the process *without* the cleanup Ctrl+C does, and leaves
the running batch's cards stuck `En cours` — see
[A run stopped halfway](#a-run-stopped-halfway).

| To… | Run |
|---|---|
| see how many cards sit in each statut | `teille-sync status` |
| run exactly 3 batches | `teille-sync run --batches 3` |
| change the batch size for this run | `teille-sync run --batch-size 10` |
| see only the closing summary of each batch | `teille-sync run --until-done -q` |
| always see the preflight table | `teille-sync run -v` |
| no colour (logging to a file) | `teille-sync run --until-done --plain` |

`--until-done` stops on its own when:

- nothing is left `À traiter`;
- a batch is refused before converting — preflight failed, a service is
  down, the share dropped (exit 3);
- a batch claimed nothing while cards are still `À traiter` — another
  machine holds them, or claiming fails. Looping again would change
  nothing.

After each batch, a table lists every document judged (statut, phase,
losses, published or not), then a summary says what was claimed,
reclaimed and released. What each statut means:
[Board statuses](#board-statuses).

---

## A run stopped halfway

**You can pick up where you left off — document by document, not in the
middle of one.** The board is the memory: `run` only ever takes cards
`À traiter`, so a document already `Terminé`, `À vérifier`, `Échec` or
`Bloqué` is never redone. At worst, the batch that was running starts
over: its documents are fetched and converted again from scratch.

| How it stopped | The batch's cards | What to do |
|---|---|---|
| **Ctrl+C** | put back `À traiter` automatically (exit 130) | `teille-sync run` |
| **A service was down when the converter started**, or the converter refused (exit 3) | put back `À traiter` automatically | fix what its output names, `check`, `run` |
| **The VPN or the share dropped while fetching** (exit 3) | put back `À traiter` automatically | reconnect / remount, `check`, `run` |
| **A service died during conversion** | judged `Échec`, cause `Phase perdue` | [retry them](#retry-a-document) |
| **The converter crashed** (e.g. out of memory) — message *the converter left no run record* | judged `Bloqué`, cause `Autre` | [retry them](#retry-a-document) |
| **GitHub stopped answering** (exit 3) | may be stuck `En cours` | [release them](#release-stuck-cards), `run` |
| **Crash, `kill`, closed terminal, WSL shut down, power cut** | **stuck `En cours`** | [release them](#release-stuck-cards), `run` |

### Release stuck cards

1. **Find them.** `teille-sync status` only counts cards. On the GitHub
   board, filter on Status `En cours`: each card's `Détail` reads
   `<machine> · <date and time>` — who claimed it, and when.
2. **Release only what is really abandoned.** A card another machine is
   converting right now also reads `En cours`; check the machine name.
3. **Release, then run:**

   ```bash
   teille-sync release LIV0044 LIV0045 LIV0046
   teille-sync run
   ```

`release` puts the cards back `À traiter` and clears their claim stamp.
If even one identifier is not on the board, it releases none of them.

Doing nothing works too, only slower: at the start of every batch, `run`
takes back any card whose claim is older than `reclaim_after` (6 hours by
default).

### Two traps

- **The documents were already published when it stopped.** A crash
  between publication and the board update leaves the TEI on the share
  and the card `En cours`. After `release`, the next run refuses to
  overwrite it: the card gets its verdict, but its `Détail` ends with
  `publish refused: … already published … pass --republish`. Run that one
  batch as `teille-sync run --republish` — not `--until-done` — then
  carry on normally. To know beforehand, look for `tei/<ID>.tei.xml` or
  `tei/_a_verifier/<ID>.tei.xml` on the share.
- **Ctrl+C at the very end of a batch** — while publishing or writing
  cards. Those documents are already judged, so they are not put back
  `À traiter`: treat them like a crash.

---

## Retry a document

`run` never retries a card that is `Bloqué`, `Échec` or `À vérifier`.
Once the cause is fixed, put the card back by hand:

```bash
teille-sync release LIV0044
teille-sync run                  # add --republish if it was À vérifier or Terminé
```

`release` works on a card in any statut. The next `run` takes it with the
other `À traiter` cards, in identifier order. `--republish` is needed
whenever a TEI for that document is already on the share: an
`À vérifier` document already sits in `tei/_a_verifier/`, a `Terminé` one
in `tei/`.

Where to look for the cause:

- the card's `Cause`, `Phase` and `Détail` fields;
- `Bloqué` / `Absent du NAS`: is `OCR/zip_reconciliate/<ID>_reconciled.zip`
  on the share, and does it open?
- the local archive of every document not finished stays in `work/OCR/`;
- the converter's own record of each run (manifest, incidents, log):
  `work/tei_output/.teille-douce/runs/<when>/`.

---

## `check` failed

`teille-sync check` runs the eight checks every `run` runs before
claiming anything. It changes nothing on the board; on the share it only
creates `tei/` and `tei/_a_verifier/` if they are missing. All eight
always run, so one pass shows everything that is wrong.

| Row | What it tests | Fix |
|---|---|---|
| **VPN** | the NAS host answers on port 445 | Bring the VPN up. *does not resolve*: VPN down, or wrong `nas_host`. |
| **NAS root** | `nas_root` exists and holds `OCR/zip_reconciliate` | Mount the share (WSL: run the `sudo mount -t drvfs …` it prints). If the path exists but has no `OCR/zip_reconciliate`, `nas_root` points at the wrong level. |
| **Destination** | `tei/` on the share is writable | Write permissions on the share. *fix the NAS root check first*: the root is not mounted. |
| **Board** | `project-board-ids.json` can be read and is valid | `teille-sync ids refresh` — and check you are in `teille-sync/`. |
| **Converter** | `teille-douce` is on `PATH` | `source .venv/bin/activate`, or [install it](#1-install-teille-sync-and-the-converter). |
| **Services** | `teille-douce check` reports PyHellen, VieuxParler **and** the NER models `up` | Its report is printed as-is: start the service not marked `up`; NER not `up` means `pip install -e '../TEIlle-douce[ner]'`. If the report shows no services block at all, run `teille-douce check -i work/OCR` by hand. |
| **Metadata** | `metadata_csv` and `persons_csv` can be opened | Fix the paths in `config.local.toml`. A document with no catalogue row still converts; only an unreadable file is refused. |
| **Disk** | free space under `work_dir`: 300 MB per document, so 1.5 GB for a batch of 5 | Free some space, or set `work_dir` to a larger volume. |

---

## Other messages

| Message | What it means, what to do |
|---|---|
| `GITHUB_TOKEN is not set` | `export GITHUB_TOKEN=<token>` in this shell. |
| `project_url must look like https://github.com/orgs/<org>/projects/<n>` | Fix `project_url`. User-owned projects are not supported. |
| `cannot read project-board-ids.json` · `is not a valid id file` | `teille-sync ids refresh`, from the `teille-sync/` directory. |
| `the board has no field named …` · `…field has no option named …` | The board changed since the id file was made: `teille-sync ids refresh`. |
| `the converter refused before writing anything (exit 3)` | Its reason is in its own output just above. Every claim was released. |
| `the share stopped answering while fetching …` | VPN or mount dropped. Every claim was released: reconnect, `check`, `run`. |
| `the converter left no run record for this batch` | It crashed (often out of memory). The batch's documents are `Bloqué`: [retry them](#retry-a-document). |
| `stopping: this batch claimed nothing while the board still has cards à traiter` | Another machine holds them, or claiming fails. Look at `teille-sync status` and the board. |
| `the board refused N verdict(s) (…)` | Those documents converted, and are on the share if they succeeded, but their cards still read `En cours`. Set them by hand on the board, or `release` them and `run --republish`. |
| `publish refused: … already published … pass --republish` (in `Détail`) | A TEI for that document was already on the share, and was left alone. [Retry it](#retry-a-document) with `--republish` if the new version should replace it. |
| `publish refused: the upload failed …` or `…a different size than the source` (in `Détail`) | The share dropped while publishing. The card shows its statut, but the TEI may not be on the share: `release` it and `run --republish`. |
| `teille-sync: <setting>: … — using … instead` (on startup) | A bad value in a `TDSYNC_*` variable or in `config.local.toml`. The run goes on with the value named; fix the setting if that is not what you want. |

---

## Reference

### Commands

```bash
teille-sync run [--batches N | --until-done] [--batch-size N]
                [--republish] [--keep] [--reclaim-after 6h]
                [--dry-run] [-v | -q] [--plain]
teille-sync check          # the eight preflight checks; nothing changes on the board
teille-sync status         # card count per statut, read from the live board
teille-sync release DOC…   # put one or more cards back À traiter, whatever their statut
teille-sync ids refresh    # rebuild project-board-ids.json from the live board
```

Options of `run`:

| Option | Effect |
|---|---|
| `--batches N` | run exactly N batches (default 1) |
| `--until-done` | run batches until nothing is `À traiter` — [stops early](#everyday-use) when nothing can progress |
| `--batch-size N` | documents per batch (default 5) |
| `--reclaim-after D` | how old a claim must be before this run takes it back: `6h`, `90m`, `30s` |
| `--republish` | overwrite a document already on the share, in `tei/` or `tei/_a_verifier/` — a document only ever sits in one of the two, so the other copy is removed |
| `--keep` | never delete a local archive, finished or not |
| `--dry-run` | print which documents would be claimed; claim nothing |
| `-v` | always print the preflight table |
| `-q` | print only the closing summary, not the per-batch table |
| `--plain` | no colour, here *and* in the converter |

`--batches` and `--until-done` cannot be combined; neither can `-v` and `-q`.

### Settings

Each setting resolves through four layers, first match wins: a
command-line flag, then a `TDSYNC_*` environment variable, then a key in
`config.local.toml`, then the default. A bad value in the environment or
the config file prints a warning naming the value used instead; the same
bad value typed as a flag is a usage error (exit 2).

| `config.local.toml` key | Environment variable | Flag | Default | What it is |
|---|---|---|---|---|
| `nas_root` | `TDSYNC_NAS_ROOT` | — | *required* | local mount point of the share |
| `nas_host` | `TDSYNC_NAS_HOST` | — | *required* | NAS host name, dialled by the VPN check |
| `project_url` | `TDSYNC_PROJECT_URL` | — | *required by `ids refresh`* | `https://github.com/orgs/<org>/projects/<n>` |
| `ids_file` | `TDSYNC_IDS_FILE` | — | `project-board-ids.json` | the board id file |
| `batch_size` | `TDSYNC_BATCH_SIZE` | `--batch-size` | `5` | documents per batch |
| `reclaim_after` | `TDSYNC_RECLAIM_AFTER` | `--reclaim-after` | `6h` | age after which a claim `En cours` is taken back |
| `work_dir` | `TDSYNC_WORK_DIR` | — | `work` | local archives and converter output |
| `metadata_csv` | `TDSYNC_METADATA_CSV` | — | `metadata_livre.csv` | catalogue of volumes, passed as `--metadata` |
| `persons_csv` | `TDSYNC_PERSONS_CSV` | — | `metadata_personne.csv` | catalogue of persons, passed as `--persons` |
| `entities_dir` | `TDSYNC_ENTITIES_DIR` | — | `entities` | where the converter writes entity CSVs, passed as `--entities` |

`GITHUB_TOKEN` is read from the environment only. Relative paths resolve
against the current directory.

### Board statuses

| Statut | Meaning | On the share | Local archive in `work/OCR/` | Taken by `run`? |
|---|---|---|---|---|
| `À traiter` | not processed yet, or released | — | — | **yes** |
| `En cours` | claimed; `Détail` reads `<machine> · <date and time>` | — | being worked on | only once older than `reclaim_after` |
| `Terminé` | converted, valid, no incident | `tei/<ID>.tei.xml` | deleted once published (unless `--keep`) | no |
| `À vérifier` | converted, but schema validation failed, the validator's report was unreadable, or incidents were recorded (lost pages…) | `tei/_a_verifier/<ID>.tei.xml` | kept | no |
| `Échec` | the converter failed on the document, or an annotation phase produced nothing | nothing | kept | no |
| `Bloqué` | the archive is absent from the share, truncated or unreadable — or the converter never saw the document | nothing | kept, if it was fetched | no |

A verdict writes `Status`, `Cause`, `Phase`, `Détail`, `Pertes`, `Pages`,
`Date de traitement` and `Version pipeline`.

### Where files go

On the share (`nas_root`):

```
OCR/zip_reconciliate/<ID>_reconciled.zip   input, only ever read
tei/<ID>.tei.xml                           Terminé
tei/_a_verifier/<ID>.tei.xml               À vérifier
tei/entities/<ID>/                         entity CSVs (Terminé and À vérifier)
```

Locally (`work_dir`, gitignored):

```
work/OCR/<ID>_reconciled.zip               fetched archive — deleted once Terminé and published
work/tei_output/                           converter output — never cleaned by teille-sync
work/tei_output/.teille-douce/runs/<when>/ each converter run's manifest, incidents and log
```

`work/tei_output/` only grows; empty it by hand between runs if space
runs short.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | every document judged was `Terminé` (or there was nothing to do) |
| `1` | at least one document was not `Terminé` — `À vérifier` included — or a publish or a board write failed |
| `2` | usage error: a bad flag, or `release` of an identifier not on the board |
| `3` | nothing was converted: preflight refused, the converter refused, the share dropped, or GitHub / the id file failed |
| `130` | interrupted with Ctrl+C |

---

## Maintenance

### Refresh the id file

```bash
teille-sync ids refresh
```

Run it:

- after renaming or recreating any field or single-select option in the
  GitHub UI — that mints a new id, and every lookup in this tool goes by
  name through whatever `project-board-ids.json` last recorded;
- right after `scripts/realign_cause.py` (below);
- whenever a message says *the id file is stale*.

Adding cards to the board does not require it.

A missing `Status` field or option is a refusal. A missing `Cause` option
is **skipped silently** — the pipeline is allowed to emit a cause the
board does not model — so a stale file can leave the `Cause` column
blank without a word.

### Realign the board's Cause options

`scripts/realign_cause.py` replaces the `Cause` field's options with the
twelve the pipeline actually emits. Run it from the project's venv, and
always with `--check` first:

```bash
python scripts/realign_cause.py --check   # inspect only, writes nothing
python scripts/realign_cause.py           # inspect, then replace
teille-sync ids refresh                   # NOT optional
```

- It refuses outright if any card already carries a `Cause` value: the
  mutation replaces the option list wholesale and would detach every such
  card from what it points to.
- **`teille-sync ids refresh` right afterwards is part of the migration.**
  Every option gets a new id, so the moment the script returns,
  `project-board-ids.json` describes a `Cause` field that no longer
  exists — and every `Cause` write would be skipped, silently, for the
  whole corpus.

---

## Design notes

### How a claim works

Claiming a card sets it `En cours` and writes a stamp
(`<machine> · <time>`) into `Détail`, then reads the card back. If the
stamp read back is not this machine's, another machine won the race, and
this one skips the card instead of converting it twice. Every machine
walks the pending cards in identifier order, so two machines starting at
once collide on the same cards — visibly — rather than silently
overlapping.

Claiming a card and writing its verdict are two separate calls to the
board, which is why a crash in between leaves a card `En cours`. A
later run only reclaims a card whose stamp it can read and show is older
than `reclaim_after`: a `Détail` in any other shape is left alone.

The TEI is always published *before* the final statut is written, so a
card never reads `Terminé` for a document whose publication was never
attempted.

### Annotation phases are mandatory

Every `run` calls the converter with `--phases all --require-services`.
Enrichment (PyHellen), modernization (VieuxParler) and named-entity
recognition run on every document, or on none: `--require-services`
makes the converter refuse before writing anything if a phase's service
is unreachable, rather than quietly produce a document that claims more
annotation than it got.

The Services check reads the services block printed by the converter's
own `teille-douce check`, not its exit code. Preflight runs before
anything is fetched, so the input directory is empty, and an empty input
exits non-zero (*unusable — nothing to convert*) with all three services
up: gating on that exit code refused every run on a clean machine. A
report in which the three service rows cannot all be found is still a
refusal — a gate that passes because it failed to parse is worse than no
gate.

### Catalogue paths are passed explicitly

`metadata_csv`, `persons_csv` and `entities_dir` are passed to
`teille-douce` on every call rather than left to its own config
discovery, which walks up the parent directories from wherever it runs
and could otherwise pick up someone else's files without a word.
