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

# The codes that mean the document died, as opposed to the ones that mean
# something inside it was lost while it carried on. Incidents append in
# occurrence order, so a volume that emitted `page_unusable` at page 12
# and then died of `document_failed` recorded the page loss first — and
# naming the failure after it filed a document that never converted under
# "Pages perdues".
TERMINAL_CODES = ("document_failed", "volume_unreadable", "archive_corrupt",
                  "phase_lost")

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


def _names_the_failure(ours):
    """Which of this document's incidents gets to name its failure.

    The first one carrying a terminal code, in occurrence order; the
    first incident of any kind when nothing terminal was recorded. Among
    terminal codes occurrence order is kept rather than ranked: each of
    them is a true cause of death, and inventing a hierarchy between
    "the archive was corrupt" and "the document failed" would be a guess
    where the run already said which came first.
    """
    for incident in ours:
        if incident.get("code") in TERMINAL_CODES:
            return incident
    return ours[0] if ours else {}


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
        named = _names_the_failure(ours)
        return Verdict(
            FAILED,
            cause=CAUSE_BY_CODE.get(named.get("code", ""), "Autre"),
            phase=PHASE_BY_STEP.get(named.get("step", "")),
            detail=named.get("detail") or "no detail recorded",
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
        # `read` is False when `convert.validate` could not parse a
        # report at all. That is not a schema failure — nothing said the
        # schema failed — so it does not claim to be one; it is a
        # document nobody checked, which is a reason to look rather than
        # a reason to publish it as finished.
        if not validation.get("read", True):
            said = "; ".join(errors[:3])
            return Verdict(
                REVIEW,
                cause="Autre",
                detail=("the validator's report could not be read"
                        + (f": {said}" if said else "")),
                losses=losses,
            )
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
