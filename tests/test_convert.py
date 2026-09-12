import json
from pathlib import Path
from unittest.mock import MagicMock, call
from teille_sync.convert import latest_run, read_manifest, read_incidents, validate, check_services


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


def test_latest_run_orders_by_timestamp_first(tmp_path):
    """Different timestamps: later timestamp wins regardless of pid."""
    _run(tmp_path, "20260912-095959-0000001")
    newest = _run(tmp_path, "20260912-100000-0000001")
    assert latest_run(tmp_path) == newest


def test_a_half_written_manifest_reads_as_empty_rather_than_raising(tmp_path):
    d = _run(tmp_path, "20260912-100000-0000001")
    (d / "run.json").write_text('{"documents": {"LIV0001_recon', encoding="utf-8")
    assert read_manifest(d) == {}


def test_missing_run_json_file_returns_empty_dict(tmp_path):
    """A run directory that exists but has no run.json returns empty dict."""
    d = _run(tmp_path, "20260912-100000-0000001", manifest=None)  # don't create run.json
    assert read_manifest(d) == {}


def test_missing_run_directory_returns_empty_dict(tmp_path):
    """A run directory that doesn't exist at all returns empty dict."""
    assert read_manifest(tmp_path / "no-such-run") == {}


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


def test_missing_run_directory_incidents_returns_empty_list(tmp_path):
    """A run directory that doesn't exist returns empty list of incidents."""
    assert read_incidents(tmp_path / "no-such-run") == []


def test_check_services_returns_ok_when_exitcode_is_zero(monkeypatch):
    """When teille-douce check succeeds, return (True, output)."""
    def mock_run(*args, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = "All services available\n"
        result.stderr = ""
        return result

    import teille_sync.convert
    monkeypatch.setattr(teille_sync.convert.subprocess, "run", mock_run)

    ok, output = check_services("some_input")
    assert ok is True
    assert "All services available" in output


def test_check_services_returns_fail_when_exitcode_nonzero(monkeypatch):
    """When teille-douce check fails, return (False, combined_output)."""
    def mock_run(*args, **kwargs):
        result = MagicMock()
        result.returncode = 1
        result.stdout = "stdout message"
        result.stderr = "stderr message"
        return result

    import teille_sync.convert
    monkeypatch.setattr(teille_sync.convert.subprocess, "run", mock_run)

    ok, output = check_services("some_input")
    assert ok is False
    assert "stdout message" in output
    assert "stderr message" in output


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


def _stub_validate(monkeypatch, response_json, expected_argv=None):
    """Stub subprocess.run to return the given JSON as stdout."""
    def mock_run(argv, **kwargs):
        if expected_argv is not None:
            assert argv == expected_argv, f"Expected argv {expected_argv}, got {argv}"
        result = MagicMock()
        result.stdout = json.dumps(response_json)
        result.returncode = 0
        return result

    import teille_sync.convert
    monkeypatch.setattr(teille_sync.convert.subprocess, "run", mock_run)


def test_an_empty_error_list_is_what_valid_means(monkeypatch, tmp_path):
    """Empty errors list means valid=True."""
    output_dir = tmp_path / "tei"
    output_dir.mkdir()
    # Create the file so validate() will pass it to the validator
    (output_dir / "LIV0044_reconciled.tei.xml").touch()

    # Stub to return CLEAN response for this file path
    def mock_run(argv, **kwargs):
        result = MagicMock()
        result.stdout = json.dumps(CLEAN)
        result.returncode = 0
        return result

    import teille_sync.convert
    monkeypatch.setattr(teille_sync.convert.subprocess, "run", mock_run)

    result = validate(output_dir, ["LIV0044_reconciled"])
    assert result["LIV0044_reconciled"]["valid"] is True


def test_errors_make_it_invalid_and_are_kept_as_written(monkeypatch, tmp_path):
    """Non-empty errors list means valid=False and errors are preserved."""
    output_dir = tmp_path / "tei"
    output_dir.mkdir()
    # Create the file so validate() will pass it to the validator
    (output_dir / "LIV9999_reconciled.tei.xml").touch()

    # Stub to return BROKEN response for this file path
    def mock_run(argv, **kwargs):
        result = MagicMock()
        result.stdout = json.dumps(BROKEN)
        result.returncode = 0
        return result

    import teille_sync.convert
    monkeypatch.setattr(teille_sync.convert.subprocess, "run", mock_run)

    result = validate(output_dir, ["LIV9999_reconciled"])
    got = result["LIV9999_reconciled"]
    assert got["valid"] is False
    assert "blockquote" in got["errors"][0]


def test_validate_passes_individual_file_paths_not_directory(monkeypatch, tmp_path):
    """validate() should pass specific file paths, not the directory."""
    # Create actual files so Path(...).exists() returns True
    output_dir = tmp_path / "tei_out"
    output_dir.mkdir()
    (output_dir / "LIV0001_reconciled.tei.xml").touch()
    (output_dir / "LIV0002_reconciled.tei.xml").touch()

    expected_argv = [
        "teille-douce", "validate",
        str(output_dir / "LIV0001_reconciled.tei.xml"),
        str(output_dir / "LIV0002_reconciled.tei.xml"),
        "--json"
    ]

    response = {"applied": ["python invariants"], "errors": 0,
                "files": [{"path": str(output_dir / "LIV0001_reconciled.tei.xml"),
                           "errors": [], "warnings": []}]}

    _stub_validate(monkeypatch, response, expected_argv=expected_argv)
    result = validate(output_dir, ["LIV0001_reconciled", "LIV0002_reconciled"])
    assert "LIV0001_reconciled" in result


def test_validate_skips_documents_with_no_output_file(monkeypatch, tmp_path):
    """If a document has no .tei.xml file, it is skipped (not passed to validator)."""
    output_dir = tmp_path / "tei_out"
    output_dir.mkdir()
    # Only create one of two files
    (output_dir / "LIV0001_reconciled.tei.xml").touch()
    # LIV0002_reconciled.tei.xml does not exist

    expected_argv = [
        "teille-douce", "validate",
        str(output_dir / "LIV0001_reconciled.tei.xml"),
        "--json"
    ]

    response = {"applied": ["python invariants"], "errors": 0,
                "files": [{"path": str(output_dir / "LIV0001_reconciled.tei.xml"),
                           "errors": [], "warnings": []}]}

    _stub_validate(monkeypatch, response, expected_argv=expected_argv)
    result = validate(output_dir, ["LIV0001_reconciled", "LIV0002_reconciled"])
    # Only LIV0001 should be in the result
    assert "LIV0001_reconciled" in result
    assert "LIV0002_reconciled" not in result


def test_validate_returns_empty_dict_when_no_documents_have_files(monkeypatch, tmp_path):
    """If no documents have output files, return empty dict without calling validator."""
    output_dir = tmp_path / "tei_out"
    output_dir.mkdir()
    # Create no files

    called = []
    def mock_run(argv, **kwargs):
        called.append(True)
        raise AssertionError("Should not call validator if no files exist")

    import teille_sync.convert
    monkeypatch.setattr(teille_sync.convert.subprocess, "run", mock_run)

    result = validate(output_dir, ["LIV0001_reconciled", "LIV0002_reconciled"])
    assert result == {}
    assert not called
