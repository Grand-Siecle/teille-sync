import socket
import zipfile
from pathlib import Path
import pytest
from teille_sync.nas import fetch, publish, archives_dir, tei_dir, reachable


def _share(tmp_path, names=("LIV0001",)):
    root = tmp_path / "share"
    (root / "OCR" / "zip_reconciliate").mkdir(parents=True)
    (root / "tei").mkdir(parents=True)
    for n in names:
        z = root / "OCR" / "zip_reconciliate" / f"{n}_reconciled.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr(f"{n}/page_0001.xml", "<alto/>")
    return root


def test_fetch_copies_and_reports_the_local_path(tmp_path):
    root = _share(tmp_path)
    got, why = fetch(root, "LIV0001", tmp_path / "in")
    assert got == tmp_path / "in" / "LIV0001_reconciled.zip"
    assert got.exists()
    assert why == ""


def test_fetch_of_an_absent_archive_says_so_and_does_not_raise(tmp_path):
    root = _share(tmp_path)
    got, why = fetch(root, "LIV9999", tmp_path / "in")
    assert got is None
    assert "not on the share" in why


def test_fetch_rejects_a_truncated_copy(tmp_path, monkeypatch):
    root = _share(tmp_path)

    def short_copy(src, dst):
        Path(dst).write_bytes(Path(src).read_bytes()[:10])

    monkeypatch.setattr("teille_sync.nas.shutil.copyfile", short_copy)
    got, why = fetch(root, "LIV0001", tmp_path / "in")
    assert got is None
    assert "size" in why
    target = tmp_path / "in" / "LIV0001_reconciled.zip"
    assert not target.exists(), "the truncated file should have been cleaned up"


def test_fetch_rejects_an_archive_that_will_not_open(tmp_path):
    root = _share(tmp_path)
    bad = archives_dir(root) / "LIV0002_reconciled.zip"
    bad.write_bytes(b"not a zip at all")
    got, why = fetch(root, "LIV0002", tmp_path / "in")
    assert got is None
    assert "open" in why
    target = tmp_path / "in" / "LIV0002_reconciled.zip"
    assert not target.exists(), "the bad archive should have been cleaned up"


def test_publish_drops_the_reconciled_suffix(tmp_path):
    root = _share(tmp_path)
    tei = tmp_path / "out" / "LIV0001_reconciled.tei.xml"
    tei.parent.mkdir(parents=True)
    tei.write_text("<TEI/>", encoding="utf-8")
    ok, why = publish(tei, None, root, "LIV0001", review=False, republish=False)
    assert ok, why
    assert (tei_dir(root) / "LIV0001.tei.xml").exists()


def test_publish_refuses_an_existing_file_without_republish(tmp_path):
    root = _share(tmp_path)
    (tei_dir(root) / "LIV0001.tei.xml").write_text("old", encoding="utf-8")
    tei = tmp_path / "out" / "LIV0001_reconciled.tei.xml"
    tei.parent.mkdir(parents=True)
    tei.write_text("<TEI/>", encoding="utf-8")

    ok, why = publish(tei, None, root, "LIV0001", review=False, republish=False)
    assert not ok
    assert "--republish" in why
    assert (tei_dir(root) / "LIV0001.tei.xml").read_text() == "old"

    ok, why = publish(tei, None, root, "LIV0001", review=False, republish=True)
    assert ok, why
    assert (tei_dir(root) / "LIV0001.tei.xml").read_text() == "<TEI/>"


def test_review_goes_to_its_own_folder(tmp_path):
    root = _share(tmp_path)
    tei = tmp_path / "out" / "LIV0001_reconciled.tei.xml"
    tei.parent.mkdir(parents=True)
    tei.write_text("<TEI/>", encoding="utf-8")
    ok, why = publish(tei, None, root, "LIV0001", review=True, republish=False)
    assert ok, why
    assert (tei_dir(root) / "_a_verifier" / "LIV0001.tei.xml").exists()
    assert not (tei_dir(root) / "LIV0001.tei.xml").exists()


def test_entities_travel_with_the_tei(tmp_path):
    root = _share(tmp_path)
    tei = tmp_path / "out" / "LIV0001_reconciled.tei.xml"
    tei.parent.mkdir(parents=True)
    tei.write_text("<TEI/>", encoding="utf-8")
    ents = tmp_path / "entities" / "LIV0001_reconciled"
    ents.mkdir(parents=True)
    (ents / "persons.csv").write_text("id;name\n", encoding="utf-8")

    ok, why = publish(tei, ents, root, "LIV0001", review=False, republish=False)
    assert ok, why
    assert (tei_dir(root) / "entities" / "LIV0001" / "persons.csv").exists()


def test_fetch_mkdir_failure_does_not_raise(tmp_path, monkeypatch):
    root = _share(tmp_path)

    def bad_mkdir(*args, **kwargs):
        raise PermissionError("permission denied")

    monkeypatch.setattr("pathlib.Path.mkdir", bad_mkdir)
    got, why = fetch(root, "LIV0001", tmp_path / "in")
    assert got is None
    assert "copy failed" in why


def test_reachable_succeeds_on_connection(monkeypatch):
    class DummySocket:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    monkeypatch.setattr("socket.create_connection", lambda *a, **k: DummySocket())
    ok, why = reachable("test.example", port=445)
    assert ok is True
    assert why == ""
    assert "test.example" not in why, "hostname should not appear in reason"


def test_reachable_reports_dns_failure_and_mentions_vpn(monkeypatch):
    def bad_dns(*a, **k):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr("socket.create_connection", bad_dns)
    ok, why = reachable("test.example", port=445)
    assert ok is False
    assert "VPN" in why
    assert "test.example" not in why, "hostname should not appear in reason"


def test_reachable_reports_timeout_and_mentions_port(monkeypatch):
    def timeout(*a, **k):
        raise TimeoutError("Connection timed out")

    monkeypatch.setattr("socket.create_connection", timeout)
    ok, why = reachable("test.example", port=445)
    assert ok is False
    assert "port" in why
    assert "445" in why
    assert "test.example" not in why, "hostname should not appear in reason"


def test_publish_leaves_no_tei_if_entities_copy_fails(tmp_path, monkeypatch):
    root = _share(tmp_path)
    tei = tmp_path / "out" / "LIV0001_reconciled.tei.xml"
    tei.parent.mkdir(parents=True)
    tei.write_text("<TEI/>", encoding="utf-8")
    ents = tmp_path / "entities" / "LIV0001_reconciled"
    ents.mkdir(parents=True)
    (ents / "persons.csv").write_text("id;name\n", encoding="utf-8")

    def bad_copytree(*a, **k):
        raise OSError("entities copy failed")

    monkeypatch.setattr("teille_sync.nas.shutil.copytree", bad_copytree)
    ok, why = publish(tei, ents, root, "LIV0001", review=False, republish=False)
    assert not ok
    assert "upload failed" in why
    tei_file = tei_dir(root) / "LIV0001.tei.xml"
    assert not tei_file.exists(), "no TEI should exist if entities copy fails"
