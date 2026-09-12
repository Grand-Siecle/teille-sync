"""`scripts/realign_cause.py`: the one-off migration of the board's
`Cause` field.

The refusal path is the one that matters: `updateProjectV2Field`
replaces a single-select's options wholesale, so a script that ran this
after even one card had a `Cause` value would silently detach that card
from whatever it pointed to. Every test that exercises the happy path
here also has a paired test that puts the same evidence on the *second*
page — a check that stopped at the first page would clear a migration
that destroys data on card 150, which is exactly what the brief warns
against.

No network: `Board` is built directly, over a `Recorder` transport that
answers from a script, the same pattern `tests/test_board.py` uses.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import realign_cause  # noqa: E402

from teille_sync import cli, exits  # noqa: E402
from teille_sync.board import Board  # noqa: E402

IDS = {
    "project": {"id": "PVT_test"},
    "fields": {
        "Cause": {"id": "F_cause", "dataType": "SINGLE_SELECT",
                  "options": {"Absent du NAS": "o_old1", "Autre": "o_old2"}},
    },
    "items": {},
}


class Recorder:
    """A transport that answers from a script and records what it was asked."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, query, variables):
        self.calls.append((query, variables))
        return self.answers.pop(0) if self.answers else {}


def _node(item_id, title, cause=None):
    values = []
    if cause is not None:
        values.append({"name": cause, "field": {"name": "Cause"}})
    return {"id": item_id, "content": {"title": title},
            "fieldValues": {"nodes": values}}


def _page(nodes, has_next=False, cursor=None):
    return {"node": {"items": {
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        "nodes": nodes}}}


# -- CAUSES ------------------------------------------------------------

def test_causes_has_the_twelve_options_from_the_brief():
    assert len(realign_cause.CAUSES) == 12
    names = [name for name, _, _ in realign_cause.CAUSES]
    assert names == [
        "Absent du NAS", "ZIP illisible", "Volume illisible", "Pages perdues",
        "Conteneur en échec", "Phase perdue", "Lot refusé",
        "Relance sans réponse", "Disjoncteur", "Document en échec",
        "Validation schéma", "Autre",
    ]
    assert len(set(names)) == 12  # no duplicate label


def test_causes_only_use_colours_the_board_api_accepts():
    valid = {"GRAY", "BLUE", "GREEN", "YELLOW", "ORANGE", "RED", "PINK", "PURPLE"}
    assert all(color in valid for _, color, _ in realign_cause.CAUSES)


# -- cards_with_cause ----------------------------------------------------

def test_cards_with_cause_empty_when_no_card_has_one():
    nodes = [_node("I_1", "LIV0001"), _node("I_2", "LIV0002")]
    t = Recorder([_page(nodes)])
    board = Board(IDS, t)
    assert realign_cause.cards_with_cause(board) == []


def test_cards_with_cause_finds_one_on_the_first_page():
    nodes = [_node("I_1", "LIV0001", cause="Autre")]
    t = Recorder([_page(nodes)])
    board = Board(IDS, t)
    assert realign_cause.cards_with_cause(board) == ["LIV0001"]


def test_cards_with_cause_pages_through_the_whole_board():
    # A check that stopped after the first page would miss LIV0150 and
    # wave a destructive migration through.
    page1 = _page([_node("I_1", "LIV0001")], has_next=True, cursor="C1")
    page2 = _page([_node("I_150", "LIV0150", cause="Document en échec")])
    t = Recorder([page1, page2])
    board = Board(IDS, t)
    assert realign_cause.cards_with_cause(board) == ["LIV0150"]
    assert len(t.calls) == 2
    assert t.calls[0][1]["c"] is None
    assert t.calls[1][1]["c"] == "C1"


# -- realign ---------------------------------------------------------------

def test_realign_refuses_when_a_card_already_carries_a_cause():
    nodes = [_node("I_1", "LIV0001", cause="Autre")]
    t = Recorder([_page(nodes)])
    board = Board(IDS, t)
    with pytest.raises(realign_cause.CauseInUse) as excinfo:
        realign_cause.realign(board, check_only=False)
    assert "LIV0001" in str(excinfo.value)
    # No mutation was ever sent: the only call made was the page read.
    assert len(t.calls) == 1
    assert "updateProjectV2Field" not in t.calls[0][0]


def test_realign_check_only_refuses_too_and_writes_nothing():
    nodes = [_node("I_1", "LIV0001", cause="Autre")]
    t = Recorder([_page(nodes)])
    board = Board(IDS, t)
    with pytest.raises(realign_cause.CauseInUse):
        realign_cause.realign(board, check_only=True)
    assert len(t.calls) == 1


def test_realign_check_only_passes_silently_when_no_card_has_cause():
    t = Recorder([_page([_node("I_1", "LIV0001")])])
    board = Board(IDS, t)
    assert realign_cause.realign(board, check_only=True) is None
    assert len(t.calls) == 1  # inspection only, nothing written


def test_realign_refuses_even_when_the_only_offending_card_is_on_page_two():
    # The mirror of test_cards_with_cause_pages_through_the_whole_board,
    # at the level realign() itself is called from main(): this is the
    # test that would pass against a realign() that only checked the
    # first page.
    page1 = _page([_node("I_1", "LIV0001")], has_next=True, cursor="C1")
    page2 = _page([_node("I_150", "LIV0150", cause="Document en échec")])
    t = Recorder([page1, page2])
    board = Board(IDS, t)
    with pytest.raises(realign_cause.CauseInUse) as excinfo:
        realign_cause.realign(board, check_only=True)
    assert "LIV0150" in str(excinfo.value)
    assert len(t.calls) == 2


def test_realign_writes_the_twelve_options_when_none_in_use():
    nodes = [_node("I_1", "LIV0001")]
    mutation_answer = {"updateProjectV2Field": {"projectV2Field": {
        "id": "F_cause", "name": "Cause",
        "options": [{"id": f"o{i}", "name": n, "color": c, "description": d}
                   for i, (n, c, d) in enumerate(realign_cause.CAUSES)]}}}
    t = Recorder([_page(nodes), mutation_answer])
    board = Board(IDS, t)
    options = realign_cause.realign(board, check_only=False)

    assert [o["name"] for o in options] == [n for n, _, _ in realign_cause.CAUSES]
    assert len(t.calls) == 2
    query, variables = t.calls[-1]
    assert "updateProjectV2Field" in query
    assert variables["f"] == "F_cause"
    assert variables["options"] == [
        {"name": n, "color": c, "description": d} for n, c, d in realign_cause.CAUSES]


def test_realign_refuses_with_a_clear_message_when_the_board_has_no_cause_field():
    ids = {"project": IDS["project"], "fields": {}, "items": {}}
    t = Recorder([_page([_node("I_1", "LIV0001")])])
    board = Board(ids, t)
    with pytest.raises(KeyError) as excinfo:
        realign_cause.realign(board, check_only=False)
    assert "Cause" in str(excinfo.value)
    assert "refresh" in str(excinfo.value)
    # The field lookup failed before any mutation was attempted.
    assert len(t.calls) == 1


# -- main() -----------------------------------------------------------------

def test_main_check_refuses_and_exits_misconfigured(monkeypatch, capsys):
    nodes = [_node("I_1", "LIV0001", cause="Autre")]
    board = Board(IDS, Recorder([_page(nodes)]))
    monkeypatch.setattr(cli, "_open_board", lambda settings: board)

    code = realign_cause.main(["--check"])

    assert code == exits.MISCONFIGURED
    assert "LIV0001" in capsys.readouterr().err


def test_main_check_reports_safety_and_writes_nothing(monkeypatch, capsys):
    t = Recorder([_page([_node("I_1", "LIV0001")])])
    board = Board(IDS, t)
    monkeypatch.setattr(cli, "_open_board", lambda settings: board)

    code = realign_cause.main(["--check"])

    assert code == exits.OK
    assert len(t.calls) == 1  # inspection only
    assert "safe" in capsys.readouterr().out.lower()


def test_main_without_check_replaces_the_options_and_exits_ok(monkeypatch, capsys):
    mutation_answer = {"updateProjectV2Field": {"projectV2Field": {
        "options": [{"name": n} for n, _, _ in realign_cause.CAUSES]}}}
    t = Recorder([_page([_node("I_1", "LIV0001")]), mutation_answer])
    board = Board(IDS, t)
    monkeypatch.setattr(cli, "_open_board", lambda settings: board)

    code = realign_cause.main([])

    assert code == exits.OK
    assert "Autre" in capsys.readouterr().out


def test_main_reports_a_board_transport_error_as_misconfigured(monkeypatch, capsys):
    def _boom(settings):
        raise cli.BoardTransportError("no token")
    monkeypatch.setattr(cli, "_open_board", _boom)

    code = realign_cause.main(["--check"])

    assert code == exits.MISCONFIGURED
    assert "no token" in capsys.readouterr().err


def test_main_tells_the_operator_the_id_file_is_now_stale(monkeypatch, capsys):
    """`updateProjectV2Field` mints a fresh option id for every option it
    writes, so the moment this runs, `project-board-ids.json` describes a
    `Cause` field that no longer exists. `Board._select` skips a label it
    has no option for — silently, because Cause is not Status — so every
    `Cause` write for the whole corpus would land nowhere and the column
    would come out blank. The script has to say so where it cannot be
    missed."""
    mutation_answer = {"updateProjectV2Field": {"projectV2Field": {
        "options": [{"name": n} for n, _, _ in realign_cause.CAUSES]}}}
    board = Board(IDS, Recorder([_page([_node("I_1", "LIV0001")]),
                                mutation_answer]))
    monkeypatch.setattr(cli, "_open_board", lambda settings: board)

    realign_cause.main([])

    out = capsys.readouterr().out
    assert "teille-sync ids refresh" in out
    assert "stale" in out.lower() or "new id" in out.lower()


def test_the_check_run_does_not_claim_the_id_file_is_stale(monkeypatch, capsys):
    """`--check` writes nothing, so it mints no ids and the file is fine.
    Telling someone to rebuild it after a no-op teaches them to skip the
    line that matters."""
    board = Board(IDS, Recorder([_page([_node("I_1", "LIV0001")])]))
    monkeypatch.setattr(cli, "_open_board", lambda settings: board)

    realign_cause.main(["--check"])

    assert "ids refresh" not in capsys.readouterr().out
