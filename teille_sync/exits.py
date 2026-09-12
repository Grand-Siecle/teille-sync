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
