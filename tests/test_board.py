from datetime import datetime, timedelta
import pytest
from teille_sync.board import Board, Card
from teille_sync.verdict import Verdict

IDS = {
    "project": {"id": "PVT_test"},
    "fields": {
        "Status": {"id": "F_status", "dataType": "SINGLE_SELECT",
                   "options": {"À traiter": "o_todo", "En cours": "o_wip",
                               "Bloqué": "o_blocked", "Échec": "o_failed",
                               "À vérifier": "o_review", "Terminé": "o_done"}},
        "Phase": {"id": "F_phase", "dataType": "SINGLE_SELECT",
                  "options": {"sourceDoc": "o_sd", "Décompression": "o_exp"}},
        "Cause": {"id": "F_cause", "dataType": "SINGLE_SELECT",
                  "options": {"Absent du NAS": "o_nas", "Document en échec": "o_docfail"}},
        "Détail": {"id": "F_detail", "dataType": "TEXT"},
        "Pertes": {"id": "F_losses", "dataType": "NUMBER"},
        "Pages": {"id": "F_pages", "dataType": "NUMBER"},
        "Date de traitement": {"id": "F_date", "dataType": "DATE"},
        "Version pipeline": {"id": "F_version", "dataType": "TEXT"},
    },
    "items": {"LIV0001": "I_1", "LIV0002": "I_2"},
}
NOW = datetime(2026, 9, 12, 14, 30, 0)


class Recorder:
    """A transport that answers from a script and records what it was asked."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, query, variables):
        self.calls.append((query, variables))
        return self.answers.pop(0) if self.answers else {}


def test_claim_writes_status_then_detail_and_reads_back(monkeypatch):
    card = Card(identifier="LIV0001", item_id="I_1", status="À traiter", detail="")
    t = Recorder([{}, {}, {"node": {"fieldValues": {"nodes": [
        {"text": "thinkpad · 2026-09-12T14:30:00",
         "field": {"name": "Détail"}}]}}}])
    board = Board(IDS, t)
    assert board.claim(card, machine="thinkpad", now=NOW) is True
    # status, detail, read-back
    assert len(t.calls) == 3
    assert t.calls[0][1]["value"] == {"singleSelectOptionId": "o_wip"}


def test_a_claim_another_machine_won_is_not_ours(monkeypatch):
    card = Card(identifier="LIV0001", item_id="I_1", status="À traiter", detail="")
    t = Recorder([{}, {}, {"node": {"fieldValues": {"nodes": [
        {"text": "desktop · 2026-09-12T14:29:58",
         "field": {"name": "Détail"}}]}}}])
    board = Board(IDS, t)
    assert board.claim(card, machine="thinkpad", now=NOW) is False


@pytest.mark.parametrize("malformed_answer", [
    {},
    {"node": None},
    {"node": {}},
    {"node": {"fieldValues": {"nodes": [
        {"text": "thinkpad · 2026-09-12T14:30:00",
         "field": {"name": "Pertes"}}]}}},
], ids=["empty_answer", "null_node", "empty_node", "no_detail_field"])
def test_claim_read_back_never_raises_or_returns_true_on_a_malformed_answer(malformed_answer):
    # A GraphQL answer missing the shape claim() expects — the request
    # was rejected, the item vanished, the schema changed underneath us —
    # must read as "not ours", never crash and never claim the card by
    # accident. Returning True here would mean two machines convert the
    # same volume, which is worse than any exception.
    card = Card(identifier="LIV0001", item_id="I_1", status="À traiter", detail="")
    t = Recorder([{}, {}, malformed_answer])
    board = Board(IDS, t)
    assert board.claim(card, machine="thinkpad", now=NOW) is False


def test_write_sets_every_field_a_verdict_carries():
    card = Card(identifier="LIV0001", item_id="I_1", status="En cours", detail="x")
    t = Recorder([{}] * 8)
    board = Board(IDS, t)
    board.write(card, Verdict("Échec", cause="Document en échec",
                              phase="sourceDoc", detail="boom", losses=3),
                pages=40, version="teille-douce 2.0.0", now=NOW)
    sent = [c[1] for c in t.calls]
    assert {"singleSelectOptionId": "o_failed"} in [s.get("value") for s in sent]
    assert {"singleSelectOptionId": "o_docfail"} in [s.get("value") for s in sent]
    assert {"text": "boom"} in [s.get("value") for s in sent]
    assert {"number": 3} in [s.get("value") for s in sent]
    assert {"date": "2026-09-12"} in [s.get("value") for s in sent]


def test_write_skips_a_phase_the_board_has_no_option_for():
    card = Card(identifier="LIV0001", item_id="I_1", status="En cours", detail="x")
    t = Recorder([{}] * 8)
    board = Board(IDS, t)
    board.write(card, Verdict("Échec", cause="Autre", phase="Écriture",
                              detail="d", losses=0),
                pages=0, version="v", now=NOW)
    # "Écriture" is not in the test ids: it must be skipped, not sent as null
    assert all(variables.get("f") != "F_phase" for _, variables in t.calls)


# -- a missing Status option is fatal, a missing Phase/Cause option is not --
#
# Skipping stays right for Phase and Cause: the pipeline legitimately emits
# steps and codes the board never modelled, and inventing an option would be
# a guess. Status is the opposite. `claim()` skipping `En cours` left the
# card reading `À traiter` while it was being converted — so a second
# machine claims the same document, which is the one thing claiming exists
# to prevent. And `write()` skipping the final status left the card `En
# cours` carrying a verdict in `Détail` that `_claim_age` cannot parse: a
# card nothing ever reclaims.

def _without_status_option(label):
    """The test ids, minus one Status option — a board whose option was
    renamed in the UI since the last `ids refresh`."""
    options = {name: oid for name, oid in IDS["fields"]["Status"]["options"].items()
               if name != label}
    fields = dict(IDS["fields"])
    fields["Status"] = {**IDS["fields"]["Status"], "options": options}
    return {**IDS, "fields": fields}


def test_claim_raises_rather_than_leave_a_card_reading_a_traiter():
    card = Card(identifier="LIV0001", item_id="I_1", status="À traiter", detail="")
    t = Recorder([{}] * 3)
    board = Board(_without_status_option("En cours"), t)

    with pytest.raises(KeyError) as excinfo:
        board.claim(card, machine="thinkpad", now=NOW)

    assert "En cours" in str(excinfo.value)
    assert "refresh" in str(excinfo.value)
    # And it raised *before* stamping Détail: a claim that half happened
    # is a claim another machine cannot see and this one cannot undo.
    assert t.calls == []


def test_release_raises_when_the_board_has_no_a_traiter_option():
    card = Card(identifier="LIV0001", item_id="I_1", status="En cours",
                detail="thinkpad · 2026-09-12T14:30:00")
    t = Recorder([{}] * 2)
    board = Board(_without_status_option("À traiter"), t)

    with pytest.raises(KeyError) as excinfo:
        board.release(card)

    assert "À traiter" in str(excinfo.value)
    assert t.calls == []


def test_write_raises_when_the_board_has_no_option_for_the_final_status():
    card = Card(identifier="LIV0001", item_id="I_1", status="En cours", detail="x")
    t = Recorder([{}] * 8)
    board = Board(_without_status_option("Terminé"), t)

    with pytest.raises(KeyError) as excinfo:
        board.write(card, Verdict("Terminé"), pages=1, version="v", now=NOW)

    assert "Terminé" in str(excinfo.value)
    assert "refresh" in str(excinfo.value)
    assert t.calls == [], "a card left En cours with a verdict in Détail is stuck"


def test_a_cause_the_board_never_modelled_is_still_skipped():
    """The other half of the rule, and the one that keeps the fix from
    turning every unmodelled pipeline code into a crashed batch."""
    card = Card(identifier="LIV0001", item_id="I_1", status="En cours", detail="x")
    t = Recorder([{}] * 8)
    board = Board(IDS, t)

    board.write(card, Verdict("Terminé", cause="Disjoncteur", phase="NER",
                              detail="d", losses=0),
                pages=1, version="v", now=NOW)

    assert all(variables.get("f") != "F_cause" for _, variables in t.calls)
    assert all(variables.get("f") != "F_phase" for _, variables in t.calls)
    # …and the Status it *does* have an option for still landed.
    assert {"singleSelectOptionId": "o_done"} in [v.get("value") for _, v in t.calls]


def test_release_puts_the_card_back_and_clears_the_claim():
    card = Card(identifier="LIV0001", item_id="I_1", status="En cours",
                detail="thinkpad · 2026-09-12T14:30:00")
    t = Recorder([{}, {}])
    board = Board(IDS, t)
    board.release(card)
    assert t.calls[0][1]["value"] == {"singleSelectOptionId": "o_todo"}
    assert t.calls[1][1]["value"] == {"text": ""}


def test_an_unknown_identifier_is_refused_rather_than_written_nowhere():
    board = Board(IDS, Recorder([]))
    with pytest.raises(KeyError):
        board.write(Card("LIV9999", "I_missing", "En cours", ""),
                    Verdict("Terminé"), pages=1, version="v", now=NOW)


def test_write_raises_when_the_board_has_no_field_for_something_it_must_write():
    # The symmetric half of "an unknown option is skipped": a missing
    # *field* means the id file is stale (someone recreated the field in
    # the UI, which mints a new id), and writing to a dead id would fail
    # silently or land nowhere. This must raise, unlike a missing option.
    ids = {
        "project": IDS["project"],
        "fields": {name: value for name, value in IDS["fields"].items()
                  if name != "Pertes"},
        "items": IDS["items"],
    }
    card = Card(identifier="LIV0001", item_id="I_1", status="En cours", detail="x")
    board = Board(ids, Recorder([{}] * 8))
    with pytest.raises(KeyError) as excinfo:
        board.write(card, Verdict("Terminé"), pages=1, version="v", now=NOW)
    assert "Pertes" in str(excinfo.value)
    assert "refresh" in str(excinfo.value)


# -- pending() and stale() -----------------------------------------------
#
# Both page through PENDING. `_node` and `_page` below build the answers
# the real API returns: an item node carries its title and a list of
# field values, each tagged with the name of the field it belongs to —
# exactly the shape the query's inline fragments produce for a
# single-select (Status) or a text field (Détail).

def _node(item_id, title, status=None, detail=None):
    values = []
    if status is not None:
        values.append({"name": status, "field": {"name": "Status"}})
    if detail is not None:
        values.append({"text": detail, "field": {"name": "Détail"}})
    return {"id": item_id, "content": {"title": title},
            "fieldValues": {"nodes": values}}


def _page(nodes, has_next=False, cursor=None):
    return {"node": {"items": {
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        "nodes": nodes}}}


def test_pending_returns_only_a_traiter_cards_in_title_order():
    # Deliberately out of title order in the answer, and mixing in a
    # status pending() must not return.
    nodes = [
        _node("I_2", "LIV0002", status="À traiter", detail=""),
        _node("I_3", "LIV0003", status="En cours", detail="thinkpad · x"),
        _node("I_1", "LIV0001", status="À traiter", detail=""),
    ]
    t = Recorder([_page(nodes)])
    board = Board(IDS, t)
    cards = board.pending()
    assert [c.identifier for c in cards] == ["LIV0001", "LIV0002"]
    assert all(c.status == "À traiter" for c in cards)


def test_pending_pages_through_the_whole_board():
    # A first page that stops here would hide LIV0003: it must not.
    page1 = _page([_node("I_1", "LIV0001", status="À traiter", detail="")],
                  has_next=True, cursor="CURSOR_1")
    page2 = _page([_node("I_3", "LIV0003", status="À traiter", detail="")],
                  has_next=False)
    t = Recorder([page1, page2])
    board = Board(IDS, t)
    cards = board.pending()
    assert [c.identifier for c in cards] == ["LIV0001", "LIV0003"]
    assert len(t.calls) == 2
    assert t.calls[0][1]["c"] is None
    assert t.calls[1][1]["c"] == "CURSOR_1"


def test_all_cards_returns_every_status_unfiltered():
    # Unlike pending() and stale(), all_cards() must not drop a card just
    # because its status is neither "À traiter" nor an aged "En cours" —
    # `teille-sync status` needs Terminé/Échec/À vérifier too, and
    # `teille-sync release` needs to find a card no matter its status.
    nodes = [
        _node("I_1", "LIV0001", status="À traiter", detail=""),
        _node("I_2", "LIV0002", status="Terminé", detail=""),
        _node("I_3", "LIV0003", status="Échec", detail=""),
    ]
    t = Recorder([_page(nodes)])
    board = Board(IDS, t)
    cards = board.all_cards()
    assert {c.identifier: c.status for c in cards} == {
        "LIV0001": "À traiter", "LIV0002": "Terminé", "LIV0003": "Échec"}


def test_all_cards_pages_through_the_whole_board():
    page1 = _page([_node("I_1", "LIV0001", status="Terminé", detail="")],
                  has_next=True, cursor="CURSOR_1")
    page2 = _page([_node("I_3", "LIV0003", status="Échec", detail="")],
                  has_next=False)
    t = Recorder([page1, page2])
    board = Board(IDS, t)
    cards = board.all_cards()
    assert {c.identifier for c in cards} == {"LIV0001", "LIV0003"}
    assert len(t.calls) == 2


def test_stale_takes_back_a_claim_older_than_the_threshold():
    old_stamp = "thinkpad · 2026-09-12T08:00:00"   # 6h30 before NOW
    fresh_stamp = "desktop · 2026-09-12T14:29:00"  # 1 minute before NOW
    nodes = [
        _node("I_1", "LIV0001", status="En cours", detail=old_stamp),
        _node("I_2", "LIV0002", status="En cours", detail=fresh_stamp),
    ]
    t = Recorder([_page(nodes)])
    board = Board(IDS, t)
    cards = board.stale(older_than=timedelta(hours=6), now=NOW)
    assert [c.identifier for c in cards] == ["LIV0001"]


def test_stale_excludes_a_claim_exactly_at_the_threshold():
    # Exactly 6h old is not yet "older than" 6h.
    boundary_stamp = "thinkpad · 2026-09-12T08:30:00"
    nodes = [_node("I_1", "LIV0001", status="En cours", detail=boundary_stamp)]
    t = Recorder([_page(nodes)])
    board = Board(IDS, t)
    assert board.stale(older_than=timedelta(hours=6), now=NOW) == []


def test_stale_ignores_cards_that_are_not_en_cours():
    old_stamp = "thinkpad · 2026-09-12T08:00:00"
    nodes = [_node("I_1", "LIV0001", status="À traiter", detail=old_stamp),
            _node("I_2", "LIV0002", status="Terminé", detail=old_stamp)]
    t = Recorder([_page(nodes)])
    board = Board(IDS, t)
    assert board.stale(older_than=timedelta(hours=6), now=NOW) == []


@pytest.mark.parametrize("detail", ["", "no separator here", "thinkpad · not-a-date"])
def test_stale_leaves_alone_an_en_cours_card_with_no_readable_stamp(detail):
    # An unreadable Détail is not evidence of abandonment: the claiming
    # machine may simply not have written its stamp in a shape this code
    # recognizes yet, or a person edited the field by hand. Treating it
    # as stale would hand the document to a second machine while a first
    # one is still converting it.
    nodes = [_node("I_1", "LIV0001", status="En cours", detail=detail)]
    t = Recorder([_page(nodes)])
    board = Board(IDS, t)
    assert board.stale(older_than=timedelta(hours=6), now=NOW) == []


def test_stale_excludes_a_future_dated_stamp_from_clock_skew():
    # Two machines with skewed clocks write stamps in each other's
    # future. A negative age (`now - claimed_at`) never exceeds a
    # positive `older_than`, so a clock-skewed stamp must not be
    # reclaimed as abandoned. The offset here (7h, past the 6h
    # threshold) is deliberately larger than `older_than`: a smaller one
    # would stay under the threshold either way and would not tell a
    # correct sign (`now - claimed_at`) apart from an accidentally
    # flipped one (`claimed_at - now`), which would read this same
    # future stamp as 7h stale and reclaim a card someone is actively
    # converting.
    future_stamp = "desktop · 2026-09-12T21:30:00"  # 7h after NOW
    nodes = [_node("I_1", "LIV0001", status="En cours", detail=future_stamp)]
    t = Recorder([_page(nodes)])
    board = Board(IDS, t)
    assert board.stale(older_than=timedelta(hours=6), now=NOW) == []


def test_stale_pages_through_the_whole_board():
    old_stamp = "thinkpad · 2026-09-12T08:00:00"
    page1 = _page([_node("I_1", "LIV0001", status="En cours", detail=old_stamp)],
                  has_next=True, cursor="CURSOR_1")
    page2 = _page([_node("I_3", "LIV0003", status="En cours", detail=old_stamp)],
                  has_next=False)
    t = Recorder([page1, page2])
    board = Board(IDS, t)
    cards = board.stale(older_than=timedelta(hours=6), now=NOW)
    assert [c.identifier for c in cards] == ["LIV0001", "LIV0003"]
    assert len(t.calls) == 2
    assert t.calls[1][1]["c"] == "CURSOR_1"
