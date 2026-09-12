# teille-sync — design

*2026-09-12. Decided with the project lead; open questions all closed.*

The corpus is 396 reconciled ALTO archives sitting on a university NAS, and
one GitHub Project tracking what has been converted. `teille-sync` is the
thing in between: it takes what is still to do, brings five archives over the
VPN, runs `teille-douce` on them, reads what the run says it lost, writes that
onto the board, and puts the TEI back on the NAS.

It lives in its own repository, `Grand-Siecle/teille-sync`. It never imports
`teille_douce`; it calls the CLI and reads the records that CLI already
writes. The converter gains no dependency on a NAS, a VPN or a GitHub token.

## The address is not in this repository

Both `TEIlle-douce` and `teille-sync` are public. The NAS host, the share and
the path are **configuration, never code**:

| | |
|---|---|
| `TDSYNC_NAS_ROOT` | the mount point, per machine (`Y:\`, `/mnt/y`, `/Volumes/…`) |
| `TDSYNC_NAS_HOST` | the host the VPN check dials |
| `config.local.toml` | same two keys, gitignored, for people who prefer a file |
| `config.example.toml` | committed, fictional values only |

Nothing that resolves the real address is written to any file the repository
tracks, and the preflight prints the *mount point* it used, never the UNC
path behind it.

## What a run looks like

One invocation processes **one batch of five**. `--batches N` and
`--until-done` chain them. The default is one because a VPN that drops in the
third hour must not leave sixty cards in a state nobody can read.

### 0. Preflight — refuses before it touches anything

Each check prints a line. Any failure exits **3** with nothing changed, no
card moved, no file copied.

1. **VPN** — resolve `TDSYNC_NAS_HOST`, then open TCP 445 with a short
   timeout. Reachability is the only honest test: a tunnel that is up but has
   no route to the share passes an interface check and fails one step later.
2. **Root mounted** — `TDSYNC_NAS_ROOT` exists, is readable, and holds
   `OCR/zip_reconciliate/`. When the drive is mapped on Windows but absent
   from WSL — the common case on this project's machines — say so and give
   the command (`sudo mount -t drvfs Y: /mnt/y`) rather than "root not found".
3. **Destination writable** — `tei/` exists or can be created, and
   `tei/_a_verifier/` likewise.
4. **Board reachable** — the token resolves, the project answers, and every
   field and option id in the id file still exists. A field someone recreated
   in the UI has a new id, and a sync writing to a dead id fails silently.
5. **Converter present** — `teille-douce --version`, recorded for the
   *Version pipeline* field.
5b. **Services up** — `teille-douce check -i <work>/OCR --strict`, whose
   output is shown verbatim when it refuses. The converter's own preflight
   probes VieuxParler, PyHellen *and* the NER dependencies, and its code
   says why teille-sync must not re-probe: a preflight that disagrees with
   the run is worse than none. All three phases are mandatory for this
   corpus (below), so a refused probe stops the batch before a single card
   is claimed.
6. **Metadata present** — `metadata_livre.csv` and `metadata_personne.csv`
   are readable. Their coverage is *not* a gate: see the note below.
7. **Disk** — free space for the batch, with margin. Archives average 30 MB;
   a batch is roughly 150 MB in, plus what the conversion writes.

### 1. Select and claim

Read every card with `Statut = À traiter`, ordered by title so two machines
starting at once tend to collide loudly rather than quietly. Take five.

**Claiming is the lock.** Before anything is downloaded, each card moves to
`En cours` with `Détail = <machine> · <ISO timestamp>`. Then the card is read
back: if the detail is not ours, another machine won the race and we take the
next one instead. GitHub Projects offers no transaction, so this optimistic
check is what there is — and it is enough, because the loser skips rather
than duplicates.

**Dead claims.** A machine that dies leaves cards in `En cours` forever.
`--reclaim-after 6h` makes such cards eligible again, and says which ones it
took and how old their claim was.

**Ctrl-C releases.** Cards claimed but not yet converted go back to
`À traiter`. Without this, every interruption costs five documents.

### 2. Fetch

For each document: copy `<root>/OCR/zip_reconciliate/<doc>_reconciled.zip`
to the local `OCR/`.

| Case | Outcome |
|---|---|
| absent from the NAS | `Bloqué` · cause **Absent du NAS** |
| copies short, or size differs | one retry, then `Bloqué` · **ZIP illisible** |
| archive will not open (`namelist()`) | `Bloqué` · **ZIP illisible** |
| VPN drops mid-batch | remaining claims released, exit 3 |

Size comparison plus opening the central directory is the integrity test. A
checksum over the VPN costs more than it catches: truncation is what happens
here, and truncation changes the size.

### 3. Convert — all three annotation phases, or nothing

This corpus wants the complete article: linguistic enrichment, modernization
and entity recognition on every document. A volume converted without them is
not a cheaper success, it is a different object — and the pipeline's default
is to carry on without a phase whose service is down, which would fill the
board with green cards for files missing half their annotation.

```
teille-douce run <doc…> -i OCR -o tei_output --phases all --require-services
```

`--require-services` is what makes that impossible: *"a phase whose service
is down is a fatal error before anything is written"*. One invocation for all
five — services are probed once per run, not once per document.

**When it refuses, nothing was attempted**, so the five claimed cards go back
to `À traiter` rather than to `Échec`. A card that says a document failed
when the document was never opened is the same lie as a loss counter left at
zero by a dead server. The batch still exits non-zero and says which service
was missing.

The child's output is streamed straight through, so the terminal looks like a
normal run. `--plain` is passed when stdout is not a TTY.

### 4. Validate

`teille-douce validate tei_output --json` over the files just produced.

### 5. Verdict

Read `tei_output/.teille-douce/runs/<latest>/run.json` — `documents` maps each
stem to `ok` or `failed` — and `incidents.jsonl`, one incident per line with
`code`, `step`, `count`, `total`, `detail`.

| Evidence | Statut |
|---|---|
| never fetched, or absent from `run.json` | **Bloqué** |
| `failed` | **Échec** — Phase from `step`, Cause from `code`, Détail from `detail` |
| `ok`, but a `phase_lost` incident | **Échec** — an annotation phase did not run |
| `ok`, validation passes, no incident | **Terminé** |
| `ok`, but validation fails or other incidents exist | **À vérifier** — Pertes = sum of incident counts |

**A lost phase is a failure, not a blemish.** A service that dies *between*
the probe and the document emits `phase_lost` while the document itself
converts and is recorded `ok` — it is written without a single `<choice>`, or
without one `<w>`, and reported as converted. For a corpus that wants all
three phases, that document is wrong, and the board must say so rather than
file it under "worth a look".

`Pages` comes from the TEI's own surface count where a file exists, and from
the archive's XML count where it does not.

**Missing metadata is not a block.** `find_metadata_row()` extracts the
`LIV####` root, so the 121 variants (`LIV0002a`, `LIV0016_t1`, `LIV0325_v1`…)
resolve to their base row and coverage is complete. And a row that is genuinely
absent does not fail the document: it produces a header of placeholders and a
warning. That path leads to `À vérifier`, never `Bloqué`.

### 6. Publish

| Statut | Destination |
|---|---|
| `Terminé` | `<root>/tei/<doc>.tei.xml` + `<root>/tei/entities/<doc>/*.csv` |
| `À vérifier` | `<root>/tei/_a_verifier/<doc>.tei.xml` + entities alongside |
| anything else | nothing leaves the machine |

A file already present is **refused** unless `--republish`. Publication happens
before the final status is written, so a card never claims `Terminé` for a
document that did not reach the NAS; when a copy fails, the computed status is
still written, the reason is appended to `Détail`, and the run exits non-zero.

### 7. Clean up and report

Sources of finished documents are deleted; **sources of failures are kept**,
because a document you cannot reproduce is a document you cannot fix.
`--keep` disables deletion entirely.

The closing table is one line per document — statut, phase, pertes, published
or not — followed by the batch counts. Exit **0** when every document
finished, **1** when some failed, **3** on preflight or a lost VPN, **130** on
interrupt.

## The board contract

Eight fields. `Status` keeps its English name — GitHub refuses to rename the
built-in field — with French options.

`Cause` is realigned onto the codes the pipeline actually emits, so the sync
translates rather than guesses:

| Option | Source |
|---|---|
| Absent du NAS | file not found on the share |
| ZIP illisible | `archive_corrupt` |
| Volume illisible | `volume_unreadable` |
| Pages perdues | `page_unusable` |
| Conteneur en échec | `container_failed` |
| Phase perdue | `phase_lost` |
| Lot refusé | `batch_failed` |
| Relance sans réponse | `retry_unanswered` |
| Disjoncteur | `breaker_skipped` |
| Document en échec | `document_failed` |
| Validation schéma | `validate --json` |
| Autre | anything else, with `Détail` |

`Phase` keeps its ten options, but only six are ever written automatically —
the pipeline emits `expand`, `sourcedoc`, `enrich`, `modernize`, `ner` and
`run`. `Métadonnées`, `En-tête TEI`, `Corps` and `Écriture` stay for manual
use, and the sync leaves them alone rather than guessing.

## Testing

The NAS, the VPN and the GitHub API are all faked at their boundary: a
temporary directory standing in for the share, a socket check that answers
what the test says, and a recorded GraphQL transport. What is tested for real
is the verdict — given a `run.json`, an `incidents.jsonl` and a validation
result, which status, which cause, which count. That is the part that can be
wrong in a way nobody notices.

Every failure path above gets a test, including the ones that are tedious:
the claim lost to another machine, the stale claim reclaimed, the interrupt
that releases, the publication refused because the file exists.

## Out of scope

Retrying failures (`teille-douce run --retry-failed` already does it, and
which failures deserve a retry is a human call), editing metadata, and
anything that writes to the NAS outside `tei/`.
