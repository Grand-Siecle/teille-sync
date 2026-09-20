import shutil
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


# -- one document, one place on the share ------------------------------------
#
# The refusal used to be keyed on the destination it was about to write, so
# a re-run that changed its mind published to the other folder and left the
# first copy behind: `tei/X.tei.xml` from run 1 and
# `tei/_a_verifier/X.tei.xml` from run 2, with the card describing only
# one of them. Both directions of that split are refused.

def _local_tei(tmp_path, text="<TEI/>"):
    tei = tmp_path / "out" / "LIV0001_reconciled.tei.xml"
    tei.parent.mkdir(parents=True, exist_ok=True)
    tei.write_text(text, encoding="utf-8")
    return tei


def test_a_document_already_finished_is_not_republished_into_review(tmp_path):
    root = _share(tmp_path)
    (tei_dir(root) / "LIV0001.tei.xml").write_text("run 1", encoding="utf-8")
    tei = _local_tei(tmp_path)

    ok, why = publish(tei, None, root, "LIV0001", review=True, republish=False)

    assert not ok
    assert "--republish" in why
    assert not (tei_dir(root) / "_a_verifier" / "LIV0001.tei.xml").exists()
    assert (tei_dir(root) / "LIV0001.tei.xml").read_text() == "run 1"


def test_a_document_already_in_review_is_not_republished_as_finished(tmp_path):
    root = _share(tmp_path)
    review = tei_dir(root) / "_a_verifier"
    review.mkdir(parents=True)
    (review / "LIV0001.tei.xml").write_text("run 1", encoding="utf-8")
    tei = _local_tei(tmp_path)

    ok, why = publish(tei, None, root, "LIV0001", review=False, republish=False)

    assert not ok
    assert "--republish" in why
    assert not (tei_dir(root) / "LIV0001.tei.xml").exists()
    assert (review / "LIV0001.tei.xml").read_text() == "run 1"


def test_republish_moves_a_finished_document_into_review_without_leaving_a_copy(
        tmp_path):
    root = _share(tmp_path)
    (tei_dir(root) / "LIV0001.tei.xml").write_text("run 1", encoding="utf-8")
    tei = _local_tei(tmp_path, "run 2")

    ok, why = publish(tei, None, root, "LIV0001", review=True, republish=True)

    assert ok, why
    assert (tei_dir(root) / "_a_verifier" / "LIV0001.tei.xml").read_text() == "run 2"
    assert not (tei_dir(root) / "LIV0001.tei.xml").exists()


def test_republish_moves_a_review_document_into_tei_without_leaving_a_copy(
        tmp_path):
    root = _share(tmp_path)
    review = tei_dir(root) / "_a_verifier"
    review.mkdir(parents=True)
    (review / "LIV0001.tei.xml").write_text("run 1", encoding="utf-8")
    tei = _local_tei(tmp_path, "run 2")

    ok, why = publish(tei, None, root, "LIV0001", review=False, republish=True)

    assert ok, why
    assert (tei_dir(root) / "LIV0001.tei.xml").read_text() == "run 2"
    assert not (review / "LIV0001.tei.xml").exists()


def test_the_refusal_says_which_of_the_two_folders_already_holds_it(tmp_path):
    root = _share(tmp_path)
    review = tei_dir(root) / "_a_verifier"
    review.mkdir(parents=True)
    (review / "LIV0001.tei.xml").write_text("run 1", encoding="utf-8")
    tei = _local_tei(tmp_path)

    _, why = publish(tei, None, root, "LIV0001", review=False, republish=False)

    assert "_a_verifier" in why


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

    real_copyfile = shutil.copyfile

    def bad_copyfile(src, dst, *a, **k):
        if "entities" in Path(dst).parts:
            raise OSError("entities copy failed")
        return real_copyfile(src, dst, *a, **k)

    monkeypatch.setattr("teille_sync.nas.shutil.copyfile", bad_copyfile)
    ok, why = publish(tei, ents, root, "LIV0001", review=False, republish=False)
    assert not ok
    assert "upload failed" in why
    tei_file = tei_dir(root) / "LIV0001.tei.xml"
    assert not tei_file.exists(), "no TEI should exist if entities copy fails"


# A share mounted over drvfs (a Windows network drive under WSL) copies
# bytes but refuses chmod and utime with EPERM. Publishing used to go
# through shutil.copytree, which sets metadata as well and folds that
# refusal into a shutil.Error; the publish then aborted before the TEI,
# leaving the entity CSVs on the share, no TEI beside them, and a card
# that read `Terminé`. Bytes arriving is what publishing is about.
def test_publish_survives_a_share_that_refuses_metadata(tmp_path, monkeypatch):
    root = _share(tmp_path)
    tei = tmp_path / "out" / "LIV0001_reconciled.tei.xml"
    tei.parent.mkdir(parents=True)
    tei.write_text("<TEI/>", encoding="utf-8")
    ents = tmp_path / "entities" / "LIV0001_reconciled"
    ents.mkdir(parents=True)
    (ents / "persons.csv").write_text("id;name\n", encoding="utf-8")

    def refuse(*a, **k):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr("shutil.copystat", refuse)
    monkeypatch.setattr("shutil.copymode", refuse)

    ok, why = publish(tei, ents, root, "LIV0001", review=False, republish=False)
    assert ok, why
    assert (tei_dir(root) / "LIV0001.tei.xml").exists()
    assert (tei_dir(root) / "entities" / "LIV0001" / "persons.csv").exists()
