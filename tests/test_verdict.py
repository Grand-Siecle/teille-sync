import pytest
from teille_sync.verdict import decide, CAUSE_BY_CODE, PHASE_BY_STEP


def v(doc="LIV0001_reconciled", manifest=None, incidents=(), validation=None,
      fetched=True):
    return decide(doc, manifest or {"documents": {}}, list(incidents),
                  validation, fetched)


def test_never_fetched_is_blocked_not_failed():
    r = v(fetched=False)
    assert r.status == "Bloqué"
    assert r.cause == "Absent du NAS"


def test_absent_from_the_manifest_is_blocked():
    # The run happened but never saw this document: expansion dropped it.
    r = v(manifest={"documents": {"LIV0002_reconciled": "ok"}})
    assert r.status == "Bloqué"


def test_failed_takes_its_cause_and_phase_from_the_incident():
    r = v(manifest={"documents": {"LIV0001_reconciled": "failed"}},
          incidents=[{"code": "document_failed", "document": "LIV0001_reconciled",
                      "step": "sourcedoc", "count": 1, "total": 1,
                      "detail": "lxml: premature end of data"}])
    assert r.status == "Échec"
    assert r.cause == "Document en échec"
    assert r.phase == "sourceDoc"
    assert "premature end" in r.detail


def test_ok_and_clean_and_valid_is_finished():
    r = v(manifest={"documents": {"LIV0001_reconciled": "ok"}},
          validation={"valid": True})
    assert r.status == "Terminé"
    assert r.losses == 0
    assert r.cause is None


def test_ok_but_invalid_needs_a_look_and_names_the_schema():
    r = v(manifest={"documents": {"LIV0001_reconciled": "ok"}},
          validation={"valid": False, "errors": ["ab is not allowed here"]})
    assert r.status == "À vérifier"
    assert r.cause == "Validation schéma"
    assert "ab is not allowed" in r.detail


def test_a_lost_phase_is_a_failure_even_though_the_document_converted():
    # A service that died between the probe and this document: the file
    # was written, recorded "ok", and has none of that phase's output.
    # This corpus wants all three phases, so that is an error.
    r = v(manifest={"documents": {"LIV0001_reconciled": "ok"}},
          validation={"valid": True},
          incidents=[{"code": "phase_lost", "document": "LIV0001_reconciled",
                      "step": "modernize", "count": 120, "total": 120,
                      "detail": "VieuxParler stopped answering"}])
    assert r.status == "Échec"
    assert r.cause == "Phase perdue"
    assert r.phase == "Modernisation"
    assert r.losses == 120


def test_a_lost_phase_outranks_a_schema_failure_in_the_detail():
    r = v(manifest={"documents": {"LIV0001_reconciled": "ok"}},
          validation={"valid": False, "errors": ["ab is not allowed here"]},
          incidents=[{"code": "phase_lost", "document": "LIV0001_reconciled",
                      "step": "enrich", "count": 9, "total": 9,
                      "detail": "PyHellen stopped answering"}])
    assert r.status == "Échec"
    assert "PyHellen" in r.detail


def test_ok_with_incidents_counts_every_one_of_them():
    r = v(manifest={"documents": {"LIV0001_reconciled": "ok"}},
          validation={"valid": True},
          incidents=[{"code": "page_unusable", "document": "LIV0001_reconciled",
                      "step": "sourcedoc", "count": 3, "total": 40, "detail": ""},
                     {"code": "container_failed", "document": "LIV0001_reconciled",
                      "step": "sourcedoc", "count": 2, "total": 99, "detail": ""}])
    assert r.status == "À vérifier"
    assert r.losses == 5


def test_incidents_of_another_document_are_not_counted_here():
    r = v(manifest={"documents": {"LIV0001_reconciled": "ok"}},
          validation={"valid": True},
          incidents=[{"code": "page_unusable", "document": "LIV0999_reconciled",
                      "step": "sourcedoc", "count": 7, "total": 40, "detail": ""}])
    assert r.status == "Terminé"
    assert r.losses == 0


def test_an_unknown_code_falls_back_rather_than_crashing():
    r = v(manifest={"documents": {"LIV0001_reconciled": "failed"}},
          incidents=[{"code": "something_new", "document": "LIV0001_reconciled",
                      "step": "run", "count": 1, "total": 1, "detail": "?"}])
    assert r.cause == "Autre"


def test_a_step_the_board_has_no_option_for_leaves_phase_empty():
    # The pipeline emits `run` for document-level failures; the board has
    # no such phase, and guessing one would be a lie.
    r = v(manifest={"documents": {"LIV0001_reconciled": "failed"}},
          incidents=[{"code": "document_failed", "document": "LIV0001_reconciled",
                      "step": "run", "count": 1, "total": 1, "detail": "boom"}])
    assert r.phase is None


def test_every_pipeline_code_has_a_board_label():
    for code in ("archive_corrupt", "volume_unreadable", "page_unusable",
                 "container_failed", "phase_lost", "batch_failed",
                 "retry_unanswered", "breaker_skipped", "document_failed"):
        assert code in CAUSE_BY_CODE, code
