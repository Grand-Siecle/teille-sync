"""One batch: claim, convert, judge, publish, release.

`run_batch()` is the whole point of this project — the other modules are
its ingredients. The seven steps below are the design spec's, in the
spec's order, and the order is the design:

    0. preflight   — refuse before anything is touched
    1. reclaim, select, claim
    2. fetch
    3. convert (one call for the whole batch)
    4. validate
    5. judge      (verdict.decide, per document)
    6. publish    (before the final status is written)
    7. write to the board, then clean up local sources

Two rules run through the middle of it, both about what happens when
something dies halfway:

* **Publication happens before the final status is written.** A card must
  never say `Terminé` for a document that did not reach the share. A
  publish failure keeps the computed status, appends the reason to the
  verdict's own `Détail`, and pushes the exit code to `SOME_FAILED` — it
  does not invent a new status.
* **The converter's exit code 3 means it refused before writing anything**
  (`--require-services`, a service down between the preflight probe and
  the run). Every card claimed this batch goes back to `À traiter`
  unjudged: a card reading `Échec` for a document that was never opened
  is the same lie as a loss counter left at zero by a dead service.
* **A converter that dies without writing a run record is not a converter
  that wrote nothing.** `output_dir` persists across batches, and
  teille-douce only writes `run.json` on a clean exit. `latest_run()` is
  read before *and* after the call, and an unchanged (or missing) run
  directory means this batch's own documents judge as if nothing had
  converted — never against whatever the previous batch left behind.

A `KeyboardInterrupt` anywhere between claiming and judging is caught here,
releases every card claimed but not yet judged (`verdict.decide()` has not
run for it), and is re-raised — the CLI is what turns that into exit 130.

Owns no console: everything below is a `BatchResult` for a later module
(Task 8's `report.py`) to render.
"""

import re
import shutil
import socket
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import Path

from teille_sync import exits, nas
from teille_sync.convert import (latest_run, read_incidents, read_manifest,
                                 run_converter, validate)
from teille_sync.names import pipeline_name
from teille_sync.preflight import CONVERTER_NAME, preflight
from teille_sync.verdict import DONE, REVIEW, decide

# One `<surface>` per page in teille-douce's own output (confirmed against
# the project's real fixtures: 190 `<surface>` and 190 `<pb>` on the same
# document). A plain substring count rather than an XML parse: teille_sync
# carries no XML dependency, and counting one repeated tag needs none.
_SURFACE_RE = re.compile(r"<surface[ >/]")

# The Converter preflight check's own detail string ends with
# "(<version>)" when `--version` answered. Harvested here rather than
# shelling out a second time.
_VERSION_RE = re.compile(r"\(([^()]+)\)\s*$")


@dataclass
class BatchResult:
    """What one `run_batch()` call did.

    `outcomes` and `published` are keyed on the bare identifier
    (`LIV0001`), matching `Card.identifier` — the board is what a caller
    of this result cares about, not the pipeline's internal name.

    `claimed`, `reclaimed` and `released` exist so a batch that claimed
    five cards and had to hand all five back (a dead service, an
    interrupt) is never mistaken, downstream, for a batch that found
    nothing to do: an empty `outcomes` dict means two very different
    things depending on whether `claimed` is also empty.

    `checks` carries this call's own `preflight()` result (passing or
    failing) so a caller running several batches in a row — Task 8's
    `cli.py`, under `--batches`/`--until-done` — can display what this
    batch found without probing a second time itself. Per-batch
    re-probing here, inside `run_batch`, stays correct and necessary (a
    service can die between batches); it is a second call for the *same*
    batch, from the caller on top of this one, that is the waste.
    """
    outcomes: dict = field(default_factory=dict)
    published: dict = field(default_factory=dict)
    exit_code: int = exits.OK
    claimed: list = field(default_factory=list)
    reclaimed: list = field(default_factory=list)
    released: list = field(default_factory=list)
    message: str = ""
    checks: list = field(default_factory=list)


def _machine_name():
    """Which machine this run's claims are stamped with. Read at call
    time from the OS — never hardcoded, so no real host name is ever
    written into this repository's source or tests."""
    return socket.gethostname()


def _pipeline_version(checks):
    """The *Version pipeline* board field: the converter's own version,
    as the Converter preflight check already found it. Reading it back
    from that check avoids a second `--version` subprocess call."""
    detail = next((c.detail for c in checks if c.name == "Converter"), "")
    match = _VERSION_RE.search(detail)
    if match:
        return f"{CONVERTER_NAME} {match.group(1)}"
    return CONVERTER_NAME


def _tei_page_count(tei_path):
    try:
        text = Path(tei_path).read_text("utf-8", errors="replace")
    except OSError:
        return None
    return len(_SURFACE_RE.findall(text)) or None


def _archive_page_count(archive_path):
    """The archive's own `.xml` entry count — unambiguous, and the
    fallback whenever there is no TEI file to count surfaces in."""
    try:
        with zipfile.ZipFile(archive_path) as zf:
            return sum(1 for name in zf.namelist() if name.lower().endswith(".xml"))
    except (zipfile.BadZipFile, OSError):
        return 0


def _pages_for(identifier, output_dir, archive_path):
    """The TEI's own surface count where a file exists, the archive's XML
    entry count where it does not — the design's own rule for `Pages`."""
    tei_path = Path(output_dir) / f"{pipeline_name(identifier)}.tei.xml"
    if tei_path.is_file():
        count = _tei_page_count(tei_path)
        if count is not None:
            return count
    if archive_path is not None:
        return _archive_page_count(archive_path)
    return 0


def _append_detail(verdict, note):
    """A verdict's `Détail`, with one more reason appended — used when a
    publish failure must be visible without inventing a new status."""
    detail = f"{verdict.detail}; {note}" if verdict.detail else note
    return replace(verdict, detail=detail)


def _delete_local_source(input_dir, identifier):
    """What `fetch()` left behind for one document: the archive itself,
    and anything the converter expanded from it under the same name.

    The archive's local name comes from `pipeline_name()`, not a
    hand-written `f"{identifier}_reconciled.zip"` — `names.py` is the one
    place that suffix is spelled out; anywhere else it can drift silently
    out of sync with `nas.fetch()`'s own naming."""
    input_dir = Path(input_dir)
    (input_dir / f"{pipeline_name(identifier)}.zip").unlink(missing_ok=True)
    expanded = input_dir / pipeline_name(identifier)
    if expanded.is_dir():
        shutil.rmtree(expanded, ignore_errors=True)


def run_batch(settings, board, now, republish=False, keep=False):
    # -- 0. Preflight ---------------------------------------------------
    checks = preflight(settings)
    failing = [c for c in checks if not c.ok]
    if failing:
        detail = "; ".join(f"{c.name}: {c.detail}" for c in failing)
        return BatchResult(exit_code=exits.MISCONFIGURED,
                           message=f"preflight refused — {detail}",
                           checks=checks)

    machine = _machine_name()
    input_dir = Path(settings.work_dir) / "OCR"
    output_dir = Path(settings.work_dir) / "tei_output"

    # `claimed` is mutated in place by the claiming loop below, and read
    # by the `except` clause: a Ctrl-C during claim() itself (a network
    # call) must release whatever this run had already claimed, not only
    # what it claimed before the fetch/convert/judge stage began.
    claimed = []
    judged = set()
    try:
        # -- 1. Reclaim, then select, then claim -----------------------------
        reclaimed = []
        for card in board.stale(settings.reclaim_after, now):
            board.release(card)
            reclaimed.append(card.identifier)

        # Walk every pending card in title order, claiming as we go. A
        # card lost to another machine (`claim()` returns False) is
        # simply skipped — the loop moves on to the next pending card
        # rather than stopping or retrying, which is what "the next one
        # taken" means.
        for card in board.pending():
            if len(claimed) >= settings.batch_size:
                break
            if board.claim(card, machine, now):
                claimed.append(card)

        result = BatchResult(claimed=[c.identifier for c in claimed],
                             reclaimed=reclaimed, checks=checks)
        if not claimed:
            return result

        # -- 2. Fetch -----------------------------------------------------
        fetched_path = {}
        fetch_reason = {}
        for card in claimed:
            local, reason = nas.fetch(settings.nas_root, card.identifier, input_dir)
            fetched_path[card.identifier] = local
            if local is None:
                fetch_reason[card.identifier] = reason

        pipeline_docs = [pipeline_name(c.identifier) for c in claimed
                        if fetched_path[c.identifier] is not None]

        # -- 3. Convert (one call for the whole batch) ---------------------
        # Captured *before* the call: `output_dir` persists across
        # batches, and teille-douce's own store only writes `run.json` on
        # a clean exit. A converter killed partway through (an OOM is
        # plausible on this corpus) leaves whatever run directory was
        # already there — this batch's own documents must not be judged
        # against a previous batch's manifest just because it happens to
        # still be the newest thing in `output_dir`.
        run_dir_before = latest_run(output_dir)
        manifest, incidents, validation = {}, [], {}
        if pipeline_docs:
            converter_exit = run_converter(pipeline_docs, input_dir, output_dir,
                                           settings.metadata_csv, settings.persons_csv,
                                           settings.entities_dir)
            if converter_exit == 3:
                # Nothing was written. Blaming a document that was never
                # opened would be the same lie as a loss counter left at
                # zero by a dead server — release, don't judge.
                for card in claimed:
                    board.release(card)
                result.released = [c.identifier for c in claimed]
                result.exit_code = exits.MISCONFIGURED
                result.message = ("the converter refused before writing "
                                  "anything: a required service is down")
                return result

            notes = []
            if converter_exit != 0:
                notes.append(f"the converter exited {converter_exit}")

            # -- 4. Validate ------------------------------------------------
            run_dir = latest_run(output_dir)
            if run_dir is None or run_dir == run_dir_before:
                # No new run was recorded for this batch's documents — a
                # crash before the converter's own `store.finish()` could
                # write one. Judging against `run_dir_before` (someone
                # else's manifest, possibly for entirely different
                # documents) would credit or blame this batch for records
                # that are not its own, so every document here judges as
                # if the run had produced nothing at all.
                notes.append("the converter left no run record for this batch")
            else:
                manifest = read_manifest(run_dir)
                incidents = read_incidents(run_dir)
                validation = validate(output_dir, pipeline_docs)

            if notes:
                result.message = "; ".join(notes)

        # -- 5. Judge ---------------------------------------------------------
        outcomes = {}
        for card in claimed:
            doc = pipeline_name(card.identifier)
            was_fetched = fetched_path[card.identifier] is not None
            verdict = decide(doc, manifest, incidents, validation.get(doc), was_fetched)
            if not was_fetched and fetch_reason.get(card.identifier):
                # `decide()` always writes the same generic "the archive
                # was never fetched" — true, but it throws away *why*:
                # absent from the share, a truncated copy, an archive
                # that will not open. The real reason from `nas.fetch()`
                # is appended rather than replacing verdict.py's own
                # label, so "Absent du NAS" still names the right cause
                # even for a truncated copy (a corrupt archive is not on
                # the share in any usable sense either).
                verdict = _append_detail(verdict, fetch_reason[card.identifier])
            outcomes[card.identifier] = verdict
            judged.add(card.identifier)

        # -- 6. Publish (before the final status is written) ------------------
        published = {}
        for card in claimed:
            verdict = outcomes[card.identifier]
            if verdict.status not in (DONE, REVIEW):
                continue   # Bloqué / Échec: nothing leaves the machine
            tei_path = output_dir / f"{pipeline_name(card.identifier)}.tei.xml"
            entities = Path(settings.entities_dir) / pipeline_name(card.identifier)
            ok, reason = nas.publish(
                tei_path, entities if entities.is_dir() else None,
                settings.nas_root, card.identifier,
                review=(verdict.status == REVIEW), republish=republish)
            published[card.identifier] = ok
            if not ok:
                outcomes[card.identifier] = _append_detail(
                    verdict, f"publish refused: {reason}")
                result.exit_code = max(result.exit_code, exits.SOME_FAILED)

        result.outcomes = outcomes
        result.published = published

        # -- 7a. Write every verdict to the board -----------------------------
        version = _pipeline_version(checks)
        for card in claimed:
            pages = _pages_for(card.identifier, output_dir, fetched_path[card.identifier])
            board.write(card, outcomes[card.identifier], pages, version, now)

        # -- 7b. Clean up: keep the failures, delete the finished --------------
        if not keep:
            for card in claimed:
                verdict = outcomes[card.identifier]
                finished = verdict.status == DONE and published.get(card.identifier)
                if finished:
                    _delete_local_source(input_dir, card.identifier)

        if any(v.status != DONE for v in outcomes.values()):
            result.exit_code = max(result.exit_code, exits.SOME_FAILED)

        return result

    except KeyboardInterrupt:
        for card in claimed:
            if card.identifier not in judged:
                board.release(card)
        raise
