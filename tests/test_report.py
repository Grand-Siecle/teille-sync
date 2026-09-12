"""report.py: Rich renderables, and nothing else.

Every function is exercised without a console: a `rich.table.Table`
stores its rows as plain data on each `Column` (`_cells`), so a test can
read exactly what would have been printed without capturing a terminal.
This is the point of the split described in `report.py`'s own module
docstring — only `cli.py` is allowed to touch a console or a clock.
"""

import inspect

from teille_sync import report
from teille_sync.batch import BatchResult
from teille_sync.board import Card
from teille_sync.preflight import Check
from teille_sync.verdict import Verdict


def _cells(table, column_index):
    return list(table.columns[column_index]._cells)


# -- the module itself is pure -------------------------------------------

def test_report_module_never_touches_a_console_or_a_clock():
    source = inspect.getsource(report)
    assert "Console(" not in source
    assert "print(" not in source
    assert "datetime.now" not in source
    assert "input(" not in source


# -- preflight_table -------------------------------------------------------

def test_preflight_table_has_one_row_per_check():
    checks = [Check("VPN", True, "the NAS answers", ""),
             Check("Disk", False, "12 MB free", "free up space")]
    table = report.preflight_table(checks)
    assert table.row_count == 2
    assert _cells(table, 0) == ["VPN", "Disk"]


def test_preflight_table_shows_detail_and_remedy_only_for_a_failure():
    checks = [Check("VPN", True, "the NAS answers", "unused remedy text"),
             Check("Disk", False, "12 MB free", "free up space")]
    table = report.preflight_table(checks)
    details = _cells(table, 2)
    remedies = _cells(table, 3)
    assert details == ["the NAS answers", "12 MB free"]
    # The passing check's remedy is never shown, even though Check itself
    # can carry one — a mutation that always prints `check.remedy` would
    # pass "no remedy for a real pass" fixtures but fail this one.
    assert remedies == ["", "free up space"]


def test_preflight_table_marks_pass_and_fail_differently():
    checks = [Check("VPN", True, "ok", ""), Check("Disk", False, "bad", "fix it")]
    table = report.preflight_table(checks)
    results = [str(cell) for cell in _cells(table, 1)]
    assert results[0] != results[1]
    assert "FAILED" in results[1]


# -- batch_table -------------------------------------------------------

def _result(outcomes=None, published=None, **over):
    defaults = dict(outcomes=outcomes or {}, published=published or {},
                    claimed=[], reclaimed=[], released=[], message="")
    defaults.update(over)
    return BatchResult(**defaults)


def test_batch_table_has_one_row_per_document_with_the_five_columns():
    outcomes = {
        "LIV0001": Verdict("Terminé", losses=0),
        "LIV0002": Verdict("À vérifier", cause="Pages perdues",
                           phase="sourceDoc", detail="d", losses=5),
    }
    published = {"LIV0001": True, "LIV0002": True}
    table = report.batch_table(_result(outcomes, published))
    assert table.row_count == 2
    assert _cells(table, 0) == ["LIV0001", "LIV0002"]
    assert _cells(table, 1) == ["Terminé", "À vérifier"]
    assert _cells(table, 2) == ["", "sourceDoc"]
    assert _cells(table, 3) == ["0", "5"]
    assert _cells(table, 4) == ["yes", "yes"]


def test_batch_table_marks_a_document_that_was_never_published():
    outcomes = {"LIV0001": Verdict("Échec", cause="Document en échec")}
    table = report.batch_table(_result(outcomes, published={}))
    # published.get() default: neither True nor False were recorded —
    # this document never reached the publish step at all (Bloqué/Échec).
    assert _cells(table, 4) == ["—"]


def test_batch_table_is_sorted_by_identifier_regardless_of_dict_order():
    outcomes = {"LIV0009": Verdict("Terminé"), "LIV0001": Verdict("Terminé")}
    table = report.batch_table(_result(outcomes, {"LIV0009": True, "LIV0001": True}))
    assert _cells(table, 0) == ["LIV0001", "LIV0009"]


def test_batch_table_with_no_outcomes_carries_a_caption_not_a_blank_table():
    # This is the exact scenario CLAUDE.md's split is guarding: five
    # cards claimed then released because a service died must not render
    # as an empty table indistinguishable from "nothing to do".
    table = report.batch_table(_result(outcomes={}, published={},
                                       claimed=["LIV0001"], released=["LIV0001"]))
    assert table.row_count == 0
    assert table.caption
    assert "summary" in table.caption.lower()


# -- batch_summary -------------------------------------------------------

def test_batch_summary_names_claimed_reclaimed_and_released():
    result = _result(claimed=["LIV0001", "LIV0002"], reclaimed=["LIV0009"],
                     released=["LIV0001", "LIV0002"],
                     message="the converter refused before writing anything")
    text = str(report.batch_summary(result))
    assert "claimed 2" in text
    assert "LIV0001" in text and "LIV0002" in text
    assert "reclaimed 1" in text
    assert "LIV0009" in text
    assert "released 2" in text
    assert "the converter refused before writing anything" in text


def test_batch_summary_when_nothing_was_claimed_still_says_zero():
    result = _result()
    text = str(report.batch_summary(result))
    assert "claimed 0" in text
    assert "reclaimed 0" in text
    assert "released 0" in text


def test_batch_summary_reports_outcome_counts_when_present():
    outcomes = {"LIV0001": Verdict("Terminé"), "LIV0002": Verdict("Terminé"),
               "LIV0003": Verdict("Échec")}
    text = str(report.batch_summary(_result(outcomes, claimed=list(outcomes))))
    assert "2 Terminé" in text
    assert "1 Échec" in text


def test_batch_summary_names_every_card_the_board_refused():
    """A document published to the share whose card was never written is
    the one loss only a person can close. It is named, not counted."""
    result = _result({"LIV0044": Verdict("Terminé")}, {"LIV0044": True},
                     claimed=["LIV0044"],
                     unwritten={"LIV0044": "the board did not answer"},
                     message="the board refused 1 verdict(s) (LIV0044)")
    text = str(report.batch_summary(result))
    assert "unwritten 1" in text
    assert "LIV0044" in text


def test_batch_summary_says_zero_unwritten_when_every_card_landed():
    text = str(report.batch_summary(_result(claimed=["LIV0001"])))
    assert "unwritten 0" in text


# -- dry_run_table -------------------------------------------------------

def test_dry_run_table_lists_the_candidate_identifiers_in_order():
    table = report.dry_run_table(["LIV0001", "LIV0002", "LIV0003"])
    assert _cells(table, 0) == ["LIV0001", "LIV0002", "LIV0003"]
    assert not table.caption


def test_dry_run_table_empty_gets_a_caption():
    table = report.dry_run_table([])
    assert table.row_count == 0
    assert table.caption


# -- status_table -------------------------------------------------------

def _card(identifier, status):
    return Card(identifier=identifier, item_id=f"I_{identifier}",
               status=status, detail="")


def test_status_table_counts_per_statut_and_a_total():
    cards = [_card("LIV0001", "À traiter"), _card("LIV0002", "À traiter"),
            _card("LIV0003", "Terminé")]
    table = report.status_table(cards)
    rows = dict(zip(_cells(table, 0), _cells(table, 1)))
    assert rows["À traiter"] == "2"
    assert rows["Terminé"] == "1"
    assert rows["Total"] == "3"


def test_status_table_follows_the_workflow_order_not_alphabetical():
    cards = [_card("LIV0001", "Terminé"), _card("LIV0002", "À traiter"),
            _card("LIV0003", "En cours")]
    table = report.status_table(cards)
    statuts = _cells(table, 0)
    assert statuts.index("À traiter") < statuts.index("En cours") < statuts.index("Terminé")


def test_status_table_keeps_an_unrecognized_status_rather_than_dropping_it():
    # A status this tool does not model (a field hand-edited to something
    # new) must still be counted somewhere — silently folding it away
    # would make the total wrong without any sign that it was.
    cards = [_card("LIV0001", "Archivé")]
    table = report.status_table(cards)
    rows = dict(zip(_cells(table, 0), _cells(table, 1)))
    assert rows["Archivé"] == "1"
    assert rows["Total"] == "1"


def test_status_table_total_matches_the_card_count_even_when_empty():
    table = report.status_table([])
    rows = dict(zip(_cells(table, 0), _cells(table, 1)))
    assert rows["Total"] == "0"


# -- release_summary / ids_refresh_summary ---------------------------------

def test_release_summary_lists_every_document():
    text = str(report.release_summary(["LIV0001", "LIV0002"]))
    assert "LIV0001" in text and "LIV0002" in text


def test_ids_refresh_summary_counts_fields_and_items_never_the_url():
    ids = {"project": {"id": "PVT_x"},
          "fields": {"Status": {}, "Cause": {}},
          "items": {"LIV0001": "I_1"}}
    text = str(report.ids_refresh_summary(ids, "ids.json"))
    assert "2 field" in text
    assert "1 item" in text
    assert "ids.json" in text
