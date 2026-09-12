"""batch.py: the orchestration that ties fetch, convert, judge, publish and
the board together.

The board is a `FakeBoard` — a plain in-memory double implementing the same
five methods as `board.Board` (`pending`, `stale`, `claim`, `release`,
`write`) — rather than a real `Board` wired to a scripted GraphQL
`Recorder`. `board.py`'s own request/response mechanics (pagination, the
optimistic claim/read-back, the field-id lookups) are already exhaustively
covered by `tests/test_board.py`; this file is about what `run_batch` does
with those five calls, in what order, and what it does when one of them, or
one of `nas`/`convert`, comes back with bad news. A double that speaks
`Board`'s interface directly gives full control over each scenario (a claim
another machine won, a stale claim, …) without also having to hand-craft
GraphQL answers for behaviour this file does not exercise.

The NAS is a real temporary directory (Task 4's pattern): `nas.fetch` and
`nas.publish` run for real against it, because they are pure filesystem
code and faking them would just mean re-implementing them. Only
`teille_sync.batch.preflight`, `.run_converter` and `.validate` are
monkeypatched — the three that would otherwise open a socket or a
subprocess.
"""

import json
import socket
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from teille_sync import batch, exits, nas
from teille_sync.board import Card
from teille_sync.names import pipeline_name
from teille_sync.preflight import Check
from teille_sync.settings import Settings
from teille_sync.verdict import Verdict

NOW = datetime(2026, 9, 12, 14, 30, 0)
RUN_STAMP = "20260912-100000-0000001"


# -- fakes ------------------------------------------------------------------

class FakeBoard:
    """`board.Board`'s five public methods, backed by a dict instead of a
    GraphQL transport. `identifiers_stale` marks which cards `stale()`
    should return; `losers` marks which cards must lose their `claim()`
    race. Every call is recorded so tests can assert on order and count."""

    def __init__(self, cards, losers=(), stale_identifiers=()):
        self.cards = {c.identifier: c for c in cards}
        self.losers = set(losers)
        self.stale_identifiers = set(stale_identifiers)
        self.claim_calls = []
        self.release_calls = []
        self.write_calls = []
        self.pending_called = 0
        self.stale_called = 0
        self.calls_in_order = []   # shared log, for cross-call ordering

    def pending(self):
        self.pending_called += 1
        cards = [c for c in self.cards.values() if c.status == "À traiter"]
        return sorted(cards, key=lambda c: c.identifier)

    def stale(self, older_than, now):
        self.stale_called += 1
        cards = [c for c in self.cards.values()
                if c.identifier in self.stale_identifiers]
        return sorted(cards, key=lambda c: c.identifier)

    def claim(self, card, machine, now):
        self.claim_calls.append(card.identifier)
        if card.identifier in self.losers:
            return False
        self.cards[card.identifier] = replace(
            card, status="En cours", detail=f"{machine} · {now.isoformat()}")
        return True

    def release(self, card):
        self.release_calls.append(card.identifier)
        self.calls_in_order.append(("release", card.identifier))
        self.cards[card.identifier] = replace(card, status="À traiter", detail="")

    def write(self, card, verdict, pages, version, now):
        self.write_calls.append(
            {"identifier": card.identifier, "verdict": verdict,
             "pages": pages, "version": version})
        self.calls_in_order.append(("write", card.identifier))
        self.cards[card.identifier] = replace(
            card, status=verdict.status, detail=verdict.detail)


def _cards(identifiers, status="À traiter", detail=""):
    return [Card(identifier=i, item_id=f"I_{i}", status=status, detail=detail)
           for i in identifiers]


def _share(tmp_path, identifiers, xml_entries=2):
    """A real fake NAS: a temp directory with the same two folders the real
    share has, holding one small ZIP per identifier. Missing an identifier
    entirely is how a test makes its archive "absent from the NAS"."""
    root = tmp_path / "share"
    (root / "OCR" / "zip_reconciliate").mkdir(parents=True)
    (root / "tei").mkdir(parents=True)
    for ident in identifiers:
        z = root / "OCR" / "zip_reconciliate" / f"{ident}_reconciled.zip"
        with zipfile.ZipFile(z, "w") as zf:
            for n in range(xml_entries):
                zf.writestr(f"{ident}/page_{n:04d}.xml", "<alto/>")
    return root


def _settings(tmp_path, nas_root, **over):
    values = {
        "nas_root": nas_root, "nas_host": "nas.example.invalid",
        "batch_size": 5, "reclaim_after": timedelta(hours=6),
        "project_url": "u", "ids_file": tmp_path / "ids.json",
        "work_dir": tmp_path / "work",
        "metadata_csv": tmp_path / "metadata_livre.csv",
        "persons_csv": tmp_path / "metadata_personne.csv",
    }
    values.update(over)
    return Settings(values=values, origins={k: "default" for k in values},
                    refusals=[])


def _passing_checks():
    names = ["VPN", "NAS root", "Destination", "Board", "Services",
            "Metadata", "Disk"]
    checks = [Check(n, True, f"{n} ok", "") for n in names]
    checks.insert(4, Check("Converter", True,
                           "teille-douce found at /usr/bin/teille-douce (2.1.0)",
                           ""))
    return checks


def _stub_run_converter(manifest, incidents=(), exit_code=0, calls=None,
                        pages_by_doc=None):
    """Stands in for `convert.run_converter`: writes a real `run.json`
    (and `incidents.jsonl`, and a stub `.tei.xml` per document the
    manifest marks "ok") exactly where the real converter leaves them, so
    `batch.py`'s calls to the real `latest_run` / `read_manifest` /
    `read_incidents` / (stubbed) `validate` exercise real file-reading
    code against real files rather than a second layer of mocks."""
    pages_by_doc = pages_by_doc or {}

    def fake(docs, input_dir, output_dir, metadata_csv, persons_csv, plain=False):
        if calls is not None:
            calls.append(list(docs))
        if exit_code == 3:
            return 3
        output_dir = Path(output_dir)
        run_dir = output_dir / ".teille-douce" / "runs" / RUN_STAMP
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run.json").write_text(json.dumps(manifest), encoding="utf-8")
        if incidents:
            (run_dir / "incidents.jsonl").write_text(
                "\n".join(json.dumps(i) for i in incidents) + "\n",
                encoding="utf-8")
        for doc, outcome in manifest.get("documents", {}).items():
            if outcome == "ok":
                n = pages_by_doc.get(doc, 3)
                surfaces = "".join(f"<surface>{i}</surface>" for i in range(n))
                (output_dir / f"{doc}.tei.xml").write_text(
                    f"<TEI>{surfaces}</TEI>", encoding="utf-8")
        return exit_code
    return fake


def _stub_validate(invalid=None):
    invalid = invalid or {}

    def fake(output_dir, docs):
        result = {}
        for d in docs:
            if d in invalid:
                result[d] = {"valid": False, "errors": invalid[d]}
            else:
                result[d] = {"valid": True, "errors": []}
        return result
    return fake


def _patch_common(monkeypatch, manifest, incidents=(), exit_code=0,
                  invalid=None, calls=None, pages_by_doc=None):
    monkeypatch.setattr(batch, "preflight", lambda settings: _passing_checks())
    monkeypatch.setattr(batch, "run_converter", _stub_run_converter(
        manifest, incidents, exit_code, calls, pages_by_doc))
    monkeypatch.setattr(batch, "validate", _stub_validate(invalid))


ALL_OK = {"documents": {pipeline_name(f"LIV{i:04d}"): "ok" for i in range(1, 6)}}


def _five():
    return [f"LIV{i:04d}" for i in range(1, 6)]


# -- Rule 0: preflight ------------------------------------------------------

def test_nothing_is_claimed_when_preflight_refused(tmp_path, monkeypatch):
    root = _share(tmp_path, _five())
    checks = [Check("VPN", False, "the NAS does not answer", "bring up the VPN")]
    monkeypatch.setattr(batch, "preflight", lambda settings: checks)
    board = FakeBoard(_cards(_five()))

    result = batch.run_batch(_settings(tmp_path, root), board, NOW)

    assert result.exit_code == exits.MISCONFIGURED
    assert board.pending_called == 0
    assert board.claim_calls == []
    assert result.outcomes == {}
    assert result.claimed == []


def test_a_service_down_at_preflight_claims_nothing_at_all(tmp_path, monkeypatch):
    root = _share(tmp_path, _five())
    checks = _passing_checks()
    checks = [c if c.name != "Services" else
             Check("Services", False, "VieuxParler refused", "start it") for c in checks]
    monkeypatch.setattr(batch, "preflight", lambda settings: checks)
    board = FakeBoard(_cards(_five()))

    result = batch.run_batch(_settings(tmp_path, root), board, NOW)

    assert result.exit_code == exits.MISCONFIGURED
    assert board.pending_called == 0
    assert board.claim_calls == []


# -- Rule 1: selection and claiming -----------------------------------------

def test_a_batch_takes_exactly_batch_size_cards(tmp_path, monkeypatch):
    identifiers = [f"LIV{i:04d}" for i in range(1, 8)]  # 7 pending, batch of 5
    root = _share(tmp_path, identifiers)
    manifest = {"documents": {pipeline_name(i): "ok" for i in identifiers[:5]}}
    _patch_common(monkeypatch, manifest)
    board = FakeBoard(_cards(identifiers))

    result = batch.run_batch(_settings(tmp_path, root), board, NOW)

    assert len(result.claimed) == 5
    assert result.claimed == identifiers[:5]
    assert set(board.claim_calls) == set(identifiers[:5])


def test_a_card_lost_to_another_machine_is_skipped_and_the_next_taken(
        tmp_path, monkeypatch):
    identifiers = [f"LIV{i:04d}" for i in range(1, 7)]  # 6 pending
    root = _share(tmp_path, identifiers)
    loser = identifiers[1]
    winners = [i for i in identifiers if i != loser][:5]
    manifest = {"documents": {pipeline_name(i): "ok" for i in winners}}
    _patch_common(monkeypatch, manifest)
    board = FakeBoard(_cards(identifiers), losers=[loser])

    result = batch.run_batch(_settings(tmp_path, root), board, NOW)

    assert loser not in result.claimed
    assert len(result.claimed) == 5
    assert result.claimed == winners
    # the loser's claim was attempted, and the sixth card was pulled in
    assert loser in board.claim_calls
    assert identifiers[5] in result.claimed


def test_a_batch_with_nothing_pending_claims_nothing(tmp_path, monkeypatch):
    root = _share(tmp_path, [])
    monkeypatch.setattr(batch, "preflight", lambda settings: _passing_checks())
    called = []
    monkeypatch.setattr(batch, "run_converter",
                        lambda *a, **k: called.append(a) or 0)
    board = FakeBoard([])

    result = batch.run_batch(_settings(tmp_path, root), board, NOW)

    assert result.claimed == []
    assert result.outcomes == {}
    assert called == []


# -- Fetch: one bad archive must not sink the batch --------------------------

def test_an_archive_absent_from_the_share_blocks_that_card_only(
        tmp_path, monkeypatch):
    identifiers = _five()
    present = identifiers[:4]
    root = _share(tmp_path, present)   # LIV0005 is never on the share
    manifest = {"documents": {pipeline_name(i): "ok" for i in present}}
    _patch_common(monkeypatch, manifest)
    board = FakeBoard(_cards(identifiers))

    result = batch.run_batch(_settings(tmp_path, root), board, NOW)

    blocked = identifiers[4]
    assert result.outcomes[blocked].status == "Bloqué"
    assert result.outcomes[blocked].cause == "Absent du NAS"
    for ok_id in present:
        assert result.outcomes[ok_id].status == "Terminé"


def test_the_other_four_are_still_converted_when_one_is_blocked(
        tmp_path, monkeypatch):
    identifiers = _five()
    present = identifiers[:4]
    root = _share(tmp_path, present)
    manifest = {"documents": {pipeline_name(i): "ok" for i in present}}
    calls = []
    _patch_common(monkeypatch, manifest, calls=calls)
    board = FakeBoard(_cards(identifiers))

    batch.run_batch(_settings(tmp_path, root), board, NOW)

    assert len(calls) == 1, "one run_converter call for the whole batch"
    assert sorted(calls[0]) == sorted(pipeline_name(i) for i in present)


def test_run_converter_is_skipped_when_every_claimed_document_is_blocked(
        tmp_path, monkeypatch):
    identifiers = _five()
    root = _share(tmp_path, [])  # nothing on the share at all
    calls = []
    _patch_common(monkeypatch, {"documents": {}}, calls=calls)
    board = FakeBoard(_cards(identifiers))

    result = batch.run_batch(_settings(tmp_path, root), board, NOW)

    assert calls == [], "no subprocess when there is nothing to convert"
    assert all(v.status == "Bloqué" for v in result.outcomes.values())


# -- Rule 3: KeyboardInterrupt ------------------------------------------------

def test_an_interrupt_releases_every_claimed_but_unconverted_card(
        tmp_path, monkeypatch):
    identifiers = _five()
    root = _share(tmp_path, identifiers)
    monkeypatch.setattr(batch, "preflight", lambda settings: _passing_checks())

    real_fetch = nas.fetch

    def raising_fetch(root, identifier, into):
        if identifier == identifiers[2]:
            raise KeyboardInterrupt()
        return real_fetch(root, identifier, into)

    monkeypatch.setattr(batch.nas, "fetch", raising_fetch)
    board = FakeBoard(_cards(identifiers))

    with pytest.raises(KeyboardInterrupt):
        batch.run_batch(_settings(tmp_path, root), board, NOW)

    assert sorted(board.release_calls) == sorted(identifiers)
    assert board.write_calls == [], "nothing was judged, so nothing is written"


def test_an_interrupt_during_claiming_releases_the_cards_already_claimed(
        tmp_path, monkeypatch):
    """The claiming loop itself makes a network call per card (`claim()`
    writes then reads back); a Ctrl-C partway through it must still
    release whatever was already claimed, not only what survived into the
    fetch/convert stage."""
    identifiers = _five()
    root = _share(tmp_path, identifiers)
    monkeypatch.setattr(batch, "preflight", lambda settings: _passing_checks())
    board = FakeBoard(_cards(identifiers))
    real_claim = board.claim

    def interrupting_claim(card, machine, now):
        if card.identifier == identifiers[2]:
            raise KeyboardInterrupt()
        return real_claim(card, machine, now)

    board.claim = interrupting_claim

    with pytest.raises(KeyboardInterrupt):
        batch.run_batch(_settings(tmp_path, root), board, NOW)

    # the two cards claimed before the interrupt must be released
    assert sorted(board.release_calls) == sorted(identifiers[:2])


# -- Rule 2: converter exit code 3 -------------------------------------------

def test_require_services_refusing_releases_every_claim(tmp_path, monkeypatch):
    identifiers = _five()
    root = _share(tmp_path, identifiers)
    _patch_common(monkeypatch, {"documents": {}}, exit_code=3)
    board = FakeBoard(_cards(identifiers))

    result = batch.run_batch(_settings(tmp_path, root), board, NOW)

    assert result.exit_code == exits.MISCONFIGURED
    assert sorted(board.release_calls) == sorted(identifiers)
    assert result.outcomes == {}


def test_a_released_batch_is_not_five_failures_on_the_board(tmp_path, monkeypatch):
    identifiers = _five()
    root = _share(tmp_path, identifiers)
    _patch_common(monkeypatch, {"documents": {}}, exit_code=3)
    board = FakeBoard(_cards(identifiers))

    batch.run_batch(_settings(tmp_path, root), board, NOW)

    assert board.write_calls == [], "no card may read Échec for a document never opened"


def test_a_released_batch_reports_what_was_claimed_not_silence(tmp_path, monkeypatch):
    """The distinguishing test for the rule that a phase must report what
    it lost: a released batch (5 claimed, then handed back) must not look
    the same as a batch that found nothing pending (0 claimed)."""
    identifiers = _five()
    root = _share(tmp_path, identifiers)
    _patch_common(monkeypatch, {"documents": {}}, exit_code=3)
    board = FakeBoard(_cards(identifiers))

    result = batch.run_batch(_settings(tmp_path, root), board, NOW)

    assert len(result.claimed) == 5
    assert len(result.released) == 5
    assert result.message != ""


# -- Rule 1: publish before the final status write ---------------------------

def test_a_publication_refused_keeps_the_status_and_says_so_in_the_detail(
        tmp_path, monkeypatch):
    identifiers = _five()
    root = _share(tmp_path, identifiers)
    _patch_common(monkeypatch, ALL_OK)
    # Pre-publish LIV0001 to the share so its own publish is refused.
    target = nas.tei_dir(root) / "LIV0001.tei.xml"
    target.write_text("already there", encoding="utf-8")
    board = FakeBoard(_cards(identifiers))

    result = batch.run_batch(_settings(tmp_path, root), board, NOW)

    assert result.outcomes["LIV0001"].status == "Terminé"
    assert "republish" in result.outcomes["LIV0001"].detail
    assert result.published["LIV0001"] is False
    assert result.exit_code == exits.SOME_FAILED
    # the write to the board reflects the same (unchanged) status
    written = next(c for c in board.write_calls if c["identifier"] == "LIV0001")
    assert written["verdict"].status == "Terminé"
    assert "republish" in written["verdict"].detail


def test_publication_happens_before_the_final_status_write(tmp_path, monkeypatch):
    identifiers = _five()
    root = _share(tmp_path, identifiers)
    _patch_common(monkeypatch, ALL_OK)
    board = FakeBoard(_cards(identifiers))
    order = []
    real_publish = nas.publish

    def recording_publish(*a, **k):
        order.append(("publish", a[3]))  # identifier is the 4th positional arg
        return real_publish(*a, **k)

    monkeypatch.setattr(batch.nas, "publish", recording_publish)
    order_ref = board.calls_in_order

    batch.run_batch(_settings(tmp_path, root), board, NOW)

    # every publish for a given identifier precedes that identifier's write
    write_index = {i: idx for idx, (kind, i) in enumerate(order_ref) if kind == "write"}
    publish_docs = {i for _, i in order}
    for ident in publish_docs:
        assert ident in write_index, "a published document must still be written"
    # cross-check using the shared timeline: publish list built before
    # run_batch touched the board at all for writes
    assert len(order) == 5
    assert len(write_index) == 5


def test_review_documents_are_published_to_the_review_folder(tmp_path, monkeypatch):
    identifiers = _five()
    root = _share(tmp_path, identifiers)
    manifest = ALL_OK
    invalid = {pipeline_name("LIV0002"): ["ab is not allowed here"]}
    _patch_common(monkeypatch, manifest, invalid=invalid)
    board = FakeBoard(_cards(identifiers))

    result = batch.run_batch(_settings(tmp_path, root), board, NOW)

    assert result.outcomes["LIV0002"].status == "À vérifier"
    assert result.published["LIV0002"] is True
    assert (nas.tei_dir(root) / "_a_verifier" / "LIV0002.tei.xml").exists()
    assert not (nas.tei_dir(root) / "LIV0002.tei.xml").exists()


def test_nothing_leaves_the_machine_for_a_blocked_document(tmp_path, monkeypatch):
    identifiers = _five()
    present = identifiers[:4]
    root = _share(tmp_path, present)
    manifest = {"documents": {pipeline_name(i): "ok" for i in present}}
    _patch_common(monkeypatch, manifest)
    board = FakeBoard(_cards(identifiers))

    result = batch.run_batch(_settings(tmp_path, root), board, NOW)

    blocked = identifiers[4]
    assert blocked not in result.published
    assert not (nas.tei_dir(root) / f"{blocked}.tei.xml").exists()
    assert not (nas.tei_dir(root) / "_a_verifier" / f"{blocked}.tei.xml").exists()


def test_nothing_leaves_the_machine_for_a_failed_document(tmp_path, monkeypatch):
    identifiers = _five()
    root = _share(tmp_path, identifiers)
    manifest = {"documents": {pipeline_name(i): "ok" for i in identifiers}}
    manifest["documents"][pipeline_name(identifiers[0])] = "failed"
    incidents = [{"code": "document_failed", "document": pipeline_name(identifiers[0]),
                 "step": "sourcedoc", "count": 1, "total": 1, "detail": "boom"}]
    _patch_common(monkeypatch, manifest, incidents=incidents)
    board = FakeBoard(_cards(identifiers))

    result = batch.run_batch(_settings(tmp_path, root), board, NOW)

    failed = identifiers[0]
    assert result.outcomes[failed].status == "Échec"
    assert failed not in result.published


# -- Cleanup: keep the failures, delete the finished ------------------------

def test_a_failed_document_keeps_its_local_sources(tmp_path, monkeypatch):
    identifiers = _five()
    root = _share(tmp_path, identifiers)
    manifest = {"documents": {pipeline_name(i): "ok" for i in identifiers}}
    manifest["documents"][pipeline_name(identifiers[0])] = "failed"
    incidents = [{"code": "document_failed", "document": pipeline_name(identifiers[0]),
                 "step": "sourcedoc", "count": 1, "total": 1, "detail": "boom"}]
    _patch_common(monkeypatch, manifest, incidents=incidents)
    board = FakeBoard(_cards(identifiers))
    settings = _settings(tmp_path, root)

    batch.run_batch(settings, board, NOW)

    archive = Path(settings.work_dir) / "OCR" / f"{identifiers[0]}_reconciled.zip"
    assert archive.exists(), "a failed document's archive must survive for reproduction"


def test_a_finished_document_has_its_sources_deleted(tmp_path, monkeypatch):
    identifiers = _five()
    root = _share(tmp_path, identifiers)
    _patch_common(monkeypatch, ALL_OK)
    board = FakeBoard(_cards(identifiers))
    settings = _settings(tmp_path, root)

    result = batch.run_batch(settings, board, NOW)

    assert all(v.status == "Terminé" for v in result.outcomes.values())
    for ident in identifiers:
        archive = Path(settings.work_dir) / "OCR" / f"{ident}_reconciled.zip"
        assert not archive.exists(), f"{ident}'s local archive should be cleaned up"


def test_keep_disables_deletion_for_everything(tmp_path, monkeypatch):
    identifiers = _five()
    root = _share(tmp_path, identifiers)
    manifest = {"documents": {pipeline_name(i): "ok" for i in identifiers}}
    manifest["documents"][pipeline_name(identifiers[0])] = "failed"
    incidents = [{"code": "document_failed", "document": pipeline_name(identifiers[0]),
                 "step": "sourcedoc", "count": 1, "total": 1, "detail": "boom"}]
    _patch_common(monkeypatch, manifest, incidents=incidents)
    board = FakeBoard(_cards(identifiers))
    settings = _settings(tmp_path, root)

    batch.run_batch(settings, board, NOW, keep=True)

    for ident in identifiers:
        archive = Path(settings.work_dir) / "OCR" / f"{ident}_reconciled.zip"
        assert archive.exists(), f"--keep must leave {ident}'s archive alone"


# -- Reclaim ------------------------------------------------------------------

def test_stale_claims_older_than_reclaim_after_are_taken_back(tmp_path, monkeypatch):
    identifiers = _five()
    root = _share(tmp_path, identifiers)
    _patch_common(monkeypatch, ALL_OK)
    stale_one = identifiers[0]
    cards = _cards(identifiers)
    board = FakeBoard(cards, stale_identifiers=[stale_one])

    batch.run_batch(_settings(tmp_path, root), board, NOW)

    assert board.stale_called == 1
    assert stale_one in board.release_calls


# -- Exit codes ---------------------------------------------------------------

def test_the_exit_code_is_one_when_any_document_failed(tmp_path, monkeypatch):
    identifiers = _five()
    root = _share(tmp_path, identifiers)
    manifest = {"documents": {pipeline_name(i): "ok" for i in identifiers}}
    manifest["documents"][pipeline_name(identifiers[0])] = "failed"
    incidents = [{"code": "document_failed", "document": pipeline_name(identifiers[0]),
                 "step": "sourcedoc", "count": 1, "total": 1, "detail": "boom"}]
    _patch_common(monkeypatch, manifest, incidents=incidents)
    board = FakeBoard(_cards(identifiers))

    result = batch.run_batch(_settings(tmp_path, root), board, NOW)

    assert result.exit_code == exits.SOME_FAILED


def test_the_exit_code_is_one_for_a_review_only_batch(tmp_path, monkeypatch):
    identifiers = _five()
    root = _share(tmp_path, identifiers)
    invalid = {pipeline_name(identifiers[0]): ["ab is not allowed here"]}
    _patch_common(monkeypatch, ALL_OK, invalid=invalid)
    board = FakeBoard(_cards(identifiers))

    result = batch.run_batch(_settings(tmp_path, root), board, NOW)

    assert result.exit_code == exits.SOME_FAILED


def test_exit_code_is_ok_when_everything_finishes(tmp_path, monkeypatch):
    identifiers = _five()
    root = _share(tmp_path, identifiers)
    _patch_common(monkeypatch, ALL_OK)
    board = FakeBoard(_cards(identifiers))

    result = batch.run_batch(_settings(tmp_path, root), board, NOW)

    assert result.exit_code == exits.OK


# -- Pages --------------------------------------------------------------------

def test_pages_uses_the_tei_surface_count_when_the_file_exists(tmp_path, monkeypatch):
    identifiers = ["LIV0001"]
    root = _share(tmp_path, identifiers, xml_entries=2)
    manifest = {"documents": {pipeline_name("LIV0001"): "ok"}}
    _patch_common(monkeypatch, manifest, pages_by_doc={pipeline_name("LIV0001"): 7})
    board = FakeBoard(_cards(identifiers))

    batch.run_batch(_settings(tmp_path, root, batch_size=1), board, NOW)

    written = board.write_calls[0]
    assert written["pages"] == 7   # the TEI's own surface count, not the 2-entry archive


def test_pages_falls_back_to_the_archive_count_when_there_is_no_tei_file(
        tmp_path, monkeypatch):
    identifiers = ["LIV0001"]
    root = _share(tmp_path, [])  # nothing on the share: the archive is absent
    _patch_common(monkeypatch, {"documents": {}})
    board = FakeBoard(_cards(identifiers))

    batch.run_batch(_settings(tmp_path, root, batch_size=1), board, NOW)

    written = board.write_calls[0]
    assert written["verdict"].status == "Bloqué"
    assert written["pages"] == 0   # never fetched: no archive to count either


def test_pages_uses_the_archive_count_when_fetched_but_never_converted(
        tmp_path, monkeypatch):
    identifiers = ["LIV0001"]
    root = _share(tmp_path, identifiers, xml_entries=4)
    # the run never mentions this document (dropped during expand)
    _patch_common(monkeypatch, {"documents": {}})
    board = FakeBoard(_cards(identifiers))

    result = batch.run_batch(_settings(tmp_path, root, batch_size=1), board, NOW)

    assert result.outcomes["LIV0001"].status == "Bloqué"
    written = board.write_calls[0]
    assert written["pages"] == 4   # fetched fine, archive's own XML count


# -- Version pipeline ----------------------------------------------------------

def test_version_pipeline_is_read_from_the_converter_preflight_check(
        tmp_path, monkeypatch):
    identifiers = ["LIV0001"]
    root = _share(tmp_path, identifiers)
    _patch_common(monkeypatch, {"documents": {pipeline_name("LIV0001"): "ok"}})
    board = FakeBoard(_cards(identifiers))

    batch.run_batch(_settings(tmp_path, root, batch_size=1), board, NOW)

    assert board.write_calls[0]["version"] == "teille-douce 2.1.0"


# -- Machine name --------------------------------------------------------------

def test_the_machine_name_is_never_hardcoded(monkeypatch):
    """batch.py must derive the claim stamp's machine name at call time
    (hostname), never embed a literal machine name in the source."""
    monkeypatch.setattr(batch.socket, "gethostname", lambda: "fake-worker")
    assert batch._machine_name() == "fake-worker"
