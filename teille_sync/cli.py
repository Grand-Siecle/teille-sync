"""The command line: the console, the clock, and `sys.exit` all live here.

`report.py` is pure — Rich renderables, no console, no clock — precisely
so that this module can own both without splitting that ownership across
two files. Every `_cmd_*` function below takes the data it needs as
arguments (never the raw `argparse.Namespace`, except in `_dispatch`,
which only unpacks it), builds a renderable via `report.py`, and prints it
through the one `Console` `main()` constructed.

`main(argv=None) -> int` never raises: an argparse usage error, a
`SettingsError`, an `exits.refuse()` call, a `KeyboardInterrupt`, and the
two the board itself raises — `BoardTransportError` and `BoardError` —
are all caught here and turned into the exit code they mean, so a caller
never has to `except SystemExit` to learn what happened.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import httpx
from rich.console import Console
from rich.text import Text

from teille_sync import exits, report
from teille_sync.batch import run_batch
from teille_sync.board import Board, BoardError
from teille_sync.preflight import preflight
from teille_sync.settings import SettingsError, resolve

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"

# `organization(login) { projectV2(number) { ... } }` is the only shape
# `_refresh_ids` understands — see its docstring for why a user-owned
# project is refused rather than guessed at.
_PROJECT_URL_RE = re.compile(
    r"^https://github\.com/orgs/(?P<org>[^/]+)/projects/(?P<number>\d+)/?$")

_PROJECT_QUERY = """
query($org:String!, $number:Int!, $cursor:String){
  organization(login:$org){ projectV2(number:$number){
    id
    fields(first:50){ nodes{
      ... on ProjectV2FieldCommon{ id name dataType }
      ... on ProjectV2SingleSelectField{ id name dataType
        options{ id name } } } }
    items(first:100, after:$cursor){
      pageInfo{ hasNextPage endCursor }
      nodes{ id content{
        ... on DraftIssue{ title }
        ... on Issue{ title }
        ... on PullRequest{ title } } } }
  } }
}
"""

# `datetime.now` read through a module attribute, not called inline —
# the clock cli.py owns, and the one thing a test replaces to make a run
# deterministic (the same pattern `preflight.py` uses for `probe=`).
now_fn = datetime.now


class BoardTransportError(Exception):
    """The board could not be opened, or refused to answer: no token, an
    unreadable id file, a malformed `project_url`, or a live GraphQL
    error. Never carries a hostname, a token or a board id — only what a
    person needs to fix it."""


def _config_file():
    """`config.local.toml`, next to the current working directory — the
    one place `config.example.toml` tells people to put it. Overridable
    in tests, never in production: there is no `--config` flag, on
    purpose, to keep the file's location predictable."""
    return Path("config.local.toml")


def _flags_from(args):
    """The two settings `run` exposes as flags. Every other setting is
    only ever a `TDSYNC_*` variable or a config-file key — see the
    Surface in the project's design spec."""
    return {
        "batch_size": getattr(args, "batch_size", None),
        "reclaim_after": getattr(args, "reclaim_after", None),
    }


# -- the board: token, transport, and the live id-file rebuild --------------

def _github_token():
    return os.environ.get("GITHUB_TOKEN")


def _require_token():
    token = _github_token()
    if not token:
        raise BoardTransportError(
            "GITHUB_TOKEN is not set — the board needs it to authenticate")
    # A command substitution that fails still exports, with the failed
    # command's own error text as the value: `export
    # GITHUB_TOKEN=$(gh auth token)` on a gh without that subcommand
    # exports gh's usage message. Left alone, that travels as far as the
    # Authorization header and comes back as httpx's `Illegal header
    # value` — true, and about nothing the operator can act on. No token
    # GitHub issues contains whitespace.
    #
    # The value is never quoted back: it is a secret whenever it is a
    # real token, and this refusal cannot know which case it has.
    if any(character.isspace() for character in token):
        raise BoardTransportError(
            "GITHUB_TOKEN does not look like a token: it contains "
            "whitespace. A command substitution that fails still exports "
            "— with the failed command's own error text as the value. "
            "Check the command you set it with.")
    return token


def _http_transport(token):
    """The production `transport` callable Board.__init__ expects:
    `(query, variables) -> dict`. Never called in a test — every test
    that needs a `Board` builds one over a recorded double instead."""
    def send(query, variables):
        try:
            response = httpx.post(
                GITHUB_GRAPHQL_URL, timeout=30.0,
                headers={"Authorization": f"Bearer {token}"},
                json={"query": query, "variables": variables})
            response.raise_for_status()
        except httpx.HTTPError as why:
            raise BoardTransportError(f"the board did not answer: {why}") from why
        payload = response.json()
        errors = payload.get("errors")
        if errors:
            raise BoardTransportError(
                "; ".join(e.get("message", "?") for e in errors))
        return payload.get("data") or {}
    return send


def _open_board(settings):
    """Read the id file and wire it to a real transport. Raises
    `BoardTransportError` rather than letting a `KeyError` or an
    `OSError` escape — every caller turns this into one `exits.refuse()`
    call, worded for a person rather than a stack trace."""
    token = _require_token()
    path = Path(settings.ids_file)
    try:
        raw = path.read_text("utf-8")
    except OSError as why:
        raise BoardTransportError(f"cannot read {path}: {why}") from why
    try:
        ids = json.loads(raw)
    except json.JSONDecodeError as why:
        raise BoardTransportError(f"{path} is not valid JSON: {why}") from why
    try:
        return Board(ids, _http_transport(token))
    except (KeyError, TypeError) as why:
        # `preflight.py`'s own Board check catches this shape for `run`
        # and `check` before either ever reaches here — but `status`,
        # `release` and `ids refresh` call this directly, with no
        # preflight in front of them, so a stale or hand-edited id file
        # missing "project"/"fields" must not surface as a bare
        # KeyError/TypeError traceback.
        raise BoardTransportError(f"{path} is not a valid id file: {why}") from why


def _refresh_ids(project_url, transport):
    """Rebuild the id-file shape `Board` consumes, straight from the
    live project: its id, every field (with single-select options), and
    every item's `(title, item_id)` pair, paging until GitHub says
    there is no more.

    Only an organization-owned project URL
    (`https://github.com/orgs/<org>/projects/<n>`) is understood. A
    user-owned project uses a different query root (`user(login:...)`
    rather than `organization(login:...)`), and guessing which one a
    given URL means would be exactly the kind of silent guess this
    project avoids elsewhere — so this refuses instead, before the
    transport is ever called.
    """
    match = _PROJECT_URL_RE.match(project_url or "")
    if not match:
        raise BoardTransportError(
            "project_url must look like "
            "https://github.com/orgs/<org>/projects/<n>, not "
            f"{project_url!r}")
    org, number = match.group("org"), int(match.group("number"))

    project_id = None
    fields = {}
    items = {}
    cursor = None
    while True:
        answer = transport(_PROJECT_QUERY,
                           {"org": org, "number": number, "cursor": cursor})
        project = ((answer.get("organization") or {}).get("projectV2")) or {}
        if not project.get("id"):
            raise BoardTransportError(
                f"the board did not answer for project {number} in {org!r}")
        project_id = project["id"]

        if not fields:
            for node in (project.get("fields") or {}).get("nodes") or []:
                name = node.get("name")
                if not name:
                    continue
                entry = {"id": node.get("id"), "dataType": node.get("dataType")}
                options = node.get("options")
                if options:
                    entry["options"] = {o["name"]: o["id"] for o in options}
                fields[name] = entry

        page = project.get("items") or {}
        for node in page.get("nodes") or []:
            title = (node.get("content") or {}).get("title")
            if title:
                items[title] = node["id"]

        page_info = page.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")

    return {"project": {"id": project_id}, "fields": fields, "items": items}


# -- commands ---------------------------------------------------------------

def _cmd_check(settings, console):
    """Preflight only. Writes nothing to the board or the NAS — the
    Destination check's own directory creation is `preflight.py`'s
    concern, unchanged by this command's existence."""
    checks = preflight(settings)
    console.print(report.preflight_table(checks))
    return exits.OK if all(c.ok for c in checks) else exits.MISCONFIGURED


def _cmd_status(settings, console):
    try:
        board = _open_board(settings)
    except BoardTransportError as why:
        exits.refuse(str(why), code=exits.MISCONFIGURED)
    console.print(report.status_table(board.all_cards()))
    return exits.OK


def _cmd_release(settings, console, documents):
    """Hand each named card back to `À traiter`. Refuses — releasing
    none of them — if any identifier is not on the board at all, the
    same "nothing touched on a bad input" rule preflight follows."""
    try:
        board = _open_board(settings)
    except BoardTransportError as why:
        exits.refuse(str(why), code=exits.MISCONFIGURED)

    by_id = {card.identifier: card for card in board.all_cards()}
    missing = [doc for doc in documents if doc not in by_id]
    if missing:
        exits.refuse(f"not on the board: {', '.join(missing)}", code=exits.USAGE)

    for doc in documents:
        board.release(by_id[doc])
    console.print(report.release_summary(documents))
    return exits.OK


def _cmd_ids_refresh(settings, console):
    try:
        token = _require_token()
        ids = _refresh_ids(settings.project_url, _http_transport(token))
    except BoardTransportError as why:
        exits.refuse(str(why), code=exits.MISCONFIGURED)

    path = Path(settings.ids_file)
    path.write_text(json.dumps(ids, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8")
    console.print(report.ids_refresh_summary(ids, path))
    return exits.OK


def _cmd_run(settings, console, *, batches, until_done, republish, keep,
            dry_run, quiet, verbose, plain):
    if dry_run:
        # Dry run never calls run_batch(), so it is the one path with no
        # BatchResult to read `.checks` off of — it has to probe
        # preflight itself.
        checks = preflight(settings)
        if verbose or any(not c.ok for c in checks):
            console.print(report.preflight_table(checks))
        if any(not c.ok for c in checks):
            return exits.MISCONFIGURED

        try:
            board = _open_board(settings)
        except BoardTransportError as why:
            exits.refuse(str(why), code=exits.MISCONFIGURED)

        # A preview from board.pending(), never from claim(): claiming
        # is the lock, and a dry run must never take it. This can list a
        # document another machine claims before the real run starts —
        # "would have taken", not a promise.
        candidates = [c.identifier for c in board.pending()[:settings.batch_size]]
        console.print(report.dry_run_table(candidates))
        return exits.OK

    try:
        board = _open_board(settings)
    except BoardTransportError as why:
        exits.refuse(str(why), code=exits.MISCONFIGURED)

    overall = exits.OK
    ran = 0
    while True:
        if until_done:
            if not board.pending():
                break
        elif ran >= (batches or 1):
            break

        # run_batch() runs its own preflight, once, and hands the result
        # back on `.checks` — this must not call `preflight()` again:
        # its Services check alone shells out to the converter's own
        # `check --strict`, which probes two HTTP services and the NER
        # dependencies, so a second call here would double that cost on
        # every single batch under --until-done. Per-batch re-probing
        # across *iterations* stays correct (a service can die between
        # batches); a second probe of the *same* batch is pure waste.
        result = run_batch(settings, board, now_fn(),
                           republish=republish, keep=keep, plain=plain)
        ran += 1
        if verbose or any(not c.ok for c in result.checks):
            console.print(report.preflight_table(result.checks))
        overall = max(overall, result.exit_code)
        if not quiet:
            console.print(report.batch_table(result))
        console.print(report.batch_summary(result))
        if result.exit_code == exits.MISCONFIGURED:
            # Preflight refused, or the converter did before writing
            # anything: nothing about the world changed, so looping
            # again would just repeat the same refusal.
            break
        if until_done and not result.claimed:
            # `--until-done` decides its own length from the board, and
            # the board said there was something to take. A batch that
            # took none of it made no progress, and the next iteration
            # would find the same board and make the same none —
            # for ever. Every card lost to another machine does this,
            # and so does anything that keeps `claim()` from succeeding.
            # `--batches N` needs no such guard: N is a count a person
            # typed.
            console.print(Text(
                "stopping: this batch claimed nothing while the board "
                "still has cards à traiter — another machine may hold "
                "them, or claiming is failing; nothing would change by "
                "running again"))
            break
    return overall


# -- argument parsing and dispatch -------------------------------------------

def _positive_batches(raw):
    """The `type=` for `--batches`. Unlike `--batch-size`, `--batches`
    never passes through `settings.py`'s converters — it is not a
    `Settings` field, only the CLI's own loop count — so this is the one
    place guarding it. `int()` alone would accept `0` (running one batch
    silently, the opposite of what was asked) and negative values
    (running none, with no error): the same "a bad flag is a usage error,
    never a silent fallback" rule `settings.py` applies to every other
    integer flag."""
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: {raw!r}")
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or more, not {value}")
    return value


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="teille-sync",
        description="Batch orchestrator between the NAS, teille-douce "
                    "and the corpus board.")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="convert one or more batches")
    count = run_p.add_mutually_exclusive_group()
    count.add_argument("--batches", type=_positive_batches, default=None,
                       metavar="N", help="run exactly N batches (default: 1)")
    count.add_argument("--until-done", action="store_true",
                       help="keep running batches until nothing is à traiter")
    run_p.add_argument("--batch-size", type=int, default=None, metavar="N",
                       help="documents per batch (default: 5)")
    run_p.add_argument("--republish", action="store_true",
                       help="overwrite a document already on the NAS")
    run_p.add_argument("--keep", action="store_true",
                       help="never delete a local source, finished or not")
    run_p.add_argument("--reclaim-after", default=None, metavar="DURATION",
                       help="take back a stale claim after e.g. 6h (default: 6h)")
    run_p.add_argument("--dry-run", action="store_true",
                       help="print what would be claimed; claim nothing")
    verbosity = run_p.add_mutually_exclusive_group()
    verbosity.add_argument("-v", "--verbose", action="store_true",
                           help="show preflight even when everything passes")
    verbosity.add_argument("-q", "--quiet", action="store_true",
                           help="only show the closing summary, not the batch table")
    run_p.add_argument("--plain", action="store_true",
                       help="no colour, for a non-interactive terminal")

    sub.add_parser("check", help="run preflight only; write nothing")
    sub.add_parser("status", help="show counts per statut")

    release_p = sub.add_parser("release", help="hand stuck cards back to À traiter")
    release_p.add_argument("documents", nargs="+", metavar="DOC")

    ids_p = sub.add_parser("ids", help="maintain the board id file")
    ids_sub = ids_p.add_subparsers(dest="ids_command", required=True)
    ids_sub.add_parser("refresh", help="rebuild the id file from the live board")

    return parser


def _dispatch(args):
    try:
        settings = resolve(_flags_from(args), config_file=_config_file())
    except SettingsError as why:
        exits.refuse(str(why), code=exits.USAGE)

    for refusal in settings.refusals:
        print(f"teille-sync: {refusal}", file=sys.stderr)

    console = Console(no_color=getattr(args, "plain", False))

    if args.command == "run":
        return _cmd_run(settings, console, batches=args.batches,
                        until_done=args.until_done, republish=args.republish,
                        keep=args.keep, dry_run=args.dry_run,
                        quiet=args.quiet, verbose=args.verbose,
                        plain=getattr(args, "plain", False))
    if args.command == "check":
        return _cmd_check(settings, console)
    if args.command == "status":
        return _cmd_status(settings, console)
    if args.command == "release":
        return _cmd_release(settings, console, args.documents)
    if args.command == "ids" and args.ids_command == "refresh":
        return _cmd_ids_refresh(settings, console)
    exits.refuse(f"unknown command {args.command!r}", code=exits.USAGE)


def main(argv=None):
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        return _dispatch(args)
    except (BoardTransportError, BoardError) as why:
        # The two the docstring above promised and the code did not
        # deliver. `_http_transport` raises `BoardTransportError` on any
        # GitHub error and `board.py` raises `StaleIdFile` for a field or
        # a Status option the board no longer has — either can surface
        # from a `pending()`, a `claim()` or a `release()` deep inside a
        # batch. Both mean the board cannot be trusted right now, which
        # is exit 3; a traceback would report it as exit 1, "some
        # documents failed", and blame the corpus.
        print(f"teille-sync: {why}", file=sys.stderr)
        return exits.MISCONFIGURED
    except SystemExit as exc:
        code = exc.code
        if isinstance(code, int):
            return code
        return exits.OK if code is None else exits.USAGE
    except KeyboardInterrupt:
        print("teille-sync: interrupted", file=sys.stderr)
        return exits.INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
