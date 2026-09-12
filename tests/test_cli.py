"""cli.py: argument parsing, dispatch, and the console/clock it owns.

`report.py`'s renderables are trusted by `tests/test_report.py`; this file
is about wiring — which function gets called, with what, in what order,
and what `main()` returns. `preflight`, `run_batch` and `_open_board` are
monkeypatched everywhere except the handful of tests that exercise
`_open_board` and `_refresh_ids` themselves: no test here opens a socket
or a subprocess, and no real GitHub Projects id, host or URL appears
anywhere below (`EXAMPLE-ORG`, `nas.example.invalid`, `PVT_x`… are all
fictional, matching the convention `tests/test_board.py` and
`tests/test_preflight.py` already use).
"""

import json
from datetime import datetime, timedelta

import pytest

from teille_sync import cli, exits
from teille_sync.batch import BatchResult
from teille_sync.board import TODO, WIP, Board, Card, StaleIdFile
from teille_sync.preflight import Check
from teille_sync.settings import Settings
from teille_sync.verdict import DONE

NOW = datetime(2026, 9, 12, 14, 30, 0)


# -- fixtures ---------------------------------------------------------------

def _settings(tmp_path, **over):
    values = {
        "nas_root": tmp_path / "share", "nas_host": "nas.example.invalid",
        "batch_size": 5, "reclaim_after": timedelta(hours=6),
        "project_url": "https://github.com/orgs/EXAMPLE-ORG/projects/1",
        "ids_file": tmp_path / "ids.json", "work_dir": tmp_path / "work",
        "metadata_csv": tmp_path / "metadata_livre.csv",
        "persons_csv": tmp_path / "metadata_personne.csv",
        "entities_dir": tmp_path / "entities",
    }
    values.update(over)
    return Settings(values=values, origins={k: "default" for k in values},
                    refusals=[])


def _passing_checks():
    return [Check(n, True, f"{n} ok", "") for n in
            ["VPN", "NAS root", "Destination", "Board", "Converter",
             "Services", "Metadata", "Disk"]]


def _cards(identifiers, status=TODO):
    return [Card(identifier=i, item_id=f"I_{i}", status=status, detail="")
           for i in identifiers]


class FakeBoard:
    """The five real `Board` methods plus `all_cards()`, all recorded.
    `pending_answers`, given, makes `pending()` return one scripted list
    per call (and `[]` once exhausted) — how the "--until-done stops when
    pending() is empty" tests script a shrinking board without a second
    layer of GraphQL fakery."""

    def __init__(self, cards=(), pending_answers=None):
        self.cards = {c.identifier: c for c in cards}
        self._pending_answers = list(pending_answers) if pending_answers else None
        self.pending_calls = 0
        self.claim_calls = []
        self.release_calls = []
        self.write_calls = []

    def pending(self):
        self.pending_calls += 1
        if self._pending_answers is not None:
            return self._pending_answers.pop(0) if self._pending_answers else []
        return sorted((c for c in self.cards.values() if c.status == TODO),
                     key=lambda c: c.identifier)

    def stale(self, older_than, now):
        return []

    def claim(self, card, machine, now):
        self.claim_calls.append(card.identifier)
        return True

    def release(self, card):
        self.release_calls.append(card.identifier)

    def write(self, card, verdict, pages, version, now):
        self.write_calls.append(card.identifier)

    def all_cards(self):
        return list(self.cards.values())


def _boom(*args, **kwargs):
    raise AssertionError(f"must not be called, got args={args} kwargs={kwargs}")


def _stub_settings(monkeypatch, settings):
    monkeypatch.setattr(cli, "resolve", lambda flags, config_file=None: settings)


# -- check --------------------------------------------------------------

def test_check_exits_ok_when_every_check_passes(monkeypatch, tmp_path):
    _stub_settings(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(cli, "preflight", lambda settings: _passing_checks())
    monkeypatch.setattr(cli, "_open_board", _boom)
    monkeypatch.setattr(cli, "run_batch", _boom)

    assert cli.main(["check"]) == exits.OK


def test_check_exits_misconfigured_when_a_check_fails(monkeypatch, tmp_path):
    _stub_settings(monkeypatch, _settings(tmp_path))
    failing = _passing_checks()
    failing[0] = Check("VPN", False, "no route", "bring up the VPN")
    monkeypatch.setattr(cli, "preflight", lambda settings: failing)
    monkeypatch.setattr(cli, "_open_board", _boom)

    assert cli.main(["check"]) == exits.MISCONFIGURED


def test_check_never_opens_the_board_or_touches_the_filesystem(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    _stub_settings(monkeypatch, settings)
    monkeypatch.setattr(cli, "preflight", lambda s: _passing_checks())
    monkeypatch.setattr(cli, "_open_board", _boom)

    cli.main(["check"])

    assert not settings.work_dir.exists()
    assert not settings.ids_file.exists()


# -- settings: usage errors vs. refusals -------------------------------------

def test_a_bad_flag_value_is_a_usage_error_not_a_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_config_file", lambda: tmp_path / "absent.toml")

    assert cli.main(["run", "--batch-size", "0"]) == exits.USAGE


def test_a_bad_reclaim_after_flag_is_also_a_usage_error(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_config_file", lambda: tmp_path / "absent.toml")

    assert cli.main(["run", "--reclaim-after", "6x"]) == exits.USAGE


def test_a_bad_env_value_is_printed_as_a_refusal_and_the_run_continues(
        monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "_config_file", lambda: tmp_path / "absent.toml")
    monkeypatch.setenv("TDSYNC_BATCH_SIZE", "0")
    monkeypatch.setattr(cli, "preflight", lambda settings: _passing_checks())
    monkeypatch.setattr(cli, "_open_board", _boom)

    code = cli.main(["check"])

    err = capsys.readouterr().err
    assert code == exits.OK
    assert "TDSYNC_BATCH_SIZE" in err


# -- run: --batches / --until-done ------------------------------------------

def test_batches_and_until_done_together_is_a_usage_error(capsys):
    assert cli.main(["run", "--batches", "2", "--until-done"]) == exits.USAGE


# `--batches` never passes through settings.py's converters (it is not a
# Settings field), so unlike --batch-size it needs its own bounds check —
# see Finding 1: `--batches 0` used to run one batch silently, and
# `--batches -5` ran zero, neither with any error.

def test_batches_zero_is_a_usage_error_not_one_batch(monkeypatch, tmp_path):
    _stub_settings(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(cli, "_open_board", lambda s: FakeBoard())
    monkeypatch.setattr(cli, "run_batch", _boom)

    assert cli.main(["run", "--batches", "0"]) == exits.USAGE


def test_batches_negative_is_a_usage_error_not_zero_batches(monkeypatch, tmp_path):
    _stub_settings(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(cli, "_open_board", lambda s: FakeBoard())
    monkeypatch.setattr(cli, "run_batch", _boom)

    assert cli.main(["run", "--batches", "-5"]) == exits.USAGE


def test_batches_non_integer_is_a_usage_error(monkeypatch, tmp_path):
    _stub_settings(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(cli, "_open_board", lambda s: FakeBoard())
    monkeypatch.setattr(cli, "run_batch", _boom)

    assert cli.main(["run", "--batches", "many"]) == exits.USAGE


# --batch-size already goes through settings.py's own _positive_int
# converter (test_a_bad_flag_value_is_a_usage_error_not_a_fallback above
# covers zero); this is the same check for a negative value, so the two
# integer flags are proven symmetric rather than merely assumed to be.
def test_batch_size_negative_is_also_a_usage_error(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_config_file", lambda: tmp_path / "absent.toml")

    assert cli.main(["run", "--batch-size", "-3"]) == exits.USAGE


def test_batches_n_runs_exactly_n_batches(monkeypatch, tmp_path):
    # No `cli.preflight` monkeypatch here: since Finding 2, `_cmd_run`'s
    # real-batch path never calls it directly — only `run_batch()` does,
    # internally — so a real-run test has nothing to stub it for.
    _stub_settings(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(cli, "_open_board", lambda s: FakeBoard())
    monkeypatch.setattr(cli, "now_fn", lambda: NOW)
    calls = []
    monkeypatch.setattr(cli, "run_batch", lambda settings, board, now,
                        republish, keep: (calls.append(now) or BatchResult(exit_code=exits.OK)))

    code = cli.main(["run", "--batches", "3"])

    assert code == exits.OK
    assert len(calls) == 3


def test_no_count_flag_defaults_to_one_batch(monkeypatch, tmp_path):
    _stub_settings(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(cli, "_open_board", lambda s: FakeBoard())
    monkeypatch.setattr(cli, "now_fn", lambda: NOW)
    calls = []
    monkeypatch.setattr(cli, "run_batch", lambda settings, board, now,
                        republish, keep: (calls.append(1) or BatchResult(exit_code=exits.OK)))

    cli.main(["run"])

    assert len(calls) == 1


def test_until_done_stops_when_pending_is_empty(monkeypatch, tmp_path):
    board = FakeBoard(pending_answers=[["LIV0001", "LIV0002"], ["LIV0003"], []])
    _stub_settings(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(cli, "_open_board", lambda s: board)
    monkeypatch.setattr(cli, "now_fn", lambda: NOW)
    calls = []
    monkeypatch.setattr(cli, "run_batch", lambda settings, b, now,
                        republish, keep: (calls.append(1) or BatchResult(exit_code=exits.OK)))

    code = cli.main(["run", "--until-done"])

    assert code == exits.OK
    # Two non-empty pending() answers, then an empty one: exactly two
    # batches, and the loop asked pending() a third time to find out.
    assert len(calls) == 2
    assert board.pending_calls == 3


def test_a_misconfigured_batch_stops_the_loop_even_with_batches_remaining(
        monkeypatch, tmp_path):
    _stub_settings(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(cli, "_open_board", lambda s: FakeBoard())
    monkeypatch.setattr(cli, "now_fn", lambda: NOW)
    calls = []
    monkeypatch.setattr(cli, "run_batch", lambda settings, board, now,
                        republish, keep: (calls.append(1) or
                                         BatchResult(exit_code=exits.MISCONFIGURED,
                                                    message="a service is down")))

    code = cli.main(["run", "--batches", "5"])

    assert code == exits.MISCONFIGURED
    assert len(calls) == 1   # did not try batches 2-5 against the same refusal


# -- run: one preflight per batch, not two (Finding 2) -----------------------
#
# run_batch() already runs its own preflight and now hands the result
# back on BatchResult.checks (see tests/test_batch.py). _cmd_run must
# display that instead of probing a second time itself — Services alone
# shells out to the converter's own `check --strict`, which dials two
# HTTP services and the NER dependencies, so a redundant call here would
# double that cost on every batch under --until-done.

def test_preflight_is_invoked_exactly_once_per_batch_iteration(monkeypatch, tmp_path):
    _stub_settings(monkeypatch, _settings(tmp_path))
    calls = []

    def spy_preflight(settings):
        calls.append(1)
        return _passing_checks()

    monkeypatch.setattr(cli, "preflight", spy_preflight)
    monkeypatch.setattr(cli, "_open_board", lambda s: FakeBoard())
    monkeypatch.setattr(cli, "now_fn", lambda: NOW)

    def fake_run_batch(settings, board, now, republish, keep):
        # Mirrors what the real run_batch() does: one preflight() call,
        # carried back on .checks. If _cmd_run also called cli.preflight
        # itself (the bug Finding 2 describes), `calls` would come out
        # longer than the number of batches run.
        checks = cli.preflight(settings)
        return BatchResult(exit_code=exits.OK, checks=checks)

    monkeypatch.setattr(cli, "run_batch", fake_run_batch)

    code = cli.main(["run", "--batches", "3"])

    assert code == exits.OK
    assert len(calls) == 3   # not 4: _cmd_run adds no call of its own


def test_a_failed_batchs_checks_are_still_shown_with_their_remedy(
        monkeypatch, tmp_path, capsys):
    # The failure path must look identical from the outside even though
    # _cmd_run no longer probes preflight itself: still MISCONFIGURED,
    # still the failing check's detail and remedy on screen.
    _stub_settings(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(cli, "_open_board", lambda s: FakeBoard())
    monkeypatch.setattr(cli, "now_fn", lambda: NOW)
    failing_checks = [Check("VPN", False, "no route", "bring up the VPN")]
    monkeypatch.setattr(cli, "run_batch", lambda settings, board, now,
                        republish, keep: BatchResult(
                            exit_code=exits.MISCONFIGURED,
                            message="preflight refused — VPN: no route",
                            checks=failing_checks))

    code = cli.main(["run"])

    out = capsys.readouterr().out
    assert code == exits.MISCONFIGURED
    assert "no route" in out
    assert "bring up the VPN" in out


# -- run: --dry-run -----------------------------------------------------

def test_dry_run_claims_nothing_copies_nothing_and_prints_the_five_documents(
        monkeypatch, tmp_path, capsys):
    board = FakeBoard(_cards([f"LIV{i:04d}" for i in range(1, 7)]))
    _stub_settings(monkeypatch, _settings(tmp_path, batch_size=5))
    monkeypatch.setattr(cli, "preflight", lambda s: _passing_checks())
    monkeypatch.setattr(cli, "_open_board", lambda s: board)
    monkeypatch.setattr(cli, "run_batch", _boom)

    code = cli.main(["run", "--dry-run"])

    out = capsys.readouterr().out
    assert code == exits.OK
    assert board.claim_calls == []
    assert board.release_calls == []
    assert board.write_calls == []
    for doc in ["LIV0001", "LIV0002", "LIV0003", "LIV0004", "LIV0005"]:
        assert doc in out
    assert "LIV0006" not in out   # sixth candidate is past batch_size
    # Nothing was fetched from anywhere: the settings' own work_dir,
    # where fetch() would have copied an archive to, was never created.
    assert not (tmp_path / "work").exists()


def test_dry_run_with_a_failing_preflight_touches_nothing_and_reports_it(
        monkeypatch, tmp_path):
    _stub_settings(monkeypatch, _settings(tmp_path))
    failing = _passing_checks()
    failing[0] = Check("VPN", False, "no route", "bring up the VPN")
    monkeypatch.setattr(cli, "preflight", lambda s: failing)
    monkeypatch.setattr(cli, "_open_board", _boom)
    monkeypatch.setattr(cli, "run_batch", _boom)

    assert cli.main(["run", "--dry-run"]) == exits.MISCONFIGURED


# -- run: KeyboardInterrupt --------------------------------------------

def test_keyboard_interrupt_out_of_run_batch_exits_130(monkeypatch, tmp_path):
    _stub_settings(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(cli, "_open_board", lambda s: FakeBoard())
    monkeypatch.setattr(cli, "now_fn", lambda: NOW)

    def raise_interrupt(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_batch", raise_interrupt)

    assert cli.main(["run"]) == exits.INTERRUPTED


# -- run: -v / -q (Finding 3) -------------------------------------------
#
# -v and -q only ever change how much chatter is on screen — never
# whether a fact the operator needs (a loss, a failure, a released
# claim) is visible at all.

# -- main() never raises: the two exceptions that used to escape it -----------

def test_a_board_transport_error_leaves_by_the_front_door(monkeypatch, tmp_path):
    """`main()`'s docstring says it never raises; it caught only
    `SystemExit` and `KeyboardInterrupt`. `_http_transport` raises
    `BoardTransportError` on any GitHub error, and the write loop runs
    *after* publication — so a board that went down mid-batch ended the
    run with a traceback and exit 1, which this project defines as "some
    documents failed"."""
    _stub_settings(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(cli, "_open_board", lambda s: FakeBoard())
    monkeypatch.setattr(cli, "now_fn", lambda: NOW)

    def transport_died(settings, board, now, republish, keep):
        raise cli.BoardTransportError("the board did not answer: 502")

    monkeypatch.setattr(cli, "run_batch", transport_died)

    code = cli.main(["run"])

    assert code == exits.MISCONFIGURED


def test_a_board_transport_error_says_what_went_wrong(monkeypatch, tmp_path,
                                                      capsys):
    _stub_settings(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(cli, "_open_board", lambda s: FakeBoard())
    monkeypatch.setattr(cli, "now_fn", lambda: NOW)
    monkeypatch.setattr(cli, "run_batch", lambda *a, **k: (_ for _ in ()).throw(
        cli.BoardTransportError("the board did not answer: 502")))

    cli.main(["run"])

    assert "502" in capsys.readouterr().err


def test_a_stale_id_file_mid_run_is_a_refusal_not_a_traceback(monkeypatch,
                                                              tmp_path):
    """`claim()` and `write()` raise `StaleIdFile` for a Status option the
    board no longer has. That is exit 3 — the id file has to be rebuilt —
    not a stack trace."""
    _stub_settings(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(cli, "_open_board", lambda s: FakeBoard())
    monkeypatch.setattr(cli, "now_fn", lambda: NOW)
    monkeypatch.setattr(cli, "run_batch", lambda *a, **k: (_ for _ in ()).throw(
        StaleIdFile("the board's Status field has no option named 'En cours'")))

    code = cli.main(["run"])

    assert code == exits.MISCONFIGURED


def test_verbose_and_quiet_together_is_a_usage_error():
    assert cli.main(["run", "-v", "-q"]) == exits.USAGE


def test_verbose_shows_the_preflight_table_on_a_clean_pass(monkeypatch, tmp_path, capsys):
    _stub_settings(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(cli, "_open_board", lambda s: FakeBoard())
    monkeypatch.setattr(cli, "now_fn", lambda: NOW)
    checks = _passing_checks()
    monkeypatch.setattr(cli, "run_batch", lambda settings, board, now,
                        republish, keep: BatchResult(exit_code=exits.OK, checks=checks))

    cli.main(["run"])
    quiet_out = capsys.readouterr().out
    cli.main(["run", "-v"])
    verbose_out = capsys.readouterr().out

    # A clean pass says nothing about preflight by default...
    assert "Preflight" not in quiet_out
    # ...but -v shows it anyway, precisely because everything passed.
    assert "Preflight" in verbose_out
    assert "VPN" in verbose_out


def test_quiet_suppresses_the_batch_table_but_never_a_loss(monkeypatch, tmp_path, capsys):
    _stub_settings(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(cli, "_open_board", lambda s: FakeBoard())
    monkeypatch.setattr(cli, "now_fn", lambda: NOW)
    # A batch that claimed five and had to release all five — exactly
    # the scenario CLAUDE.md warns must never look like "nothing to do".
    result = BatchResult(exit_code=exits.MISCONFIGURED,
                         claimed=["LIV0001", "LIV0002", "LIV0003", "LIV0004", "LIV0005"],
                         released=["LIV0001", "LIV0002", "LIV0003", "LIV0004", "LIV0005"],
                         message="the converter refused before writing anything: "
                                 "a required service is down")
    monkeypatch.setattr(cli, "run_batch", lambda settings, board, now,
                        republish, keep: result)

    code = cli.main(["run", "-q"])

    out = capsys.readouterr().out
    assert code == exits.MISCONFIGURED
    # The per-document table (there is nothing to show a row for) is
    # gone under -q...
    assert "Identifier" not in out   # batch_table's own column header
    # ...but the fact that five were claimed and released, and why, is
    # not chatter — it must survive -q.
    assert "released 5" in out
    assert "a required service is down" in out


# -- status --------------------------------------------------------------

def test_status_prints_counts_per_statut(monkeypatch, tmp_path, capsys):
    board = FakeBoard(_cards(["LIV0001", "LIV0002"], status=TODO) +
                      _cards(["LIV0003"], status=DONE))
    _stub_settings(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(cli, "_open_board", lambda s: board)

    code = cli.main(["status"])

    out = capsys.readouterr().out
    assert code == exits.OK
    assert TODO in out
    assert DONE in out


def test_status_refuses_when_the_board_cannot_be_opened(monkeypatch, tmp_path):
    _stub_settings(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(cli, "_open_board",
                        lambda s: (_ for _ in ()).throw(
                            cli.BoardTransportError("no token")))

    assert cli.main(["status"]) == exits.MISCONFIGURED


# -- release --------------------------------------------------------------

def test_release_calls_board_release_for_every_named_document(monkeypatch, tmp_path):
    board = FakeBoard(_cards(["LIV0001", "LIV0002", "LIV0003"], status=WIP))
    _stub_settings(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(cli, "_open_board", lambda s: board)

    code = cli.main(["release", "LIV0001", "LIV0002"])

    assert code == exits.OK
    assert board.release_calls == ["LIV0001", "LIV0002"]


def test_release_refuses_on_an_identifier_the_board_does_not_have(monkeypatch, tmp_path):
    board = FakeBoard(_cards(["LIV0001"], status=WIP))
    _stub_settings(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(cli, "_open_board", lambda s: board)

    code = cli.main(["release", "LIV0999"])

    assert code == exits.USAGE
    assert board.release_calls == []


def test_release_refuses_atomically_when_one_of_several_is_unknown(monkeypatch, tmp_path):
    board = FakeBoard(_cards(["LIV0001"], status=WIP))
    _stub_settings(monkeypatch, _settings(tmp_path))
    monkeypatch.setattr(cli, "_open_board", lambda s: board)

    code = cli.main(["release", "LIV0001", "LIV0999"])

    assert code == exits.USAGE
    # The known one is not released either: a partial release on a typo'd
    # batch of identifiers would be a silent surprise later.
    assert board.release_calls == []


# -- _open_board --------------------------------------------------------

def test_open_board_refuses_without_a_token(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    settings = _settings(tmp_path)
    with pytest.raises(cli.BoardTransportError):
        cli._open_board(settings)


def test_open_board_refuses_on_an_unreadable_ids_file(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    settings = _settings(tmp_path, ids_file=tmp_path / "missing.json")
    with pytest.raises(cli.BoardTransportError):
        cli._open_board(settings)


def test_open_board_refuses_on_malformed_json(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    ids_path = tmp_path / "ids.json"
    ids_path.write_text("{not json", encoding="utf-8")
    settings = _settings(tmp_path, ids_file=ids_path)
    with pytest.raises(cli.BoardTransportError):
        cli._open_board(settings)


def test_open_board_builds_a_real_board_from_a_valid_ids_file(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    ids = {"project": {"id": "PVT_x"}, "fields": {}, "items": {}}
    ids_path = tmp_path / "ids.json"
    ids_path.write_text(json.dumps(ids), encoding="utf-8")
    settings = _settings(tmp_path, ids_file=ids_path)

    board = cli._open_board(settings)

    assert isinstance(board, Board)
    assert board.project == "PVT_x"


# -- ids refresh --------------------------------------------------------

def test_ids_refresh_writes_the_file_the_refresh_produced(monkeypatch, tmp_path):
    ids_path = tmp_path / "ids.json"
    settings = _settings(tmp_path, ids_file=ids_path)
    _stub_settings(monkeypatch, settings)
    monkeypatch.setattr(cli, "_require_token", lambda: "tok")
    produced = {"project": {"id": "PVT_x"},
               "fields": {"Status": {"id": "F1", "dataType": "SINGLE_SELECT",
                                    "options": {"À traiter": "o1"}}},
               "items": {"LIV0001": "I_1"}}
    monkeypatch.setattr(cli, "_refresh_ids", lambda project_url, transport: produced)

    code = cli.main(["ids", "refresh"])

    assert code == exits.OK
    assert json.loads(ids_path.read_text("utf-8")) == produced


def test_ids_refresh_refuses_without_a_token_and_writes_nothing(monkeypatch, tmp_path):
    ids_path = tmp_path / "ids.json"
    settings = _settings(tmp_path, ids_file=ids_path)
    _stub_settings(monkeypatch, settings)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(cli, "_refresh_ids", _boom)

    code = cli.main(["ids", "refresh"])

    assert code == exits.MISCONFIGURED
    assert not ids_path.exists()


# -- _refresh_ids (real pagination logic, fake transport) -------------------

def test_refresh_ids_builds_the_boards_shape_and_pages_through_items():
    answers = [
        {"organization": {"projectV2": {
            "id": "PVT_1",
            "fields": {"nodes": [
                {"id": "F_status", "name": "Status", "dataType": "SINGLE_SELECT",
                 "options": [{"id": "o1", "name": "À traiter"}]},
                {"id": "F_detail", "name": "Détail", "dataType": "TEXT"},
            ]},
            "items": {"pageInfo": {"hasNextPage": True, "endCursor": "CURSOR_1"},
                     "nodes": [{"id": "I_1", "content": {"title": "LIV0001"}}]},
        }}},
        {"organization": {"projectV2": {
            "id": "PVT_1", "fields": {"nodes": []},
            "items": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                     "nodes": [{"id": "I_2", "content": {"title": "LIV0002"}}]},
        }}},
    ]
    calls = []

    def transport(query, variables):
        calls.append(variables)
        return answers.pop(0)

    ids = cli._refresh_ids("https://github.com/orgs/EXAMPLE-ORG/projects/1", transport)

    assert ids == {
        "project": {"id": "PVT_1"},
        "fields": {"Status": {"id": "F_status", "dataType": "SINGLE_SELECT",
                              "options": {"À traiter": "o1"}},
                  "Détail": {"id": "F_detail", "dataType": "TEXT"}},
        "items": {"LIV0001": "I_1", "LIV0002": "I_2"},
    }
    assert calls[0]["cursor"] is None
    assert calls[1]["cursor"] == "CURSOR_1"


def test_refresh_ids_refuses_a_malformed_project_url_without_calling_the_transport():
    with pytest.raises(cli.BoardTransportError):
        cli._refresh_ids("not-a-project-url", _boom)


def test_refresh_ids_refuses_a_user_owned_project_url_without_guessing():
    # https://github.com/users/<login>/projects/<n> is a real shape
    # GitHub issues, but it needs a different query root — refuse rather
    # than silently query the wrong one and report an empty board.
    with pytest.raises(cli.BoardTransportError):
        cli._refresh_ids("https://github.com/users/someone/projects/1", _boom)


def test_open_board_refuses_cleanly_on_a_malformed_but_valid_json_ids_file(
        monkeypatch, tmp_path):
    # Valid JSON, but missing the "project" key preflight's own Board
    # check would have caught for `run`/`check`. `status` and `release`
    # never run that check, so this must not surface as a bare KeyError.
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    ids_path = tmp_path / "ids.json"
    ids_path.write_text(json.dumps({"fields": {}}), encoding="utf-8")
    settings = _settings(tmp_path, ids_file=ids_path)
    with pytest.raises(cli.BoardTransportError):
        cli._open_board(settings)
