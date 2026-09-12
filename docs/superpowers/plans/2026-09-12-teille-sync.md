# teille-sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the batch orchestrator that claims five documents from the
GitHub Project, fetches their archives from the NAS over the VPN, runs
`teille-douce`, writes the verdict back onto the board, and publishes the TEI.

**Architecture:** A standalone package in a new repository. It never imports
`teille_douce`; it invokes the CLI as a subprocess and reads the records that
CLI already writes (`run.json`, `incidents.jsonl`, `validate --json`). Pure
decision logic (`verdict.py`) is separated from every I/O boundary (`nas.py`,
`board.py`, `convert.py`) so the part that can be silently wrong is the part
that is exhaustively tested.

**Tech Stack:** Python 3.12+, `httpx` (GraphQL), `rich` (terminal), `pytest`.
No GraphQL client library — the four queries are hand-written.

**Spec:** `docs/superpowers/specs/2026-09-12-teille-sync-design.md`

## Global Constraints

- **Repository:** `Grand-Siecle/teille-sync`, **private**. Unlike
  `TEIlle-douce` it holds operational configuration, and a private repo
  removes a whole class of leak.
- **Python 3.12+**, matching TEIlle-douce's floor.
- **The code is in English** — comments, docstrings, test names, log
  messages, CLI help. Exceptions: the board's option labels, which are
  French because the board is.
- **The NAS address never appears in a tracked file.** Not in code, not in
  tests, not in fixtures, not in a committed log. `TDSYNC_NAS_ROOT` and
  `TDSYNC_NAS_HOST` carry it; `config.example.toml` holds fictional values.
- **Every phase reports what it lost.** A counter left at zero because a
  server died must not look like a batch that had nothing to process.
  Inherited from TEIlle-douce's CLAUDE.md, and it applies here too.
- **Every fix carries a regression test** that fails before the change.
- **Name mapping, fixed once:** card title `LIV0001` ↔ archive
  `LIV0001_reconciled.zip` ↔ `teille-douce` document `LIV0001_reconciled` ↔
  output `LIV0001_reconciled.tei.xml` ↔ published `LIV0001.tei.xml`. The
  `_reconciled` suffix is an artefact of the reconciliation step and is
  dropped on publication; the identifier is what the corpus is indexed by.
- **Exit codes,** mirroring TEIlle-douce: `0` all finished, `1` some failed,
  `2` usage, `3` misconfigured or preflight refused, `130` interrupted.
- **All three annotation phases are mandatory.** Every conversion runs
  `--phases all --require-services`. Enrichment (PyHellen), modernization
  (VieuxParler) and NER are not optional for this corpus, and a document
  written without one of them is a failure, not a cheaper success. Two
  consequences carried through every task: `phase_lost` escalates to
  `Échec`, and a batch whose services are down claims nothing.

---

### Task 1: Repository scaffolding, exits and settings

**Files:**
- Create: `pyproject.toml`, `README.md`, `.gitignore`, `config.example.toml`
- Create: `teille_sync/__init__.py`, `teille_sync/exits.py`,
  `teille_sync/settings.py`
- Test: `tests/test_settings.py`

**Interfaces:**
- Produces: `exits.refuse(message, code=MISCONFIGURED)` (never returns);
  constants `OK=0, SOME_FAILED=1, USAGE=2, MISCONFIGURED=3, INTERRUPTED=130`.
  `settings.Settings` with attributes `nas_root: Path`, `nas_host: str`,
  `batch_size: int`, `reclaim_after: timedelta`, `project_url: str`,
  `ids_file: Path`, `work_dir: Path`, and `origin(name) -> str` returning
  one of `"flag" | "env" | "file" | "default"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_settings.py
import pytest
from datetime import timedelta
from pathlib import Path
from teille_sync.settings import resolve, SettingsError


def test_flag_beats_env_beats_file_beats_default(tmp_path, monkeypatch):
    cfg = tmp_path / "config.local.toml"
    cfg.write_text('nas_root = "/from/file"\nbatch_size = 3\n')
    monkeypatch.setenv("TDSYNC_NAS_ROOT", "/from/env")

    s = resolve(flags={"batch_size": 7}, config_file=cfg)

    assert s.nas_root == Path("/from/env")   # env beats file
    assert s.origin("nas_root") == "env"
    assert s.batch_size == 7                 # flag beats file
    assert s.origin("batch_size") == "flag"


def test_batch_size_rejects_zero_and_says_what_it_uses(tmp_path, monkeypatch):
    monkeypatch.setenv("TDSYNC_BATCH_SIZE", "0")
    s = resolve(flags={}, config_file=tmp_path / "absent.toml")
    assert s.batch_size == 5                 # the default
    assert "TDSYNC_BATCH_SIZE" in s.refusals[0]
    assert "5" in s.refusals[0]              # says what it used instead


def test_batch_size_as_a_flag_is_a_usage_error(tmp_path):
    with pytest.raises(SettingsError):
        resolve(flags={"batch_size": 0}, config_file=tmp_path / "absent.toml")


def test_reclaim_after_parses_hours(tmp_path, monkeypatch):
    monkeypatch.setenv("TDSYNC_RECLAIM_AFTER", "6h")
    s = resolve(flags={}, config_file=tmp_path / "absent.toml")
    assert s.reclaim_after == timedelta(hours=6)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_settings.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'teille_sync'`

- [ ] **Step 3: Write the scaffolding and the implementation**

`pyproject.toml`:

```toml
[project]
name = "teille-sync"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["httpx>=0.27", "rich>=13.7"]

[project.optional-dependencies]
dev = ["pytest>=8.0", "coverage>=7.4"]

[project.scripts]
teille-sync = "teille_sync.cli:main"

[tool.pytest.ini_options]
testpaths = ["tests"]
```

`.gitignore`:

```
__pycache__/
*.egg-info/
.venv/
venv/
config.local.toml
project-board-ids.json
work/
.coverage*
```

`config.example.toml` — fictional values, and the file says so:

```toml
# Copy to config.local.toml and put your own values in.
# config.local.toml is gitignored: the real address never lands here.
nas_root = "/mnt/example"          # Y:\  ·  /mnt/y  ·  /Volumes/Example
nas_host = "nas.example.invalid"   # the host the VPN check dials
batch_size = 5
reclaim_after = "6h"
project_url = "https://github.com/orgs/EXAMPLE-ORG/projects/0"
ids_file = "project-board-ids.json"
```

`teille_sync/exits.py`:

```python
"""The exit codes, and the one way to leave with one.

`raise SystemExit("a message")` exits 1, and 1 here means "some documents
failed" — so a refusal written that way tells a wrapper the corpus was at
fault when the truth was a missing token.

    0    every document in the batch finished
    1    partial failure: some documents failed
    2    usage error, on the command line
    3    misconfigured, or preflight refused: nothing ran
    130  interrupted
"""

import sys

OK = 0
SOME_FAILED = 1
USAGE = 2
MISCONFIGURED = 3
INTERRUPTED = 130


def refuse(message, code=MISCONFIGURED, program="teille-sync"):
    """Say why, on stderr, and leave with *code*. Never returns."""
    print(f"{program}: {message}", file=sys.stderr)
    raise SystemExit(code)
```

`teille_sync/settings.py`:

```python
"""Four layers, per setting: flag > TDSYNC_* > config file > default.

Anything from the environment is converted here and nowhere else. A bad
value from the environment is a refusal that says what the run uses
instead; the same bad value typed as a flag is a usage error, because a
person who typed it wants to know they typed it wrong.
"""

import os
import tomllib
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

DEFAULTS = {
    "nas_root": None,
    "nas_host": None,
    "batch_size": 5,
    "reclaim_after": timedelta(hours=6),
    "project_url": None,
    "ids_file": Path("project-board-ids.json"),
    "work_dir": Path("work"),
}


class SettingsError(Exception):
    """A value typed on the command line that cannot be used."""


def _positive_int(raw):
    value = int(raw)
    if value < 1:
        raise ValueError(f"must be 1 or more, not {value}")
    return value


def _duration(raw):
    text = str(raw).strip().lower()
    unit = {"h": 3600, "m": 60, "s": 1}.get(text[-1:])
    if unit is None:
        raise ValueError(f"expected a duration like '6h', not {raw!r}")
    amount = float(text[:-1])
    if amount <= 0:
        raise ValueError(f"must be positive, not {raw!r}")
    return timedelta(seconds=amount * unit)


CONVERTERS = {
    "batch_size": _positive_int,
    "reclaim_after": _duration,
    "nas_root": Path,
    "ids_file": Path,
    "work_dir": Path,
    "nas_host": str,
    "project_url": str,
}


@dataclass
class Settings:
    values: dict
    origins: dict
    refusals: list = field(default_factory=list)

    def origin(self, name):
        return self.origins[name]

    def __getattr__(self, name):
        try:
            return self.values[name]
        except KeyError:
            raise AttributeError(name) from None


def resolve(flags, config_file=None):
    from_file = {}
    if config_file and Path(config_file).is_file():
        from_file = tomllib.loads(Path(config_file).read_text("utf-8"))

    values, origins, refusals = {}, {}, []
    for name, default in DEFAULTS.items():
        convert = CONVERTERS[name]

        if flags.get(name) is not None:
            try:
                values[name], origins[name] = convert(flags[name]), "flag"
                continue
            except ValueError as why:
                # Typed by a person: tell them, do not paper over it.
                raise SettingsError(f"--{name.replace('_', '-')}: {why}") from None

        env_name = f"TDSYNC_{name.upper()}"
        for raw, where, label in ((os.environ.get(env_name), "env", env_name),
                                  (from_file.get(name), "file", f"{name} in the config file")):
            if raw is None:
                continue
            try:
                values[name], origins[name] = convert(raw), where
                break
            except ValueError as why:
                refusals.append(
                    f"{label}: {why} — using {default!r} instead")
        else:
            values[name], origins[name] = default, "default"

    return Settings(values=values, origins=origins, refusals=refusals)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_settings.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml README.md .gitignore config.example.toml teille_sync tests
git commit -m "feat: the four configuration layers, and the codes to leave with"
```

---

### Task 2: The verdict — the part that must not be silently wrong

**Files:**
- Create: `teille_sync/verdict.py`
- Test: `tests/test_verdict.py`

**Interfaces:**
- Consumes: nothing. Pure functions over already-parsed data.
- Produces:
  `decide(doc, run_manifest: dict, incidents: list[dict], validation: dict | None, fetched: bool) -> Verdict`
  where `Verdict` is a frozen dataclass with fields
  `status: str` (one of `"Bloqué" | "Échec" | "À vérifier" | "Terminé"`),
  `cause: str | None`, `phase: str | None`, `detail: str`, `losses: int`.
  Also `CAUSE_BY_CODE: dict[str, str]` and `PHASE_BY_STEP: dict[str, str]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_verdict.py
import pytest
from teille_sync.verdict import decide, CAUSE_BY_CODE, PHASE_BY_STEP


def v(doc="LIV0001_reconciled", manifest=None, incidents=(), validation=None,
      fetched=True):
    return decide(doc, manifest or {"documents": {}}, list(incidents),
                  validation, fetched)


def test_never_fetched_is_blocked_not_failed():
    r = v(fetched=False)
    assert r.status == "Bloqué"
    assert r.cause == "Absent du NAS"


def test_absent_from_the_manifest_is_blocked():
    # The run happened but never saw this document: expansion dropped it.
    r = v(manifest={"documents": {"LIV0002_reconciled": "ok"}})
    assert r.status == "Bloqué"


def test_failed_takes_its_cause_and_phase_from_the_incident():
    r = v(manifest={"documents": {"LIV0001_reconciled": "failed"}},
          incidents=[{"code": "document_failed", "document": "LIV0001_reconciled",
                      "step": "sourcedoc", "count": 1, "total": 1,
                      "detail": "lxml: premature end of data"}])
    assert r.status == "Échec"
    assert r.cause == "Document en échec"
    assert r.phase == "sourceDoc"
    assert "premature end" in r.detail


def test_ok_and_clean_and_valid_is_finished():
    r = v(manifest={"documents": {"LIV0001_reconciled": "ok"}},
          validation={"valid": True})
    assert r.status == "Terminé"
    assert r.losses == 0
    assert r.cause is None


def test_ok_but_invalid_needs_a_look_and_names_the_schema():
    r = v(manifest={"documents": {"LIV0001_reconciled": "ok"}},
          validation={"valid": False, "errors": ["ab is not allowed here"]})
    assert r.status == "À vérifier"
    assert r.cause == "Validation schéma"
    assert "ab is not allowed" in r.detail


def test_a_lost_phase_is_a_failure_even_though_the_document_converted():
    # A service that died between the probe and this document: the file
    # was written, recorded "ok", and has none of that phase's output.
    # This corpus wants all three phases, so that is an error.
    r = v(manifest={"documents": {"LIV0001_reconciled": "ok"}},
          validation={"valid": True},
          incidents=[{"code": "phase_lost", "document": "LIV0001_reconciled",
                      "step": "modernize", "count": 120, "total": 120,
                      "detail": "VieuxParler stopped answering"}])
    assert r.status == "Échec"
    assert r.cause == "Phase perdue"
    assert r.phase == "Modernisation"
    assert r.losses == 120


def test_a_lost_phase_outranks_a_schema_failure_in_the_detail():
    r = v(manifest={"documents": {"LIV0001_reconciled": "ok"}},
          validation={"valid": False, "errors": ["ab is not allowed here"]},
          incidents=[{"code": "phase_lost", "document": "LIV0001_reconciled",
                      "step": "enrich", "count": 9, "total": 9,
                      "detail": "PyHellen stopped answering"}])
    assert r.status == "Échec"
    assert "PyHellen" in r.detail


def test_ok_with_incidents_counts_every_one_of_them():
    r = v(manifest={"documents": {"LIV0001_reconciled": "ok"}},
          validation={"valid": True},
          incidents=[{"code": "page_unusable", "document": "LIV0001_reconciled",
                      "step": "sourcedoc", "count": 3, "total": 40, "detail": ""},
                     {"code": "container_failed", "document": "LIV0001_reconciled",
                      "step": "sourcedoc", "count": 2, "total": 99, "detail": ""}])
    assert r.status == "À vérifier"
    assert r.losses == 5


def test_incidents_of_another_document_are_not_counted_here():
    r = v(manifest={"documents": {"LIV0001_reconciled": "ok"}},
          validation={"valid": True},
          incidents=[{"code": "page_unusable", "document": "LIV0999_reconciled",
                      "step": "sourcedoc", "count": 7, "total": 40, "detail": ""}])
    assert r.status == "Terminé"
    assert r.losses == 0


def test_an_unknown_code_falls_back_rather_than_crashing():
    r = v(manifest={"documents": {"LIV0001_reconciled": "failed"}},
          incidents=[{"code": "something_new", "document": "LIV0001_reconciled",
                      "step": "run", "count": 1, "total": 1, "detail": "?"}])
    assert r.cause == "Autre"


def test_a_step_the_board_has_no_option_for_leaves_phase_empty():
    # The pipeline emits `run` for document-level failures; the board has
    # no such phase, and guessing one would be a lie.
    r = v(manifest={"documents": {"LIV0001_reconciled": "failed"}},
          incidents=[{"code": "document_failed", "document": "LIV0001_reconciled",
                      "step": "run", "count": 1, "total": 1, "detail": "boom"}])
    assert r.phase is None


def test_every_pipeline_code_has_a_board_label():
    for code in ("archive_corrupt", "volume_unreadable", "page_unusable",
                 "container_failed", "phase_lost", "batch_failed",
                 "retry_unanswered", "breaker_skipped", "document_failed"):
        assert code in CAUSE_BY_CODE, code
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_verdict.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'teille_sync.verdict'`

- [ ] **Step 3: Write the implementation**

```python
"""What the evidence says about one document.

Pure: a manifest, a list of incidents, a validation result, and whether
the archive ever arrived. No clock, no console, no network. This is the
one place where a wrong answer is invisible — it produces a card that
looks deliberate — so it is the one place tested exhaustively.
"""

from dataclasses import dataclass

# Every code teille_douce.report.record.Code can emit, in the board's
# words. Translation, not interpretation: one code, one label.
CAUSE_BY_CODE = {
    "archive_corrupt": "ZIP illisible",
    "volume_unreadable": "Volume illisible",
    "page_unusable": "Pages perdues",
    "container_failed": "Conteneur en échec",
    "phase_lost": "Phase perdue",
    "batch_failed": "Lot refusé",
    "retry_unanswered": "Relance sans réponse",
    "breaker_skipped": "Disjoncteur",
    "document_failed": "Document en échec",
}

# The six steps the pipeline emits, against the board's ten options. The
# four it never emits — Métadonnées, En-tête TEI, Corps, Écriture — are
# left for a person; `run` is document-level and maps to nothing.
PHASE_BY_STEP = {
    "expand": "Décompression",
    "sourcedoc": "sourceDoc",
    "enrich": "Enrichissement",
    "modernize": "Modernisation",
    "ner": "NER",
}

BLOCKED, FAILED, REVIEW, DONE = "Bloqué", "Échec", "À vérifier", "Terminé"


@dataclass(frozen=True, slots=True)
class Verdict:
    status: str
    cause: str = None
    phase: str = None
    detail: str = ""
    losses: int = 0


def _mine(incidents, doc):
    return [i for i in incidents if i.get("document") == doc]


def decide(doc, run_manifest, incidents, validation, fetched):
    """One document's verdict, from everything known about it."""
    if not fetched:
        return Verdict(BLOCKED, cause="Absent du NAS",
                       detail="the archive was never fetched")

    outcome = (run_manifest.get("documents") or {}).get(doc)
    if outcome is None:
        return Verdict(BLOCKED, cause="Autre",
                       detail="the run never saw this document")

    ours = _mine(incidents, doc)
    losses = sum(int(i.get("count") or 0) for i in ours)

    if outcome == "failed":
        first = ours[0] if ours else {}
        code = first.get("code", "")
        return Verdict(
            FAILED,
            cause=CAUSE_BY_CODE.get(code, "Autre"),
            phase=PHASE_BY_STEP.get(first.get("step", "")),
            detail=first.get("detail") or "no detail recorded",
            losses=losses,
        )

    # A lost phase outranks everything below. The document converted and
    # was recorded "ok", but a service died under it and the file has none
    # of that phase's output. This corpus wants all three phases: that is
    # a failure, not something to look at later.
    lost = next((i for i in ours if i.get("code") == "phase_lost"), None)
    if lost is not None:
        return Verdict(
            FAILED,
            cause=CAUSE_BY_CODE["phase_lost"],
            phase=PHASE_BY_STEP.get(lost.get("step", "")),
            detail=lost.get("detail") or "an annotation phase produced nothing",
            losses=losses,
        )

    if validation is not None and not validation.get("valid", False):
        errors = validation.get("errors") or []
        return Verdict(
            REVIEW,
            cause="Validation schéma",
            detail="; ".join(errors[:3]) or "validation failed",
            losses=losses,
        )

    if ours:
        first = ours[0]
        return Verdict(
            REVIEW,
            cause=CAUSE_BY_CODE.get(first.get("code", ""), "Autre"),
            phase=PHASE_BY_STEP.get(first.get("step", "")),
            detail=first.get("detail") or f"{len(ours)} incident(s)",
            losses=losses,
        )

    return Verdict(DONE)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_verdict.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add teille_sync/verdict.py tests/test_verdict.py
git commit -m "feat: the verdict, translated from the pipeline's own codes"
```

---

### Task 3: Reading what a run left behind

**Files:**
- Create: `teille_sync/convert.py`
- Test: `tests/test_convert.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  `latest_run(output_dir: Path) -> Path | None`,
  `read_manifest(run_dir: Path) -> dict`,
  `read_incidents(run_dir: Path) -> list[dict]`,
  `run_converter(docs: list[str], input_dir, output_dir, plain: bool) -> int`,
  `validate(output_dir: Path, docs: list[str]) -> dict[str, dict]` returning
  `{doc_name: {"valid": bool, "errors": [str]}}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_convert.py
import json
from pathlib import Path
from teille_sync.convert import latest_run, read_manifest, read_incidents


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_convert.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'teille_sync.convert'`

- [ ] **Step 3: Write the implementation**

```python
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


def run_converter(docs, input_dir, output_dir, plain=False):
    """One invocation for the whole batch: services are probed once, and
    `--require-services` makes a phase whose service is down fatal before
    anything is written. Output is streamed straight through.

    Returns the child's exit code. 3 means it refused — a service went
    away between the preflight and here — and nothing was written, so the
    caller releases its claims rather than blaming the documents.
    """
    argv = ["teille-douce", "run", *docs,
            "-i", str(input_dir), "-o", str(output_dir),
            "--phases", "all", "--require-services"]
    if plain or not sys.stdout.isatty():
        argv.append("--plain")
    return subprocess.run(argv, check=False).returncode


def validate(output_dir, docs):
    """`teille-douce validate --json`, reduced to one verdict per document."""
    proc = subprocess.run(
        ["teille-douce", "validate", str(output_dir), "--json"],
        capture_output=True, text=True, check=False)
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        # The validator refused before producing a report. Say nothing
        # rather than claim every document is valid.
        return {}
    verdicts = {}
    for entry in report.get("documents", report.get("files", [])):
        name = Path(entry.get("path", entry.get("file", ""))).name
        stem = name[:-len(".tei.xml")] if name.endswith(".tei.xml") else name
        verdicts[stem] = {
            "valid": bool(entry.get("valid", entry.get("ok", False))),
            "errors": [str(e) for e in (entry.get("errors") or [])],
        }
    return verdicts
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_convert.py -v`
Expected: 5 passed

- [ ] **Step 5: Verify the real shape of `validate --json`**

The parser above guesses at two possible key names. Confirm against the
real thing before trusting it:

```bash
cd <TEIlle-douce checkout> && venv/bin/teille-douce validate tei_test --json | head -40
```

Adjust `validate()` to the keys actually emitted, and add a fixture test
using that exact JSON. Do not leave both spellings in.

- [ ] **Step 6: Commit**

```bash
git add teille_sync/convert.py tests/test_convert.py
git commit -m "feat: read a run's record, damaged or not"
```

---

### Task 4: The NAS — reachability, fetch, publish

**Files:**
- Create: `teille_sync/nas.py`
- Test: `tests/test_nas.py`

**Interfaces:**
- Consumes: `Settings` from Task 1.
- Produces:
  `reachable(host: str, port=445, timeout=3.0) -> tuple[bool, str]`,
  `archives_dir(root: Path) -> Path`, `tei_dir(root: Path) -> Path`,
  `fetch(root, identifier, into: Path) -> tuple[Path | None, str]`,
  `publish(tei_path, entities_dir, root, identifier, review: bool, republish: bool) -> tuple[bool, str]`.
  Every one returns its reason as the second element; none raises on an
  expected failure.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_nas.py
import zipfile
from pathlib import Path
import pytest
from teille_sync.nas import fetch, publish, archives_dir, tei_dir


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


def test_fetch_rejects_an_archive_that_will_not_open(tmp_path):
    root = _share(tmp_path)
    bad = archives_dir(root) / "LIV0002_reconciled.zip"
    bad.write_bytes(b"not a zip at all")
    got, why = fetch(root, "LIV0002", tmp_path / "in")
    assert got is None
    assert "open" in why


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_nas.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'teille_sync.nas'`

- [ ] **Step 3: Write the implementation**

```python
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
    into.mkdir(parents=True, exist_ok=True)
    target = into / source.name
    try:
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
    except (zipfile.BadZipFile, OSError) as why:
        target.unlink(missing_ok=True)
        return None, f"the archive will not open: {why}"
    return target, ""


def publish(tei_path, entities_dir, root, identifier, review, republish):
    """Put one document on the share. Returns (True, "") or (False, reason)."""
    destination = tei_dir(root) / (REVIEW if review else "")
    target = destination / f"{identifier}.tei.xml"
    if target.exists() and not republish:
        return False, (f"{target.name} is already published — "
                       f"pass --republish to replace it")
    try:
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(tei_path, target)
        if target.stat().st_size != Path(tei_path).stat().st_size:
            target.unlink(missing_ok=True)
            return False, "the upload is a different size than the source"
        if entities_dir and Path(entities_dir).is_dir():
            where = tei_dir(root) / ENTITIES / identifier
            shutil.copytree(entities_dir, where, dirs_exist_ok=True)
    except OSError as why:
        return False, f"the upload failed: {why}"
    return True, ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_nas.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add teille_sync/nas.py tests/test_nas.py
git commit -m "feat: fetch and publish, with every refusal named"
```

---

### Task 5: The board — reading cards, claiming them, writing verdicts

**Files:**
- Create: `teille_sync/board.py`
- Test: `tests/test_board.py`

**Interfaces:**
- Consumes: `Verdict` from Task 2.
- Produces: `Board(ids: dict, transport)` with
  `pending() -> list[Card]`, `claim(card, machine, now) -> bool`,
  `release(card) -> None`, `stale(older_than, now) -> list[Card]`,
  `write(card, verdict, pages, version, now) -> None`.
  `Card` is a frozen dataclass: `identifier: str`, `item_id: str`,
  `status: str`, `detail: str`. `transport` is any callable
  `(query: str, variables: dict) -> dict` — `httpx` in production, a
  recorded list in tests.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_board.py
from datetime import datetime
import pytest
from teille_sync.board import Board, Card
from teille_sync.verdict import Verdict

IDS = {
    "project": {"id": "PVT_test"},
    "fields": {
        "Status": {"id": "F_status", "dataType": "SINGLE_SELECT",
                   "options": {"À traiter": "o_todo", "En cours": "o_wip",
                               "Bloqué": "o_blocked", "Échec": "o_failed",
                               "À vérifier": "o_review", "Terminé": "o_done"}},
        "Phase": {"id": "F_phase", "dataType": "SINGLE_SELECT",
                  "options": {"sourceDoc": "o_sd", "Décompression": "o_exp"}},
        "Cause": {"id": "F_cause", "dataType": "SINGLE_SELECT",
                  "options": {"Absent du NAS": "o_nas", "Document en échec": "o_docfail"}},
        "Détail": {"id": "F_detail", "dataType": "TEXT"},
        "Pertes": {"id": "F_losses", "dataType": "NUMBER"},
        "Pages": {"id": "F_pages", "dataType": "NUMBER"},
        "Date de traitement": {"id": "F_date", "dataType": "DATE"},
        "Version pipeline": {"id": "F_version", "dataType": "TEXT"},
    },
    "items": {"LIV0001": "I_1", "LIV0002": "I_2"},
}
NOW = datetime(2026, 9, 12, 14, 30, 0)


class Recorder:
    """A transport that answers from a script and records what it was asked."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, query, variables):
        self.calls.append((query, variables))
        return self.answers.pop(0) if self.answers else {}


def test_claim_writes_status_then_detail_and_reads_back(monkeypatch):
    card = Card(identifier="LIV0001", item_id="I_1", status="À traiter", detail="")
    t = Recorder([{}, {}, {"node": {"fieldValues": {"nodes": [
        {"text": "thinkpad · 2026-09-12T14:30:00",
         "field": {"name": "Détail"}}]}}}])
    board = Board(IDS, t)
    assert board.claim(card, machine="thinkpad", now=NOW) is True
    # status, detail, read-back
    assert len(t.calls) == 3
    assert t.calls[0][1]["value"] == {"singleSelectOptionId": "o_wip"}


def test_a_claim_another_machine_won_is_not_ours(monkeypatch):
    card = Card(identifier="LIV0001", item_id="I_1", status="À traiter", detail="")
    t = Recorder([{}, {}, {"node": {"fieldValues": {"nodes": [
        {"text": "desktop · 2026-09-12T14:29:58",
         "field": {"name": "Détail"}}]}}}])
    board = Board(IDS, t)
    assert board.claim(card, machine="thinkpad", now=NOW) is False


def test_write_sets_every_field_a_verdict_carries():
    card = Card(identifier="LIV0001", item_id="I_1", status="En cours", detail="x")
    t = Recorder([{}] * 8)
    board = Board(IDS, t)
    board.write(card, Verdict("Échec", cause="Document en échec",
                              phase="sourceDoc", detail="boom", losses=3),
                pages=40, version="teille-douce 2.0.0", now=NOW)
    sent = [c[1] for c in t.calls]
    assert {"singleSelectOptionId": "o_failed"} in [s.get("value") for s in sent]
    assert {"singleSelectOptionId": "o_docfail"} in [s.get("value") for s in sent]
    assert {"text": "boom"} in [s.get("value") for s in sent]
    assert {"number": 3} in [s.get("value") for s in sent]
    assert {"date": "2026-09-12"} in [s.get("value") for s in sent]


def test_write_skips_a_phase_the_board_has_no_option_for():
    card = Card(identifier="LIV0001", item_id="I_1", status="En cours", detail="x")
    t = Recorder([{}] * 8)
    board = Board(IDS, t)
    board.write(card, Verdict("Échec", cause="Autre", phase="Écriture",
                              detail="d", losses=0),
                pages=0, version="v", now=NOW)
    # "Écriture" is not in the test ids: it must be skipped, not sent as null
    assert all(variables.get("f") != "F_phase" for _, variables in t.calls)


def test_release_puts_the_card_back_and_clears_the_claim():
    card = Card(identifier="LIV0001", item_id="I_1", status="En cours",
                detail="thinkpad · 2026-09-12T14:30:00")
    t = Recorder([{}, {}])
    board = Board(IDS, t)
    board.release(card)
    assert t.calls[0][1]["value"] == {"singleSelectOptionId": "o_todo"}
    assert t.calls[1][1]["value"] == {"text": ""}


def test_an_unknown_identifier_is_refused_rather_than_written_nowhere():
    board = Board(IDS, Recorder([]))
    with pytest.raises(KeyError):
        board.write(Card("LIV9999", "I_missing", "En cours", ""),
                    Verdict("Terminé"), pages=1, version="v", now=NOW)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_board.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'teille_sync.board'`

- [ ] **Step 3: Write the implementation**

```python
"""The GitHub Project, as four operations.

Projects v2 offers no transaction, so claiming is optimistic: write, then
read back, and let the loser of a race skip rather than duplicate. That
is enough because the cost of losing is one document deferred, and the
cost of not checking is two machines converting the same volume.

The id file is the contract. A field renamed or recreated in the UI gets
a new id, so every lookup here is by name against the ids loaded at
startup, and a name that is missing raises rather than writing nowhere.
"""

from dataclasses import dataclass

SET_FIELD = """
mutation($p:ID!,$i:ID!,$f:ID!,$value:ProjectV2FieldValue!){
  updateProjectV2ItemFieldValue(input:{
    projectId:$p,itemId:$i,fieldId:$f,value:$value}){ projectV2Item{ id } } }
"""

READ_ITEM = """
query($i:ID!){ node(id:$i){ ... on ProjectV2Item{
  fieldValues(first:20){ nodes{
    ... on ProjectV2ItemFieldTextValue{ text field{
      ... on ProjectV2FieldCommon{ name } } } } } } } }
"""

PENDING = """
query($p:ID!,$c:String){ node(id:$p){ ... on ProjectV2{
  items(first:100, after:$c){ pageInfo{ hasNextPage endCursor }
    nodes{ id content{ ... on DraftIssue{ title } }
      fieldValues(first:20){ nodes{
        ... on ProjectV2ItemFieldSingleSelectValue{ name field{
          ... on ProjectV2FieldCommon{ name } } }
        ... on ProjectV2ItemFieldTextValue{ text field{
          ... on ProjectV2FieldCommon{ name } } } } } } } } } }
"""

TODO, WIP = "À traiter", "En cours"


@dataclass(frozen=True, slots=True)
class Card:
    identifier: str
    item_id: str
    status: str
    detail: str


class Board:
    def __init__(self, ids, transport):
        self.ids = ids
        self.send = transport
        self.project = ids["project"]["id"]
        self.fields = ids["fields"]

    # -- writing ---------------------------------------------------------

    def _field(self, name):
        try:
            return self.fields[name]
        except KeyError:
            raise KeyError(
                f"the board has no field named {name!r} — the id file is "
                f"stale, run `teille-sync ids refresh`") from None

    def _set(self, item_id, field_name, value):
        self.send(SET_FIELD, {"p": self.project, "i": item_id,
                              "f": self._field(field_name)["id"],
                              "value": value})

    def _select(self, item_id, field_name, label):
        """Write a single-select. A label the board has no option for is
        skipped: the pipeline emits steps the board never modelled, and
        inventing an option would be a guess."""
        options = self._field(field_name).get("options", {})
        if label is None or label not in options:
            return False
        self._set(item_id, field_name, {"singleSelectOptionId": options[label]})
        return True

    def claim(self, card, machine, now):
        """Take a card. True if it is ours after the read-back."""
        stamp = f"{machine} · {now.isoformat(timespec='seconds')}"
        self._select(card.item_id, "Status", WIP)
        self._set(card.item_id, "Détail", {"text": stamp})
        answer = self.send(READ_ITEM, {"i": card.item_id})
        nodes = (((answer.get("node") or {}).get("fieldValues") or {})
                 .get("nodes") or [])
        for node in nodes:
            if (node.get("field") or {}).get("name") == "Détail":
                return node.get("text") == stamp
        return False

    def release(self, card):
        """Put a claimed card back, and clear the claim with it. An empty
        Détail is what makes a released card indistinguishable from one
        never taken."""
        self._select(card.item_id, "Status", TODO)
        self._set(card.item_id, "Détail", {"text": ""})

    def write(self, card, verdict, pages, version, now):
        if card.identifier not in self.ids.get("items", {}):
            raise KeyError(f"{card.identifier} has no card on the board")
        self._select(card.item_id, "Status", verdict.status)
        self._select(card.item_id, "Cause", verdict.cause)
        self._select(card.item_id, "Phase", verdict.phase)
        self._set(card.item_id, "Détail", {"text": verdict.detail[:900]})
        self._set(card.item_id, "Pertes", {"number": verdict.losses})
        self._set(card.item_id, "Pages", {"number": pages})
        self._set(card.item_id, "Date de traitement",
                  {"date": now.date().isoformat()})
        self._set(card.item_id, "Version pipeline", {"text": version})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_board.py -v`
Expected: 6 passed

- [ ] **Step 5: Add `pending()` and `stale()` with their tests**

Both page through `PENDING`, build `Card`s, and filter on the `Status`
and `Détail` values. Write the test first, with a two-page recorded
answer so pagination is exercised — a first page of 100 that stops at the
first page is the bug this catches.

- [ ] **Step 6: Commit**

```bash
git add teille_sync/board.py tests/test_board.py
git commit -m "feat: claim, release and write, with the race decided by a read-back"
```

---

### Task 6: Preflight — the seven refusals

**Files:**
- Create: `teille_sync/preflight.py`
- Test: `tests/test_preflight.py`

**Interfaces:**
- Consumes: `nas.reachable`, `Settings`, `convert`.
- Produces: `Check` (frozen dataclass: `name: str`, `ok: bool`,
  `detail: str`, `remedy: str`), and
  `preflight(settings, probe=nas.reachable, which=shutil.which) -> list[Check]`.
  Pure of console: it returns checks, the reporter prints them.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_preflight.py
from pathlib import Path
from teille_sync.preflight import preflight
from teille_sync.settings import Settings


def _settings(**over):
    values = {"nas_root": Path("/nope"), "nas_host": "nas.example.invalid",
              "batch_size": 5, "reclaim_after": None, "project_url": "u",
              "ids_file": Path("ids.json"), "work_dir": Path("work")}
    values.update(over)
    return Settings(values=values,
                    origins={k: "default" for k in values}, refusals=[])


def _by(checks, name):
    return next(c for c in checks if c.name == name)


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_preflight.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'teille_sync.preflight'`

- [ ] **Step 3: Write the implementation**

Eight checks in order — VPN, NAS root, Destination, Board, Converter,
**Services**, Metadata, Disk — each returning a `Check`. The three rules
that the tests pin down:

0. **Services are a gate, not a warning.** `check_services()` from Task 3
   wraps `teille-douce check --strict`; its output is carried verbatim
   into `Check.detail` so the operator reads the converter's own words.
   The remedy names the two services and says the run would have used
   `--phases all --require-services` anyway, so there is no point
   claiming cards.

1. **A refusal names its remedy.** "NAS root not found" sends someone
   looking in the wrong place when the truth is that `Y:` is mapped in
   Windows and invisible from WSL. Detect WSL (`/proc/version` contains
   `microsoft`) and a `/mnt/<letter>` root, and print
   `sudo mount -t drvfs Y: /mnt/y`.
2. **No check ever repeats the host or the UNC path.** The VPN check says
   "the NAS does not answer" and names the *mount point*, which is local.

```python
def on_wsl():
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_preflight.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add teille_sync/preflight.py tests/test_preflight.py
git commit -m "feat: seven checks that refuse before anything is touched"
```

---

### Task 7: The batch — claim, convert, judge, publish, release

**Files:**
- Create: `teille_sync/batch.py`
- Test: `tests/test_batch.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `run_batch(settings, board, now, republish=False, keep=False) -> BatchResult`
  with `BatchResult.outcomes: dict[str, Verdict]`,
  `.published: dict[str, bool]`, `.exit_code: int`.

- [ ] **Step 1: Write the failing test**

The tests that matter here are the ones about what happens when something
goes wrong halfway. Write all of them:

```python
def test_a_batch_takes_exactly_batch_size_cards(...)
def test_a_card_lost_to_another_machine_is_skipped_and_the_next_taken(...)
def test_an_archive_absent_from_the_share_blocks_that_card_only(...)
def test_the_other_four_are_still_converted_when_one_is_blocked(...)
def test_an_interrupt_releases_every_claimed_but_unconverted_card(...)
def test_a_publication_refused_keeps_the_status_and_says_so_in_the_detail(...)
def test_a_failed_document_keeps_its_local_sources(...)
def test_a_finished_document_has_its_sources_deleted(...)
def test_keep_disables_deletion_for_everything(...)
def test_stale_claims_older_than_reclaim_after_are_taken_back(...)
def test_the_exit_code_is_one_when_any_document_failed(...)
def test_nothing_is_claimed_when_preflight_refused(...)
def test_a_service_down_at_preflight_claims_nothing_at_all(...)
def test_require_services_refusing_releases_every_claim(...)
def test_a_released_batch_is_not_five_failures_on_the_board(...)
```

The last three are the ones worth writing first. When the converter exits
3 — `--require-services` refused, nothing written — the five cards must go
back to `À traiter`, and the board must show no `Échec` at all. Five cards
saying a document failed, when no document was ever opened, is the same
lie as a loss counter left at zero by a dead server.

Each uses the fake share from Task 4, the `Recorder` transport from Task
5, and a stub `run_converter` that writes a chosen `run.json`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_batch.py -v`
Expected: all FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

The loop is the spec's steps 1–7 in order. Two rules the tests pin:

- **Publication happens before the final status is written**, so a card
  never claims `Terminé` for a document that never reached the share.
  When publication fails, the computed status is still written, the
  reason is appended to `Détail`, and the batch exits non-zero.
- **`KeyboardInterrupt` is caught around the whole batch**, releases every
  card claimed but not yet judged, and re-raises so the CLI exits 130.
- **Exit code 3 from the converter releases, it does not blame.**
  `--require-services` refused before writing anything, so every claim
  goes back to `À traiter` and the batch exits 3 naming the service.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_batch.py -v`

- [ ] **Step 5: Commit**

```bash
git add teille_sync/batch.py tests/test_batch.py
git commit -m "feat: a batch of five, and every way one of them can go wrong"
```

---

### Task 8: The command line and what the terminal shows

**Files:**
- Create: `teille_sync/cli.py`, `teille_sync/report.py`
- Test: `tests/test_cli.py`, `tests/test_report.py`

**Interfaces:**
- Produces: `main(argv=None) -> int`; `report.preflight_table(checks)`,
  `report.batch_table(result)` — both return Rich renderables and touch
  no console, following TEIlle-douce's split where only `dashboard.py`
  owns a console.

Surface:

```
teille-sync run [--batches N | --until-done] [--batch-size N]
                [--republish] [--keep] [--reclaim-after 6h]
                [--dry-run] [-v | -q] [--plain]
teille-sync check          # preflight only, writes nothing
teille-sync status         # what the board says: counts per statut
teille-sync release DOC…   # hand a stuck card back to À traiter
teille-sync ids refresh    # rebuild project-board-ids.json from the board
```

- [ ] **Step 1–5:** test-first as above. The CLI test that matters:
  `--dry-run` claims nothing, copies nothing, and prints the five
  documents it would have taken.

- [ ] **Step 6: Commit**

```bash
git add teille_sync/cli.py teille_sync/report.py tests/test_cli.py tests/test_report.py
git commit -m "feat: the command line, and a batch you can read"
```

---

### Task 9: Realign the board's Cause field, and the README

**Files:**
- Create: `scripts/realign_cause.py`, `README.md` (fill in)

**Interfaces:**
- Consumes: the ids file, `board.Board`.

- [ ] **Step 1: Write the script**

`updateProjectV2Field` replaces a single-select's options wholesale. No
card uses `Cause` yet, so nothing is lost — **and this is only true
before the first batch runs.** The script refuses if any card has a
`Cause` value, and says to do it by hand instead.

The twelve options, with their colours:

```python
CAUSES = [
    ("Absent du NAS",        "ORANGE", "Le fichier n'est pas sur le partage."),
    ("ZIP illisible",        "ORANGE", "archive_corrupt — archive corrompue ou tronquée."),
    ("Volume illisible",     "RED",    "volume_unreadable."),
    ("Pages perdues",        "YELLOW", "page_unusable — des pages n'ont pas pu être lues."),
    ("Conteneur en échec",   "YELLOW", "container_failed."),
    ("Phase perdue",         "RED",    "phase_lost — une phase entière n'a rien produit."),
    ("Lot refusé",           "GRAY",   "batch_failed — un service a refusé un lot."),
    ("Relance sans réponse", "GRAY",   "retry_unanswered."),
    ("Disjoncteur",          "GRAY",   "breaker_skipped — le disjoncteur a coupé."),
    ("Document en échec",    "RED",    "document_failed."),
    ("Validation schéma",    "RED",    "RelaxNG, Schematron ou contrôle Python."),
    ("Autre",                "PINK",   "Voir le champ Détail."),
]
```

- [ ] **Step 2: Run it against the real board, then verify**

```bash
python scripts/realign_cause.py --check     # refuses if any card uses Cause
python scripts/realign_cause.py
python -c "..."                             # re-read and print the options
```

- [ ] **Step 3: Write the README**

What the tool does, the two environment variables (with fictional
values), how to mount the share on each platform, the four commands, and
the one paragraph that matters: **what to do when a batch dies halfway** —
`teille-sync release LIV0044 LIV0045`, then run again.

- [ ] **Step 4: Commit**

```bash
git add scripts/realign_cause.py README.md
git commit -m "feat: realign Cause onto the codes the pipeline emits"
```

---

## Self-review notes

**Spec coverage.** Preflight's seven checks → Task 6. Claim, dead claims,
interrupt → Tasks 5 and 7. Fetch's four failure cases → Task 4. One
invocation for the batch → Task 3. The verdict table → Task 2. Publication
and its refusal → Task 4, sequenced in Task 7. Clean-up and the closing
table → Tasks 7 and 8. The board contract and the Cause realignment →
Tasks 5 and 9. Configuration and the address → Task 1.

**Two things this plan does not settle,** flagged rather than guessed:

1. **`validate --json`'s real shape** is guessed in Task 3 and must be
   confirmed against the running CLI (Task 3, Step 5) before the parser is
   trusted. Every other parser here reads a format this plan verified.
2. **`Pages`** is specified as "the TEI's surface count, or the archive's
   XML count". Which XPath gives the first is left to Task 7's
   implementer; counting `.xml` entries in the archive is unambiguous and
   is the fallback.
