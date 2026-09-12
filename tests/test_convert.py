import json
from pathlib import Path
from unittest.mock import MagicMock
from teille_sync.convert import latest_run, read_manifest, read_incidents, validate


def _run(root, stamp, manifest=None, incidents=()):
    d = root / ".teille-douce" / "runs" / stamp
    d.mkdir(parents=True)
    if manifest is not None:
        (d / "run.json").write_text(json.dumps(manifest), encoding="utf-8")
    if incidents:
        (d / "incidents.jsonl").write_text(
            "\n".join(json.dumps(i) for i in incidents) + "\n", encoding="utf-8")
    return d


def test_latest_run_sorts_on_the_padded_name(tmp_path):
    _run(tmp_path, "20260912-100000-0000987")
    newest = _run(tmp_path, "20260912-100000-0001234")
    assert latest_run(tmp_path) == newest


def test_latest_run_is_none_when_nothing_ran(tmp_path):
    assert latest_run(tmp_path) is None


def test_a_half_written_manifest_reads_as_empty_rather_than_raising(tmp_path):
    d = _run(tmp_path, "20260912-100000-0000001")
    (d / "run.json").write_text('{"documents": {"LIV0001_recon', encoding="utf-8")
    assert read_manifest(d) == {}


def test_incidents_skip_damaged_lines_and_keep_the_rest(tmp_path):
    d = _run(tmp_path, "20260912-100000-0000001",
             manifest={"documents": {}},
             incidents=[{"code": "page_unusable", "document": "LIV0001_reconciled"}])
    with open(d / "incidents.jsonl", "a", encoding="utf-8") as fh:
        fh.write('{"code": "truncated\n')          # a Ctrl-C mid-write
    got = read_incidents(d)
    assert len(got) == 1
    assert got[0]["code"] == "page_unusable"


def test_missing_incidents_file_is_an_empty_list(tmp_path):
    d = _run(tmp_path, "20260912-100000-0000001", manifest={"documents": {}})
    assert read_incidents(d) == []


CLEAN = {"applied": ["python invariants"], "errors": 0,
         "files": [{"path": "tei/LIV0044_reconciled.tei.xml",
                    "errors": [], "warnings": []}]}
BROKEN = {"applied": ["python invariants"], "errors": 2,
          "files": [{"path": "tei/LIV9999_reconciled.tei.xml",
                     "errors": ["teille-douce.rng L27663: Did not expect "
                                "element blockquote there",
                                "Schematron: Element \"blockquote\" is not "
                                "part of the TEIlle-douce inventory."],
                     "warnings": []}]}


def _stub_validate(monkeypatch, response_json):
    """Stub subprocess.run to return the given JSON as stdout."""
    def mock_run(*args, **kwargs):
        result = MagicMock()
        result.stdout = json.dumps(response_json)
        result.returncode = 0
        return result

    import teille_sync.convert
    monkeypatch.setattr(teille_sync.convert.subprocess, "run", mock_run)


def test_an_empty_error_list_is_what_valid_means(monkeypatch):
    _stub_validate(monkeypatch, CLEAN)
    assert validate("tei", [])["LIV0044_reconciled"]["valid"] is True


def test_errors_make_it_invalid_and_are_kept_as_written(monkeypatch):
    _stub_validate(monkeypatch, BROKEN)
    got = validate("tei", [])["LIV9999_reconciled"]
    assert got["valid"] is False
    assert "blockquote" in got["errors"][0]
