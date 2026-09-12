"""Running the converter, and reading what it left behind.

Everything here tolerates a damaged record. A run that was interrupted in
its fourth hour leaves a half-written manifest, and refusing to read it
would throw away the twenty documents that did finish.
"""

import json
import subprocess
import sys
from pathlib import Path

RUNS = Path(".teille-douce") / "runs"


def latest_run(output_dir):
    """The newest run directory, or None. Names are zero-padded, so a
    lexical sort is a chronological one."""
    where = Path(output_dir) / RUNS
    try:
        dirs = sorted(d for d in where.iterdir() if d.is_dir())
    except OSError:
        return None
    return dirs[-1] if dirs else None


def read_manifest(run_dir):
    """Read the run manifest, returning an empty dict if it is damaged."""
    try:
        return json.loads((Path(run_dir) / "run.json").read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def read_incidents(run_dir):
    """One incident per line. A damaged line is skipped, not fatal."""
    path = Path(run_dir) / "incidents.jsonl"
    out = []
    try:
        text = path.read_text("utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def check_services(input_dir):
    """The converter's own preflight, strict. Returns (ok, output).

    Not a re-probe: teille_douce/preflight.py runs the same checks the run
    does, on purpose, "because a preflight that disagreed with the run"
    would be worse than none. It covers VieuxParler, PyHellen *and* the
    NER dependencies, which have no service to dial.
    """
    proc = subprocess.run(
        ["teille-douce", "check", "-i", str(input_dir), "--strict"],
        capture_output=True, text=True, check=False)
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def run_converter(docs, input_dir, output_dir, metadata_csv, persons_csv,
                  plain=False):
    """One invocation for the whole batch: services are probed once, and
    `--require-services` makes a phase whose service is down fatal before
    anything is written. Output is streamed straight through.

    `metadata_csv` and `persons_csv` are required, not defaulted: the
    converter's own config discovery walks up the parent directories from
    wherever it is run, so a sync invoked from anywhere but the
    TEIlle-douce checkout would otherwise have it looking somewhere this
    tool's own Metadata preflight check never verified — a green check,
    then placeholder headers fifteen minutes later. Passing both
    explicitly is what closes that gap; an optional parameter here would
    just reopen it under a different name.

    Returns the child's exit code. 3 means it refused — a service went
    away between the preflight and here — and nothing was written, so the
    caller releases its claims rather than blaming the documents.
    """
    argv = ["teille-douce", "run", *docs,
            "-i", str(input_dir), "-o", str(output_dir),
            "--metadata", str(metadata_csv), "--persons", str(persons_csv),
            "--phases", "all", "--require-services"]
    if plain or not sys.stdout.isatty():
        argv.append("--plain")
    return subprocess.run(argv, check=False).returncode


def validate(output_dir, docs):
    """`teille-douce validate --json`, reduced to one verdict per document.

    Validates only the files for documents in the `docs` batch.
    If no document files exist, returns {} without invoking the validator.

    The shape, confirmed against the running CLI rather than guessed:

        {"applied": ["python invariants", "teille-douce.rng", "…svrl.xsl"],
         "files":   [{"path": "tei_out/LIV0044_reconciled.tei.xml",
                      "errors": ["teille-douce.rng L27663: Did not expect …",
                                 "Schematron: Element \"blockquote\" is not …"],
                      "warnings": []}],
         "errors":  2}

    There is **no `valid` key**: a document is valid when its `errors` list
    is empty. Errors are plain strings. The command exits 1 when anything
    failed and 0 when everything passed.
    """
    output_dir = Path(output_dir)

    # Build list of file paths that actually exist
    file_paths = []
    for doc in docs:
        path = output_dir / f"{doc}.tei.xml"
        if path.exists():
            file_paths.append(str(path))

    # If no files exist, nothing to validate
    if not file_paths:
        return {}

    argv = ["teille-douce", "validate"] + file_paths + ["--json"]
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        # The validator refused before producing a report. Say nothing
        # rather than claim every document is valid.
        return {}
    verdicts = {}
    for entry in report.get("files") or []:
        stem = Path(entry.get("path") or "").name.removesuffix(".tei.xml")
        if not stem:
            continue
        errors = [str(e) for e in (entry.get("errors") or [])]
        verdicts[stem] = {"valid": not errors, "errors": errors}
    return verdicts
