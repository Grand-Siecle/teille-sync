import json
from collections import namedtuple
from pathlib import Path

from teille_sync.preflight import preflight
from teille_sync.settings import Settings

Usage = namedtuple("Usage", "total used free")

GIB = 1024 * 1024 * 1024


def _settings(**over):
    values = {"nas_root": Path("/nope"), "nas_host": "nas.example.invalid",
              "batch_size": 5, "reclaim_after": None, "project_url": "u",
              "ids_file": Path("ids.json"), "work_dir": Path("work"),
              "metadata_csv": Path("nonexistent-metadata.csv"),
              "persons_csv": Path("nonexistent-persons.csv")}
    values.update(over)
    return Settings(values=values,
                    origins={k: "default" for k in values}, refusals=[])


def _by(checks, name):
    return next(c for c in checks if c.name == name)


# -- the brief's own tests (Step 1) --------------------------------------

def test_a_dead_tunnel_stops_everything_and_names_the_vpn():
    checks = preflight(_settings(), probe=lambda h, **k: (False, "no route"))
    assert _by(checks, "VPN").ok is False
    assert "VPN" in _by(checks, "VPN").remedy


def test_a_windows_drive_missing_from_wsl_gets_the_mount_command(tmp_path,
                                                                 monkeypatch):
    monkeypatch.setattr("teille_sync.preflight.on_wsl", lambda: True)
    checks = preflight(_settings(nas_root=Path("/mnt/y")),
                       probe=lambda h, **k: (True, ""))
    root = _by(checks, "NAS root")
    assert root.ok is False
    assert "mount -t drvfs" in root.remedy
    assert "sudo mount -t drvfs Y: /mnt/y" == root.remedy


def test_the_mount_command_uses_the_letter_from_the_configured_root(
        monkeypatch):
    """A different drive letter must produce a different command — an
    implementation with "Y:" hardcoded would pass the test above without
    actually deriving the letter from nas_root."""
    monkeypatch.setattr("teille_sync.preflight.on_wsl", lambda: True)
    checks = preflight(_settings(nas_root=Path("/mnt/z")),
                       probe=lambda h, **k: (True, ""))
    root = _by(checks, "NAS root")
    assert root.ok is False
    assert root.remedy == "sudo mount -t drvfs Z: /mnt/z"


def test_a_root_without_the_archives_folder_is_the_wrong_root(tmp_path):
    (tmp_path / "something-else").mkdir()
    checks = preflight(_settings(nas_root=tmp_path),
                       probe=lambda h, **k: (True, ""))
    assert _by(checks, "NAS root").ok is False
    assert "zip_reconciliate" in _by(checks, "NAS root").detail


def test_a_complete_share_passes_the_root_and_destination_checks(tmp_path):
    (tmp_path / "OCR" / "zip_reconciliate").mkdir(parents=True)
    checks = preflight(_settings(nas_root=tmp_path),
                       probe=lambda h, **k: (True, ""))
    assert _by(checks, "NAS root").ok
    assert _by(checks, "Destination").ok


def test_destination_check_refuses_a_pre_existing_directory_that_is_not_writable(
        tmp_path):
    """`mkdir(exist_ok=True)` is silent when the directory (and its
    `_a_verifier` child) already exist — that alone would not catch a
    `tei/` that is read-only, since checking "is it already there" needs
    no write access to the parent. Both must be created *before* the
    permission is dropped, so `mkdir(exist_ok=True)` has nothing left to
    do and only an actual write probe can catch the missing permission."""
    (tmp_path / "OCR" / "zip_reconciliate").mkdir(parents=True)
    tei = tmp_path / "tei"
    (tei / "_a_verifier").mkdir(parents=True)
    tei.chmod(0o500)  # read + execute, no write
    try:
        checks = preflight(_settings(nas_root=tmp_path),
                           probe=lambda h, **k: (True, ""))
    finally:
        tei.chmod(0o700)  # tmp_path cleanup needs this back
    assert _by(checks, "Destination").ok is False


def test_a_refused_service_probe_stops_the_batch(tmp_path, monkeypatch):
    (tmp_path / "OCR" / "zip_reconciliate").mkdir(parents=True)
    monkeypatch.setattr(
        "teille_sync.preflight.check_services",
        lambda d: (False, "  services   VieuxParler modernization   refused"))
    checks = preflight(_settings(nas_root=tmp_path),
                       probe=lambda h, **k: (True, ""))
    services = _by(checks, "Services")
    assert services.ok is False
    # the converter's own words, not a paraphrase
    assert "VieuxParler" in services.detail
    assert "--phases all" in services.remedy


def test_services_up_passes_and_says_so(tmp_path, monkeypatch):
    (tmp_path / "OCR" / "zip_reconciliate").mkdir(parents=True)
    monkeypatch.setattr("teille_sync.preflight.check_services",
                        lambda d: (True, "all up"))
    checks = preflight(_settings(nas_root=tmp_path),
                       probe=lambda h, **k: (True, ""))
    assert _by(checks, "Services").ok


def test_a_missing_converter_says_how_to_install_it(tmp_path):
    (tmp_path / "OCR" / "zip_reconciliate").mkdir(parents=True)
    checks = preflight(_settings(nas_root=tmp_path),
                       probe=lambda h, **k: (True, ""),
                       which=lambda name: None)
    assert _by(checks, "Converter").ok is False
    assert "pip install" in _by(checks, "Converter").remedy


def test_the_checks_never_carry_the_host_in_their_detail():
    checks = preflight(_settings(nas_host="secret.internal.example"),
                       probe=lambda h, **k: (False, "no route to host"))
    for c in checks:
        assert "secret.internal.example" not in c.detail
        assert "secret.internal.example" not in c.remedy


def test_preflight_returns_all_eight_checks_even_when_the_first_fails():
    checks = preflight(_settings(), probe=lambda h, **k: (False, "no route"))
    names = [c.name for c in checks]
    assert names == ["VPN", "NAS root", "Destination", "Board", "Converter",
                     "Services", "Metadata", "Disk"]


def test_a_missing_converter_does_not_crash_the_services_probe(tmp_path):
    """check_services() shells out to `teille-douce`; if the binary is not
    on PATH, subprocess raises FileNotFoundError. The Services check must
    turn that into a refusal, not a crash that hides the other seven
    checks from the operator."""
    (tmp_path / "OCR" / "zip_reconciliate").mkdir(parents=True)
    checks = preflight(_settings(nas_root=tmp_path),
                       probe=lambda h, **k: (True, ""),
                       which=lambda name: None)
    services = _by(checks, "Services")
    assert services.ok is False
    assert services.detail  # some honest explanation, not an empty string


# -- Board: the ids file exists, parses, and carries the right shape ----

VALID_IDS = {
    "project": {"id": "PVT_test"},
    "fields": {"Status": {"id": "F_status"}},
}


def test_board_check_refuses_when_the_ids_file_is_absent(tmp_path):
    checks = preflight(_settings(ids_file=tmp_path / "missing.json"),
                       probe=lambda h, **k: (True, ""))
    board = _by(checks, "Board")
    assert board.ok is False
    assert "missing.json" in board.detail


def test_board_check_refuses_on_invalid_json(tmp_path):
    ids_file = tmp_path / "ids.json"
    ids_file.write_text("{not json", encoding="utf-8")
    checks = preflight(_settings(ids_file=ids_file),
                       probe=lambda h, **k: (True, ""))
    board = _by(checks, "Board")
    assert board.ok is False
    assert "ids.json" in board.detail


def test_board_check_refuses_when_the_project_id_is_missing(tmp_path):
    ids_file = tmp_path / "ids.json"
    ids_file.write_text(json.dumps({"fields": {"Status": {"id": "F_1"}}}),
                        encoding="utf-8")
    checks = preflight(_settings(ids_file=ids_file),
                       probe=lambda h, **k: (True, ""))
    board = _by(checks, "Board")
    assert board.ok is False


def test_board_check_refuses_when_the_fields_map_is_missing(tmp_path):
    ids_file = tmp_path / "ids.json"
    ids_file.write_text(json.dumps({"project": {"id": "PVT_1"}}),
                        encoding="utf-8")
    checks = preflight(_settings(ids_file=ids_file),
                       probe=lambda h, **k: (True, ""))
    board = _by(checks, "Board")
    assert board.ok is False


def test_board_check_refuses_when_the_fields_map_is_empty(tmp_path):
    ids_file = tmp_path / "ids.json"
    ids_file.write_text(json.dumps({"project": {"id": "PVT_1"}, "fields": {}}),
                        encoding="utf-8")
    checks = preflight(_settings(ids_file=ids_file),
                       probe=lambda h, **k: (True, ""))
    assert _by(checks, "Board").ok is False


def test_board_check_passes_on_a_complete_ids_file(tmp_path):
    ids_file = tmp_path / "ids.json"
    ids_file.write_text(json.dumps(VALID_IDS), encoding="utf-8")
    checks = preflight(_settings(ids_file=ids_file),
                       probe=lambda h, **k: (True, ""))
    assert _by(checks, "Board").ok is True


def test_board_check_makes_no_network_call(tmp_path):
    """Checking the file is what this check is for — a stale id would
    otherwise only be caught by a live GraphQL call, which preflight must
    never make."""
    ids_file = tmp_path / "ids.json"
    ids_file.write_text(json.dumps(VALID_IDS), encoding="utf-8")

    def explode(*a, **k):
        raise AssertionError("preflight must not touch the network")

    import teille_sync.preflight as preflight_module
    import socket as socket_module
    # If anything under preflight tried to open a socket outside of the
    # injected `probe`, this would raise instead of connecting anywhere.
    monkeypatch_target = socket_module.socket
    try:
        socket_module.socket = explode
        checks = preflight(_settings(ids_file=ids_file),
                           probe=lambda h, **k: (True, ""))
    finally:
        socket_module.socket = monkeypatch_target
    assert _by(checks, "Board").ok is True


# -- Metadata: only unreadable files are a refusal -----------------------
#
# The check reads settings.metadata_csv / settings.persons_csv directly —
# the exact paths run_converter() passes as --metadata/--persons — rather
# than inferring a location from work_dir, since the converter's own
# config discovery walks up the parent directories from wherever it runs
# and a guessed path here could pass while the run reads a different file.

def test_metadata_check_refuses_when_both_catalogues_are_absent(tmp_path):
    checks = preflight(_settings(metadata_csv=tmp_path / "metadata_livre.csv",
                                 persons_csv=tmp_path / "metadata_personne.csv"),
                       probe=lambda h, **k: (True, ""))
    metadata = _by(checks, "Metadata")
    assert metadata.ok is False
    assert "metadata_livre.csv" in metadata.detail
    assert "metadata_personne.csv" in metadata.detail


def test_metadata_check_refuses_when_one_catalogue_is_missing(tmp_path):
    livre = tmp_path / "metadata_livre.csv"
    livre.write_text("id;title\n", encoding="utf-8")
    checks = preflight(_settings(metadata_csv=livre,
                                 persons_csv=tmp_path / "metadata_personne.csv"),
                       probe=lambda h, **k: (True, ""))
    metadata = _by(checks, "Metadata")
    assert metadata.ok is False
    assert "metadata_personne.csv" in metadata.detail


def test_metadata_check_refuses_on_an_unreadable_file(tmp_path):
    livre = tmp_path / "metadata_livre.csv"
    livre.write_text("id;title\n", encoding="utf-8")
    persons = tmp_path / "metadata_personne.csv"
    persons.write_text("id;name\n", encoding="utf-8")
    livre.chmod(0o000)
    try:
        checks = preflight(_settings(metadata_csv=livre, persons_csv=persons),
                           probe=lambda h, **k: (True, ""))
    finally:
        livre.chmod(0o644)  # tmp_path cleanup needs this back
    assert _by(checks, "Metadata").ok is False


def test_metadata_check_passes_on_two_readable_catalogues(tmp_path):
    livre = tmp_path / "metadata_livre.csv"
    livre.write_text("id;title\n", encoding="utf-8")
    persons = tmp_path / "metadata_personne.csv"
    persons.write_text("id;name\n", encoding="utf-8")
    checks = preflight(_settings(metadata_csv=livre, persons_csv=persons),
                       probe=lambda h, **k: (True, ""))
    assert _by(checks, "Metadata").ok is True


def test_metadata_coverage_is_not_a_gate_an_empty_catalogue_still_passes(tmp_path):
    """An empty file — zero rows, zero coverage — is still readable, and
    readability is the only thing this check tests. Coverage gaps are a
    per-document warning at conversion time, not a preflight refusal."""
    livre = tmp_path / "metadata_livre.csv"
    livre.write_text("", encoding="utf-8")
    persons = tmp_path / "metadata_personne.csv"
    persons.write_text("", encoding="utf-8")
    checks = preflight(_settings(metadata_csv=livre, persons_csv=persons),
                       probe=lambda h, **k: (True, ""))
    assert _by(checks, "Metadata").ok is True


def test_metadata_check_uses_a_custom_location_not_work_dir(tmp_path):
    """A catalogue that lives nowhere near work_dir must still pass — the
    check must not silently look in work_dir instead of the configured
    paths."""
    catalogue_dir = tmp_path / "elsewhere"
    catalogue_dir.mkdir()
    livre = catalogue_dir / "metadata_livre.csv"
    livre.write_text("id;title\n", encoding="utf-8")
    persons = catalogue_dir / "metadata_personne.csv"
    persons.write_text("id;name\n", encoding="utf-8")
    checks = preflight(_settings(work_dir=tmp_path / "not-the-catalogue-dir",
                                 metadata_csv=livre, persons_csv=persons),
                       probe=lambda h, **k: (True, ""))
    assert _by(checks, "Metadata").ok is True


# -- Disk: free space for a batch, with margin ---------------------------

def test_disk_check_passes_when_free_space_comfortably_exceeds_the_margin(
        tmp_path, monkeypatch):
    monkeypatch.setattr("teille_sync.preflight.disk_usage",
                        lambda p: Usage(100 * GIB, 50 * GIB, 50 * GIB))
    checks = preflight(_settings(work_dir=tmp_path),
                       probe=lambda h, **k: (True, ""))
    assert _by(checks, "Disk").ok is True


def test_disk_check_refuses_when_free_space_is_below_the_margin(
        tmp_path, monkeypatch):
    monkeypatch.setattr("teille_sync.preflight.disk_usage",
                        lambda p: Usage(10 * GIB, 10 * GIB - 1024, 1024))
    checks = preflight(_settings(work_dir=tmp_path, batch_size=5),
                       probe=lambda h, **k: (True, ""))
    disk = _by(checks, "Disk")
    assert disk.ok is False
    assert disk.remedy


def test_disk_check_scales_the_requirement_with_batch_size(tmp_path,
                                                            monkeypatch):
    """A bigger batch needs more headroom: five hundred MB clears a batch
    of one but not a batch of thirty."""
    free_bytes = 500 * 1024 * 1024
    monkeypatch.setattr("teille_sync.preflight.disk_usage",
                        lambda p: Usage(free_bytes * 4, free_bytes * 3,
                                       free_bytes))
    small = preflight(_settings(work_dir=tmp_path, batch_size=1),
                      probe=lambda h, **k: (True, ""))
    big = preflight(_settings(work_dir=tmp_path, batch_size=30),
                    probe=lambda h, **k: (True, ""))
    assert _by(small, "Disk").ok is True
    assert _by(big, "Disk").ok is False


def test_disk_check_walks_up_to_an_existing_ancestor(tmp_path, monkeypatch):
    """work_dir is created only once a batch actually fetches something —
    at preflight time it may not exist yet, and disk_usage() raises on a
    path that is not there."""
    missing = tmp_path / "not-created-yet" / "nested"
    seen = []

    def fake_usage(path):
        seen.append(Path(path))
        return Usage(100 * GIB, 50 * GIB, 50 * GIB)

    monkeypatch.setattr("teille_sync.preflight.disk_usage", fake_usage)
    checks = preflight(_settings(work_dir=missing),
                       probe=lambda h, **k: (True, ""))
    assert _by(checks, "Disk").ok is True
    assert seen[0] == tmp_path
