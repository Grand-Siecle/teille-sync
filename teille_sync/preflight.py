"""What runs before anything is claimed, fetched or written.

Eight checks, in order: VPN, NAS root, Destination, Board, Converter,
Services, Metadata, Disk. Any failure means the caller exits 3 with
nothing touched — no card moved, no file copied. `preflight()` always
returns all eight, even when an early one fails, so the operator sees
everything wrong in one pass rather than fixing it one refusal at a time.

Pure of console: this module returns `Check` objects, a later module
prints them.

Two rules run through every check below:

* **A refusal names its remedy.** "NAS root not found" sends someone
  looking in the wrong place when the truth is that the drive is mapped
  in Windows and invisible from WSL — the common case on this project's
  machines.
* **No check ever repeats the host or the UNC path**, in `detail` or in
  `remedy`. The address is configuration, never something this tool
  prints back — see `nas.py`. Checks that need to name *something* local
  name the mount point instead, which says nothing about what is behind
  it.
"""

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from shutil import disk_usage

from teille_sync import nas
from teille_sync.convert import check_services

CONVERTER_NAME = "teille-douce"

# "Archives average 30 MB" (design spec). The conversion then writes more
# than it reads: TEI XML carries the OCR text plus every enrichment
# token, modernized reading and NER span, and each document also leaves
# entity CSVs, a run manifest and an incident log alongside it — none of
# it compressed the way the source ZIP is. Budget 1x per document for the
# fetch itself and roughly 4x for everything the conversion writes on top
# of it (5x), then double the total again as margin for whatever else is
# already on the disk and because "average" undercounts the largest
# documents in the batch. 10x per document, times a full batch, is the
# number below.
ARCHIVE_AVERAGE_BYTES = 30 * 1024 * 1024
DISK_MARGIN_MULTIPLIER = 10


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str
    remedy: str


def on_wsl():
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def preflight(settings, probe=nas.reachable, which=shutil.which):
    """Run all eight checks and return them, in order.

    Every check runs regardless of what came before it: the operator
    should see everything that is wrong in one pass, not fix one thing
    per run. A check that genuinely cannot be evaluated because an
    earlier one failed (Destination needs a mounted root) says so
    honestly rather than reporting a silent pass.
    """
    return [
        _check_vpn(settings, probe),
        _check_nas_root(settings),
        _check_destination(settings),
        _check_board(settings),
        _check_converter(which),
        _check_services(settings),
        _check_metadata(settings),
        _check_disk(settings),
    ]


# -- 1. VPN ---------------------------------------------------------------

def _check_vpn(settings, probe):
    """Reachability is the only honest VPN test — see `nas.reachable`."""
    ok, reason = probe(settings.nas_host, port=445, timeout=3.0)
    mount = settings.nas_root
    if ok:
        return Check("VPN", True, "the NAS answers", "")
    detail = f"the NAS behind {mount} does not answer"
    if reason:
        detail = f"{detail} — {reason}"
    remedy = (f"bring the VPN up and retry — nothing behind {mount} is "
             f"reachable until the tunnel is")
    return Check("VPN", False, detail, remedy)


# -- 2. NAS root ------------------------------------------------------------

def _windows_drive_letter(root):
    """`/mnt/y` -> `"y"`. Anything else -> `None`."""
    parts = Path(root).parts
    if (len(parts) == 3 and parts[1] == "mnt"
            and len(parts[2]) == 1 and parts[2].isalpha()):
        return parts[2]
    return None


def _check_nas_root(settings):
    root = Path(settings.nas_root)
    if not root.is_dir():
        letter = _windows_drive_letter(root)
        if letter is not None and on_wsl():
            drive = f"{letter.upper()}:"
            return Check("NAS root", False, f"{root} does not exist",
                         f"sudo mount -t drvfs {drive} {root}")
        detail = f"{root} does not exist" if not root.exists() else f"{root} is not a directory"
        return Check("NAS root", False, detail,
                     "confirm the mount point, then mount the share there")

    archives = nas.archives_dir(root)
    if not archives.is_dir():
        return Check(
            "NAS root", False,
            f"{root} has no OCR/zip_reconciliate — this may not be the share root",
            "point nas_root at the share that holds OCR/zip_reconciliate")
    return Check("NAS root", True, f"{root} holds OCR/zip_reconciliate", "")


# -- 3. Destination ---------------------------------------------------------

def _check_destination(settings):
    root = Path(settings.nas_root)
    if not root.is_dir():
        # The NAS root check already failed on this same path — do not
        # pretend to have verified a destination under a root that is not
        # even mounted.
        return Check("Destination", False,
                     "the NAS root is not mounted, so the destination "
                     "cannot be verified",
                     "fix the NAS root check first")

    destination = nas.tei_dir(root)
    review = destination / nas.REVIEW
    try:
        destination.mkdir(parents=True, exist_ok=True)
        review.mkdir(parents=True, exist_ok=True)
        # mkdir(exist_ok=True) is silent when the directory is already
        # there, so it alone would not catch a `tei/` that exists but is
        # not writable by this machine. A probe file that is written and
        # removed is the only honest test of that.
        probe = destination / ".teille-sync-write-check"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as why:
        return Check("Destination", False,
                     f"cannot write to {destination.name}/: {why}",
                     "check write permissions on the share")
    return Check("Destination", True,
                 f"{destination.name}/ and {destination.name}/{nas.REVIEW}/ are ready",
                 "")


# -- 4. Board ---------------------------------------------------------------

def _check_board(settings):
    """The ids file only — no network call. A live GraphQL probe belongs
    to the run itself, not to a check that must stay side-effect free."""
    path = Path(settings.ids_file)
    try:
        raw = path.read_text("utf-8")
    except OSError as why:
        return Check("Board", False, f"{path} cannot be read: {why}",
                     "run `teille-sync ids refresh` to (re)create the id file")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as why:
        return Check("Board", False, f"{path} is not valid JSON: {why}",
                     "run `teille-sync ids refresh` to rebuild the id file")

    if not isinstance(data, dict):
        return Check("Board", False, f"{path} does not hold a JSON object",
                     "run `teille-sync ids refresh` to rebuild the id file")

    project_id = (data.get("project") or {}).get("id")
    fields = data.get("fields")
    if not project_id:
        return Check("Board", False, f"{path} has no project id",
                     "the id file may be stale — run `teille-sync ids refresh`")
    if not isinstance(fields, dict) or not fields:
        return Check("Board", False, f"{path} has no fields map",
                     "the id file may be stale — run `teille-sync ids refresh`")
    return Check("Board", True,
                 f"{path} carries a project id and {len(fields)} field(s)", "")


# -- 5. Converter -----------------------------------------------------------

def _converter_version(path):
    """Best-effort version string, for the *Version pipeline* field later.
    Never raises: a converter that exists but cannot answer `--version`
    is still a converter that passes this check."""
    try:
        proc = subprocess.run([path, "--version"], capture_output=True,
                              text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or proc.stderr or "").strip()


def _check_converter(which):
    path = which(CONVERTER_NAME)
    if not path:
        return Check(
            "Converter", False, f"{CONVERTER_NAME} is not on PATH",
            f"pip install -e '.[dev]' the teille-douce checkout, or make "
            f"sure this shell's PATH includes the venv that has it")
    version = _converter_version(path)
    detail = f"{CONVERTER_NAME} found at {path}"
    if version:
        detail = f"{detail} ({version})"
    return Check("Converter", True, detail, "")


# -- 6. Services --------------------------------------------------------

def _check_services(settings):
    """`check_services()` runs `teille-douce check` and reads the
    services block it prints — VieuxParler, PyHellen and the NER
    dependencies. All three phases are mandatory for this corpus, so a
    refusal here stops the batch before a single card is claimed: this
    is a gate, not a warning, and fetching 150 MB over a VPN only to
    find PyHellen down is exactly what it exists to avoid.

    The input directory it points the converter at is empty at this
    point — nothing has been fetched yet — which is why the exit code is
    not what decides. See `convert.check_services`.
    """
    input_dir = Path(settings.work_dir) / "OCR"
    try:
        ok, output = check_services(input_dir)
    except (OSError, subprocess.SubprocessError) as why:
        # Most commonly: the converter binary is not on PATH, which the
        # Converter check above already reports — but this check must
        # not crash and hide the other seven checks from the operator.
        return Check("Services", False,
                     f"could not run the converter's own preflight: {why}",
                     "install teille-douce and make sure it is on PATH")

    if ok:
        # One line, not the converter's whole report: this check passes
        # on every healthy run — the report is thirty lines wide and
        # ends with `unusable — nothing to convert`, which is true of an
        # input directory nothing has been fetched into yet and reads
        # like a failure beside a green `ok`. Verbatim is for the
        # refusal below, which is where it carries something.
        return Check("Services", True,
                     "VieuxParler, PyHellen and NER models all report up", "")

    # The converter's own words, verbatim — not a paraphrase.
    detail = output if output else "the converter's own preflight refused"
    remedy = ("read the services block above: a service that is not `up` "
             "has to be brought up — the run passes `--phases all "
             "--require-services` and would refuse anyway — and a block "
             "the report never printed means `teille-douce check -i "
             f"{input_dir}` has to be run by hand to see what it says")
    return Check("Services", False, detail, remedy)


# -- 7. Metadata --------------------------------------------------------

def _check_metadata(settings):
    """Coverage is not a gate — a document with no catalogue row still
    converts, with a header of placeholders and a warning. Only files
    that cannot be read at all are a refusal.

    Reads `settings.metadata_csv` / `settings.persons_csv` directly
    rather than inferring a location from `work_dir`. The converter's own
    config discovery walks up the parent directories from wherever it is
    run, so guessing a path here and passing a *different* one to
    `run_converter()` would make this check meaningless — verifying a
    file the run never actually reads. Checking the exact paths that
    `run_converter()` passes as `--metadata`/`--persons` is what makes a
    green check here mean something.
    """
    paths = {"metadata_csv": Path(settings.metadata_csv),
            "persons_csv": Path(settings.persons_csv)}
    unreadable = []
    for path in paths.values():
        try:
            with path.open("rb") as handle:
                handle.read(1)
        except OSError as why:
            unreadable.append(f"{path} ({why.strerror or why})")

    if unreadable:
        return Check("Metadata", False,
                     "cannot read " + ", ".join(unreadable),
                     "point --metadata/--persons (or TDSYNC_METADATA_CSV/"
                     "TDSYNC_PERSONS_CSV) at readable catalogue files — "
                     "missing rows inside them are not a problem, an "
                     "unreadable file is")
    return Check("Metadata", True,
                 f"{paths['metadata_csv']} and {paths['persons_csv']} are readable",
                 "")


# -- 8. Disk --------------------------------------------------------------

def _existing_ancestor(path):
    """The nearest directory on `path`'s line that actually exists.
    `work_dir` is only created once a batch fetches something, so at
    preflight time it — or several of its parents — may not exist yet,
    and `disk_usage()` raises on a path that is not there."""
    path = Path(path)
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return Path(path.anchor or ".")


def _check_disk(settings):
    required = settings.batch_size * ARCHIVE_AVERAGE_BYTES * DISK_MARGIN_MULTIPLIER
    target = _existing_ancestor(settings.work_dir)
    try:
        usage = disk_usage(target)
    except OSError as why:
        return Check("Disk", False, f"cannot read free space at {target}: {why}",
                     "check that the working directory's filesystem is available")

    free_mb = usage.free // (1024 * 1024)
    required_mb = required // (1024 * 1024)
    if usage.free < required:
        return Check(
            "Disk", False,
            f"{free_mb} MB free at {target}, need at least {required_mb} MB "
            f"for a batch of {settings.batch_size}",
            "free up space, or point --work-dir at a volume with more room")
    return Check("Disk", True, f"{free_mb} MB free at {target}", "")
