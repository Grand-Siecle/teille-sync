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


# The three services `teille-douce check` reports on, as (name, endpoint)
# — the two halves of the middle column of its own services block:
#
#     services   VieuxParler modernization                            up
#                PyHellen    enrichment                          refused
#                NER models  entity recognition                       up
#
# Names and endpoints are `teille_douce.preflight.Service`'s own; the
# state is whatever the converter wrote flush right on that row.
SERVICES = (
    ("VieuxParler", "modernization"),
    ("PyHellen", "enrichment"),
    ("NER models", "entity recognition"),
)


def service_states(output):
    """`{"PyHellen": "refused", …}` for every service row found.

    A service the report never mentions is simply absent from the
    result — the caller decides what a missing row means, and for
    `check_services` below it means refusal.
    """
    states = {}
    for line in output.splitlines():
        for name, endpoint in SERVICES:
            if name in states:
                continue
            at_name = line.find(name)
            if at_name < 0:
                continue
            at_endpoint = line.find(endpoint, at_name + len(name))
            if at_endpoint < 0:
                continue
            state = line[at_endpoint + len(endpoint):].strip()
            if state:
                states[name] = state
    return states


def _is_up(state):
    """`up`, with or without the parenthesised detail the report may
    append. Every other state — `refused`, `not probed`, `not asked
    for`, `missing (torch)` — is not up."""
    return state.split("(")[0].strip() == "up"


def check_services(input_dir):
    """The converter's own preflight, read rather than exited on.
    Returns (ok, output), `ok` only when all three services say `up`.

    Not a re-probe: teille_douce/preflight.py runs the same checks the run
    does, on purpose, "because a preflight that disagreed with the run"
    would be worse than none. It covers VieuxParler, PyHellen *and* the
    NER dependencies, which have no service to dial.

    **`--strict` is deliberately not passed, and the exit code is
    deliberately ignored.** This runs before anything is fetched, so the
    input directory is empty on every fresh machine, and an empty input
    is `unusable — nothing to convert`: exit 3 with all three services
    up. Gating on that exit code refused every clean run and sent the
    operator to restart two services that were already answering. What
    this gate is for is the services, so the services block is what it
    reads.

    A report whose three service rows cannot all be found is a refusal,
    not a pass: a gate that passes because it failed to parse would send
    150 MB over the VPN to a converter nobody checked. The child's whole
    output travels back either way, so preflight still shows the
    converter's own words.
    """
    proc = subprocess.run(
        ["teille-douce", "check", "-i", str(input_dir)],
        capture_output=True, text=True, check=False)
    output = (proc.stdout or "") + (proc.stderr or "")

    states = service_states(output)
    unread = [name for name, _ in SERVICES if name not in states]
    if unread:
        note = ("the converter's own services report could not be read — "
                f"no row for {', '.join(unread)}")
        return False, f"{output.rstrip()}\n{note}" if output.strip() else note
    return all(_is_up(states[name]) for name, _ in SERVICES), output


def run_converter(docs, input_dir, output_dir, metadata_csv, persons_csv,
                  entities_dir, plain=False):
    """One invocation for the whole batch: services are probed once, and
    `--require-services` makes a phase whose service is down fatal before
    anything is written. Output is streamed straight through.

    `metadata_csv`, `persons_csv` and `entities_dir` are required, not
    defaulted: the converter's own config discovery walks up the parent
    directories from wherever it is run, so a sync invoked from anywhere
    but the TEIlle-douce checkout would otherwise have it looking
    somewhere this tool's own Metadata preflight check never verified — a
    green check, then placeholder headers fifteen minutes later. The same
    trap catches `entities_dir` on its own: a `TDOUCE_ENTITIES_DIR` or a
    `paths.entities` in some parent directory's TOML silently relocates
    the NER entity CSVs, and `nas.publish()` skips a missing entities
    directory without complaint — a `Terminé` card whose entities never
    left the machine. Passing all three explicitly is what closes that
    gap; an optional parameter here would just reopen it under a
    different name.

    Returns the child's exit code. 3 means it refused — a service went
    away between the preflight and here — and nothing was written, so the
    caller releases its claims rather than blaming the documents.
    """
    argv = ["teille-douce", "run", *docs,
            "-i", str(input_dir), "-o", str(output_dir),
            "--metadata", str(metadata_csv), "--persons", str(persons_csv),
            "--entities", str(entities_dir),
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

    What comes back carries `read`, which the validator's own report does
    not: `True` when this function parsed a report about that document,
    `False` when it could not parse one at all. A caller that cannot tell
    those apart treats an unvalidated document as a valid one.
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
        # The validator refused before producing a report. Answering
        # `{}` here was indistinguishable from "there was nothing to
        # validate", and `verdict.decide` reads a missing entry as "no
        # opinion" — so a document nobody validated came out `Terminé`,
        # published, with an empty Détail. That is the opposite of
        # saying nothing. Every document that *had* a file to validate
        # gets an explicit unread verdict instead, and `read: False` is
        # what tells it apart from a document the validator rejected.
        said = (proc.stderr or proc.stdout or "").strip().splitlines()
        reason = said[-1].strip() if said else "the validator produced no report"
        return {Path(path).name.removesuffix(".tei.xml"):
                {"valid": False, "read": False, "errors": [reason]}
                for path in file_paths}
    verdicts = {}
    for entry in report.get("files") or []:
        stem = Path(entry.get("path") or "").name.removesuffix(".tei.xml")
        if not stem:
            continue
        errors = [str(e) for e in (entry.get("errors") or [])]
        verdicts[stem] = {"valid": not errors, "read": True, "errors": errors}
    return verdicts
