"""One-off migration: replace the board's `Cause` options with the twelve
that reflect what the pipeline actually emits.

The board's `Cause` field currently carries eleven options invented
before anyone had read the converter's source. `CAUSES` below is the
real list: one label for every code `teille_douce.report.record.Code`
emits (see `teille_sync.verdict.CAUSE_BY_CODE`, which this script's
labels match), plus two from elsewhere in this project — `Absent du NAS`
from the NAS layer (`teille_sync.nas.fetch`) and `Validation schéma` from
the validator — and `Autre` for everything else.

`updateProjectV2Field` replaces a single-select field's option list
*wholesale* — there is no "add one option" or "rename one option"
mutation. That is harmless today only because no card has ever had a
`Cause` value written to it. The day a batch runs and `Board.write()`
starts setting `Cause`, every option on the board becomes something a
card actually points to (by option id, minted fresh by this very
mutation), and running this again would silently detach every one of
those cards from whatever it held. So this refuses outright, loudly, the
moment it finds a single card already carrying a `Cause` value, and says
to fix the field by hand instead — a script that cannot see what a
person meant by that value has no business guessing.

Usage (from the project's Python 3.12 venv — `teille_sync.settings` needs
`tomllib`, which the system `python3` on some of this project's machines
does not have)::

    python scripts/realign_cause.py --check   # inspect only, write nothing
    python scripts/realign_cause.py           # inspect, then replace

Both paths page through the *entire* board before deciding anything is
safe. The board holds 396 cards and the underlying query
(`teille_sync.board.PENDING`) returns 100 at a time — a check that
stopped at the first page could clear a migration that destroys a
`Cause` value sitting on, say, card 150.

Do not run this against the real board without `--check` first.

**And run `teille-sync ids refresh` immediately afterwards.** The
mutation mints a new id for every option it writes, so the id file
describes a `Cause` field that no longer exists the moment this returns.
`Board._select` skips a `Cause` label it has no option for — silently,
because unlike `Status` an unmodelled Cause is a legitimate thing for the
pipeline to emit — so a stale file means every `Cause` write lands
nowhere and the whole corpus runs with a blank column.
"""

import argparse
import sys

from teille_sync import cli, exits
from teille_sync.board import PENDING
from teille_sync.settings import SettingsError, resolve

# The twelve replacements, taken verbatim from the migration plan
# (docs/superpowers/plans/2026-09-12-teille-sync.md, Task 9). Order is
# the order they will appear in the board's dropdown.
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

UPDATE_FIELD = """
mutation($f:ID!, $options:[ProjectV2SingleSelectFieldOptionInput!]!){
  updateProjectV2Field(input:{
    fieldId:$f, singleSelectOptions:$options}){
    projectV2Field{ ... on ProjectV2SingleSelectField{ id name
      options{ id name color description } } } } }
"""


class CauseInUse(Exception):
    """At least one card already carries a `Cause` value. Replacing the
    option list would mint new option ids and detach every one of those
    cards from whatever it points to now — so this is refused, and the
    offending identifiers are named so the operator knows where to look."""

    def __init__(self, identifiers):
        self.identifiers = identifiers
        shown = ", ".join(identifiers[:10])
        if len(identifiers) > 10:
            shown += ", …"
        super().__init__(
            f"{len(identifiers)} card(s) already carry a Cause value "
            f"({shown}) — edit the Cause field by hand instead of "
            f"running this script")


def cards_with_cause(board):
    """Every card identifier that already has a `Cause` value, paging
    through the whole board.

    Reuses `Board`'s own `PENDING` query and transport rather than
    inventing a second query: `PENDING` already asks for every
    single-select value on an item, tagged with the name of the field it
    belongs to — `Board._card_from_node` just keeps only Status and
    Détail, since that is all the rest of the pipeline needs. This walks
    the same pages, the same way `Board._all_cards` does (see its
    docstring for why stopping early would be wrong), and keeps `Cause`
    instead.
    """
    found = []
    cursor = None
    while True:
        answer = board.send(PENDING, {"p": board.project, "c": cursor})
        items = ((answer.get("node") or {}).get("items")) or {}
        for node in items.get("nodes") or []:
            for value in (node.get("fieldValues") or {}).get("nodes") or []:
                if ((value.get("field") or {}).get("name") == "Cause"
                        and value.get("name")):
                    title = (node.get("content") or {}).get("title") or node["id"]
                    found.append(title)
        page_info = items.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
    return found


def realign(board, *, check_only):
    """Refuse (`CauseInUse`) if any card already has a `Cause` value.

    Otherwise, unless `check_only`, replace the field's options with
    `CAUSES` and return the board's own echo of the new option list.
    `check_only` returns `None` without writing anything — the whole
    point of `--check` is that it is safe to run against the real board
    at any time.
    """
    in_use = cards_with_cause(board)
    if in_use:
        raise CauseInUse(in_use)
    if check_only:
        return None

    try:
        field_id = board.fields["Cause"]["id"]
    except KeyError:
        raise KeyError(
            "the board has no field named 'Cause' — the id file is "
            "stale, run `teille-sync ids refresh`") from None

    options = [{"name": name, "color": color, "description": description}
              for name, color, description in CAUSES]
    answer = board.send(UPDATE_FIELD, {"f": field_id, "options": options})
    field = ((answer.get("updateProjectV2Field") or {}).get("projectV2Field")) or {}
    return field.get("options") or []


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="realign_cause.py",
        description="Replace the board's Cause options with the twelve "
                    "the pipeline actually emits. Refuses if any card "
                    "already carries a Cause value.")
    parser.add_argument("--check", action="store_true",
                        help="inspect only; write nothing")
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)

    try:
        settings = resolve({}, config_file=cli._config_file())
    except SettingsError as why:
        print(f"realign_cause: {why}", file=sys.stderr)
        return exits.USAGE

    try:
        board = cli._open_board(settings)
    except cli.BoardTransportError as why:
        print(f"realign_cause: {why}", file=sys.stderr)
        return exits.MISCONFIGURED

    try:
        options = realign(board, check_only=args.check)
    except (CauseInUse, KeyError) as why:
        print(f"realign_cause: {why}", file=sys.stderr)
        return exits.MISCONFIGURED

    if args.check:
        print("realign_cause: no card carries a Cause value — "
             "safe to run without --check")
    else:
        names = ", ".join(o.get("name", "?") for o in options)
        print(f"realign_cause: Cause now offers: {names}")
        # Not a footnote. `updateProjectV2Field` minted a new id for
        # every option above, so `project-board-ids.json` now describes
        # a Cause field that no longer exists — and `Board._select`
        # *skips* a Cause label it has no option for, silently, because
        # Cause is not Status. Without this step the next run writes a
        # verdict to every card with the Cause column left blank, all
        # 396 of them, and nothing says why.
        print("")
        print("realign_cause: !! the id file is now stale — every option "
              "above has a NEW id.")
        print("realign_cause: !! run `teille-sync ids refresh` before the "
              "next `teille-sync run`,")
        print("realign_cause: !! or every Cause write will be skipped and "
              "the column will stay blank.")
    return exits.OK


if __name__ == "__main__":
    sys.exit(main())
