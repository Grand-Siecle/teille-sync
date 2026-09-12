"""The share, and the tunnel to it.

Nothing here raises on an expected failure — an absent archive, a dropped
copy, a file already published are all answers, and the batch has to keep
its promise to the four other documents.

The address is never logged. Callers print the mount point they were
given, which is a local path and says nothing about the host behind it.
"""

import shutil
import socket
import zipfile
from pathlib import Path

ARCHIVES = Path("OCR") / "zip_reconciliate"
TEI = Path("tei")
REVIEW = "_a_verifier"
ENTITIES = "entities"
SUFFIX = "_reconciled"


def reachable(host, port=445, timeout=3.0):
    """Is the share's host answering? The only honest VPN test: a tunnel
    that is up but has no route passes an interface check and fails one
    step later."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, ""
    except socket.gaierror:
        return False, "the NAS host does not resolve — is the VPN up?"
    except (TimeoutError, OSError) as why:
        return False, f"the NAS does not answer on port {port}: {why}"


def archives_dir(root):
    return Path(root) / ARCHIVES


def tei_dir(root):
    return Path(root) / TEI


def fetch(root, identifier, into):
    """Bring one archive over. Returns (local path, "") or (None, reason)."""
    source = archives_dir(root) / f"{identifier}{SUFFIX}.zip"
    if not source.is_file():
        return None, f"{identifier} is not on the share"

    into = Path(into)
    target = into / source.name
    try:
        into.mkdir(parents=True, exist_ok=True)
        expected = source.stat().st_size
        shutil.copyfile(source, target)
    except OSError as why:
        return None, f"the copy failed: {why}"

    if target.stat().st_size != expected:
        target.unlink(missing_ok=True)
        return None, ("the copy is a different size than the source — "
                      "the connection dropped mid-transfer")
    try:
        with zipfile.ZipFile(target) as zf:
            zf.namelist()
    except (zipfile.BadZipFile, EOFError, OSError) as why:
        target.unlink(missing_ok=True)
        return None, f"the archive will not open: {why}"
    return target, ""


def published_at(root, identifier):
    """The two places one document can sit on the share, finished first.

    A document belongs in exactly one of them, and which one depends on
    a verdict that can change between runs: `Terminé` on Monday and `À
    vérifier` on Tuesday used to leave a copy in each, with the card
    describing only the second. Both are checked before either is
    written.
    """
    return (tei_dir(root) / f"{identifier}.tei.xml",
            tei_dir(root) / REVIEW / f"{identifier}.tei.xml")


def publish(tei_path, entities_dir, root, identifier, review, republish):
    """Put one document on the share. Returns (True, "") or (False, reason)."""
    destination = tei_dir(root) / (REVIEW if review else "")
    target = destination / f"{identifier}.tei.xml"
    # Both locations, not just the one about to be written: see
    # `published_at`. `--republish` removes the copy in the other folder
    # rather than adding a second one beside it.
    existing = [path for path in published_at(root, identifier) if path.exists()]
    if existing and not republish:
        where = ", ".join(
            str(path.relative_to(tei_dir(root)).parent) if path.parent != tei_dir(root)
            else str(tei_dir(root).name) for path in existing)
        return False, (f"{identifier}.tei.xml is already published in "
                       f"{where} — pass --republish to replace it")
    try:
        destination.mkdir(parents=True, exist_ok=True)
        for stale in existing:
            # Only the copy this publish is not about to overwrite: the
            # target itself is replaced by the copy below, and unlinking
            # it first would lose the published document if that copy
            # then failed.
            if stale != target:
                stale.unlink(missing_ok=True)
        # Copy entities first; TEI is the marker of completion.
        if entities_dir and Path(entities_dir).is_dir():
            where = tei_dir(root) / ENTITIES / identifier
            shutil.copytree(entities_dir, where, dirs_exist_ok=True)
        # TEI last: if this succeeds, the document is fully published.
        shutil.copyfile(tei_path, target)
        if target.stat().st_size != Path(tei_path).stat().st_size:
            target.unlink(missing_ok=True)
            return False, ("the upload is a different size than the source — "
                          "the connection dropped mid-transfer")
    except OSError as why:
        return False, f"the upload failed: {why}"
    return True, ""
