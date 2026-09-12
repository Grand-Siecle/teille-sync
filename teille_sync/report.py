"""What a run did, and what it lost — as Rich renderables.

Pure of console and of clock, mirroring TEIlle-douce's `report/` package:
every function here takes data that some other module already computed
(`preflight.Check`, `batch.BatchResult`, `board.Card`, a plain value) and
returns a Rich renderable. Nothing here opens a `Console`, calls `print`,
or reads a clock — `cli.py` is the only module allowed to do either, which
is what makes every function below testable by inspecting the renderable
it returns rather than capturing a terminal.

**A batch that lost documents must say so in words.** `batch_summary()`
always names `claimed`, `reclaimed`, `released` and `unwritten`, and
`batch_table()`
leaves a caption on an empty table rather than rendering a bare header —
so a batch that claimed five cards and had to hand all five back (a dead
service, an interrupt) never looks, on screen, like a batch that found
nothing to do.
"""

from collections import Counter

from rich.table import Table
from rich.text import Text

from teille_sync.board import TODO, WIP

# Import by name, not `from teille_sync.verdict import *`: these four
# strings are also the canonical display order for `status_table()`.
from teille_sync.verdict import BLOCKED, DONE, FAILED, REVIEW

STATUS_ORDER = [TODO, WIP, BLOCKED, FAILED, REVIEW, DONE]


def preflight_table(checks):
    """One row per `preflight.Check`. A failing check shows its detail
    and its remedy; a passing one shows neither — there is nothing to
    remedy, and printing one anyway would suggest there was."""
    table = Table(title="Preflight")
    table.add_column("Check")
    table.add_column("Result")
    table.add_column("Detail")
    table.add_column("Remedy")
    for check in checks:
        result = Text("ok", style="green") if check.ok else Text("FAILED", style="bold red")
        table.add_row(check.name, result, check.detail,
                      "" if check.ok else check.remedy)
    return table


def batch_table(result):
    """One row per document judged this batch: identifier, statut,
    phase, pertes, and whether it was published.

    `result.outcomes` is empty both when nothing was claimed and when
    everything claimed was released before judging (a converter refusal,
    an interrupt) — two very different situations that must not render
    as the same silent, header-only table. The caption is the difference;
    `batch_summary()` carries the rest.
    """
    table = Table(title="Batch")
    table.add_column("Identifier")
    table.add_column("Statut")
    table.add_column("Phase")
    table.add_column("Pertes", justify="right")
    table.add_column("Published")
    for identifier in sorted(result.outcomes):
        verdict = result.outcomes[identifier]
        published = result.published.get(identifier)
        published_cell = "—" if published is None else ("yes" if published else "no")
        table.add_row(identifier, verdict.status, verdict.phase or "",
                      str(verdict.losses), published_cell)
    if not result.outcomes:
        table.caption = ("no document was judged this batch — see the "
                        "summary below for what happened to the claims")
    return table


def batch_summary(result):
    """The closing block: what was claimed, reclaimed and released, and
    the batch's own `message` — the words behind an empty `batch_table`,
    when there is one. Always mentions all four counts, even at zero,
    so a batch that genuinely found nothing pending reads differently
    from one that claimed five and lost all five."""
    parts = [
        f"claimed {len(result.claimed)}" +
        (f" ({', '.join(result.claimed)})" if result.claimed else ""),
        f"reclaimed {len(result.reclaimed)}" +
        (f" ({', '.join(result.reclaimed)})" if result.reclaimed else ""),
        f"released {len(result.released)}" +
        (f" ({', '.join(result.released)})" if result.released else ""),
        # Named, never merely counted: a card the board refused belongs
        # to a document that is already on the share, and only a person
        # can close the gap. Shown at zero like the other three, so a
        # batch where every verdict landed reads differently from one
        # where five did not.
        f"unwritten {len(result.unwritten)}" +
        (f" ({', '.join(sorted(result.unwritten))})" if result.unwritten else ""),
    ]
    counts = Counter(v.status for v in result.outcomes.values())
    if counts:
        parts.append("outcomes: " + ", ".join(
            f"{n} {status}" for status, n in sorted(counts.items())))
    text = Text("; ".join(parts) + ".")
    if result.message:
        text.append("\n" + result.message, style="italic")
    return text


def dry_run_table(identifiers):
    """The documents `--dry-run` would have taken — a preview from
    `board.pending()`, never from an actual `claim()`, so it is a
    best-effort list rather than a guarantee: another machine can still
    win the race for any one of them before a real run tries."""
    table = Table(title="Would take (dry run)")
    table.add_column("Identifier")
    for identifier in identifiers:
        table.add_row(identifier)
    if not identifiers:
        table.caption = "nothing pending — the board has no cards à traiter"
    return table


def status_table(cards):
    """Counts per statut, in the board's own workflow order, plus a
    total. A status this table does not expect (a field edited by hand
    into something new) is appended rather than folded away — a count
    that silently vanished from the total would be exactly the kind of
    loss this project's reports are not allowed to hide."""
    counts = Counter(card.status for card in cards)
    table = Table(title="Board status")
    table.add_column("Statut")
    table.add_column("Count", justify="right")
    seen = set()
    for status in STATUS_ORDER:
        if status in counts:
            table.add_row(status, str(counts[status]))
            seen.add(status)
    for status in sorted(set(counts) - seen):
        table.add_row(status, str(counts[status]))
    table.add_row("Total", str(len(cards)), style="bold")
    return table


def release_summary(documents):
    """Confirmation for `teille-sync release DOC…`."""
    return Text(f"released: {', '.join(documents)}")


def ids_refresh_summary(ids, path):
    """Confirmation for `teille-sync ids refresh`. Never prints the
    board's URL or any field's raw id — only counts, which say nothing
    about the address behind them."""
    return Text(
        f"wrote {path}: project id captured, "
        f"{len(ids.get('fields', {}))} field(s), "
        f"{len(ids.get('items', {}))} item(s)")
