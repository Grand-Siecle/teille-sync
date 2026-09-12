import json

import pytest

import teille_sync.convert
from pathlib import Path
from unittest.mock import MagicMock, call
from teille_sync.convert import (latest_run, read_manifest, read_incidents,
                                 validate, check_services, run_converter)


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


# -- check_services: the services block, read rather than the exit code ----
#
# `teille-douce check --strict` exits 3 on an empty input directory ("nothing
# to convert"), and preflight runs *before* anything is fetched, so that
# directory is always empty on a fresh machine. Reading the exit code made
# the Services gate refuse on every clean run and blame two services that
# were up. The gate now reads the converter's own services block instead.

SERVICES_BLOCK = """\
  input      work/OCR                                 0 volumes · 0 pages
  output     work/tei_output                                     writable
  catalogue  metadata_livre.csv                    396 rows · 0 matched
             metadata_personne.csv                        1 234 persons
  services   VieuxParler modernization                                {vp}
             PyHellen    enrichment                                   {ph}
             NER models  entity recognition                           {ner}
"""


def _report(vp="up", ph="up", ner="up", tail=""):
    return SERVICES_BLOCK.format(vp=vp, ph=ph, ner=ner) + tail


def _answers(output, returncode=0, stderr=""):
    """A `subprocess.run` double that answers with one recorded report."""
    seen = {}

    def mock_run(argv, **kwargs):
        seen["argv"] = list(argv)
        result = MagicMock()
        result.returncode = returncode
        result.stdout = output
        result.stderr = stderr
        return result

    return mock_run, seen


def test_check_services_passes_when_all_three_rows_say_up(monkeypatch):
    mock_run, _ = _answers(_report())
    monkeypatch.setattr(teille_sync.convert.subprocess, "run", mock_run)

    ok, output = check_services("some_input")

    assert ok is True
    # The child's own words travel with the answer — preflight shows them.
    assert "VieuxParler" in output and "PyHellen" in output


@pytest.mark.parametrize("down", [
    {"vp": "refused"}, {"ph": "refused"}, {"ner": "missing (torch)"},
    {"vp": "not probed"}, {"ph": "not probed"},
    {"ner": "not asked for"}, {"vp": "not asked for"},
], ids=["vieuxparler_refused", "pyhellen_refused", "ner_missing",
        "vieuxparler_not_probed", "pyhellen_not_probed",
        "ner_not_asked_for", "vieuxparler_not_asked_for"])
def test_one_service_that_is_not_up_refuses_the_whole_gate(monkeypatch, down):
    mock_run, _ = _answers(_report(**down))
    monkeypatch.setattr(teille_sync.convert.subprocess, "run", mock_run)

    ok, output = check_services("some_input")

    assert ok is False
    assert "services" in output


def test_a_report_with_no_services_block_is_a_refusal_not_a_pass(monkeypatch):
    """A gate that passes because it failed to parse is the worst
    outcome here: it would send five archives over the VPN to a
    converter whose services nobody checked."""
    mock_run, _ = _answers("teille-douce: -i: no such directory\n", returncode=3)
    monkeypatch.setattr(teille_sync.convert.subprocess, "run", mock_run)

    ok, output = check_services("some_input")

    assert ok is False
    # It says *why* it refused, and names what it could not find.
    assert "could not be read" in output
    assert "VieuxParler" in output and "PyHellen" in output
    assert "NER models" in output
    # The child's own words are kept alongside the explanation.
    assert "no such directory" in output


def test_a_partial_services_block_is_a_refusal(monkeypatch):
    """Two rows out of three: the third might be down, might be absent.
    Refuse rather than guess."""
    partial = ("  services   VieuxParler modernization      up\n"
               "             PyHellen    enrichment         up\n")
    mock_run, _ = _answers(partial)
    monkeypatch.setattr(teille_sync.convert.subprocess, "run", mock_run)

    ok, output = check_services("some_input")

    assert ok is False
    assert "NER models" in output


def test_an_empty_input_directory_is_not_a_service_failure(monkeypatch):
    """**The regression guard for the gate that refused on every fresh
    machine.** Preflight runs before anything is fetched, so the input
    directory is empty and the converter's verdict line reads "unusable
    — nothing to convert" with a non-zero exit code. All three services
    are up; the gate exists to check the services, and it must pass."""
    mock_run, _ = _answers(
        _report(tail="\n  unusable — nothing to convert\n"), returncode=3)
    monkeypatch.setattr(teille_sync.convert.subprocess, "run", mock_run)

    ok, output = check_services("some_input")

    assert ok is True
    assert "nothing to convert" in output


def test_check_services_never_passes_strict_to_the_converter(monkeypatch):
    """`--strict` is what turned "nothing to convert" into a refusal.
    The flag is gone; the command is the plain `check -i <dir>`."""
    mock_run, seen = _answers(_report())
    monkeypatch.setattr(teille_sync.convert.subprocess, "run", mock_run)

    check_services("some_input")

    assert "--strict" not in seen["argv"]
    assert seen["argv"][:2] == ["teille-douce", "check"]
    assert seen["argv"][seen["argv"].index("-i") + 1] == "some_input"


def test_a_service_row_state_is_read_even_when_stderr_carried_it(monkeypatch):
    """The converter writes its report to stdout, but a run that fell
    over can leave part of it on stderr. Both halves are searched."""
    mock_run, _ = _answers("", returncode=3, stderr=_report())
    monkeypatch.setattr(teille_sync.convert.subprocess, "run", mock_run)

    ok, _ = check_services("some_input")

    assert ok is True


def test_run_converter_passes_metadata_and_persons_flags(monkeypatch):
    """The converter's own config discovery walks up the parent
    directories from wherever it runs — teille-sync must be explicit
    rather than let a sync invoked from elsewhere inherit whatever
    discovery finds. This is the test that fails if `--metadata` /
    `--persons` are ever dropped from argv again."""
    seen = {}

    def mock_run(argv, **kwargs):
        seen["argv"] = argv
        result = MagicMock()
        result.returncode = 0
        return result

    import teille_sync.convert
    monkeypatch.setattr(teille_sync.convert.subprocess, "run", mock_run)

    run_converter(["LIV0001_reconciled"], "OCR", "tei_output",
                  Path("cat/metadata_livre.csv"), Path("cat/metadata_personne.csv"),
                  Path("cat/entities"))

    argv = seen["argv"]
    assert "--metadata" in argv
    assert argv[argv.index("--metadata") + 1] == "cat/metadata_livre.csv"
    assert "--persons" in argv
    assert argv[argv.index("--persons") + 1] == "cat/metadata_personne.csv"


def test_run_converter_passes_the_entities_flag(monkeypatch):
    """`--entities` closes the same config-discovery trap as `--metadata`/
    `--persons`: without it, a `TDOUCE_ENTITIES_DIR` or a `paths.entities`
    in some parent directory's TOML silently relocates the NER entity
    CSVs, and a publish step looking in the wrong place skips them with
    no complaint. This is the test that fails if `--entities` is ever
    dropped from argv again."""
    seen = {}

    def mock_run(argv, **kwargs):
        seen["argv"] = argv
        result = MagicMock()
        result.returncode = 0
        return result

    import teille_sync.convert
    monkeypatch.setattr(teille_sync.convert.subprocess, "run", mock_run)

    run_converter(["LIV0001_reconciled"], "OCR", "tei_output",
                  Path("metadata_livre.csv"), Path("metadata_personne.csv"),
                  Path("cat/entities"))

    argv = seen["argv"]
    assert "--entities" in argv
    assert argv[argv.index("--entities") + 1] == "cat/entities"


def test_run_converter_still_requires_all_phases_and_services(monkeypatch):
    seen = {}

    def mock_run(argv, **kwargs):
        seen["argv"] = argv
        result = MagicMock()
        result.returncode = 0
        return result

    import teille_sync.convert
    monkeypatch.setattr(teille_sync.convert.subprocess, "run", mock_run)

    run_converter(["LIV0001_reconciled"], "OCR", "tei_output",
                  Path("metadata_livre.csv"), Path("metadata_personne.csv"),
                  Path("entities"))

    argv = seen["argv"]
    assert "--phases" in argv and argv[argv.index("--phases") + 1] == "all"
    assert "--require-services" in argv


def test_run_converter_returns_the_childs_exit_code(monkeypatch):
    def mock_run(argv, **kwargs):
        result = MagicMock()
        result.returncode = 3
        return result

    import teille_sync.convert
    monkeypatch.setattr(teille_sync.convert.subprocess, "run", mock_run)

    code = run_converter(["LIV0001_reconciled"], "OCR", "tei_output",
                         Path("metadata_livre.csv"), Path("metadata_personne.csv"),
                         Path("entities"))
    assert code == 3


def test_run_converter_requires_metadata_csv_and_persons_csv():
    """Required positional, not optional-with-a-default: an optional
    parameter here would silently reopen the config-discovery trap this
    signature exists to close."""
    with pytest.raises(TypeError):
        run_converter(["LIV0001_reconciled"], "OCR", "tei_output")


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
