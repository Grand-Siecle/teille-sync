#!/usr/bin/env bash
#
# Set up a machine to run teille-sync: virtual environment, the converter,
# teille-sync itself, and a verification that the two are where the batch
# will look for them.
#
# It exists because every step below has a way of failing that names
# something other than its cause, and all of them were hit on the same
# evening:
#
#   - `pip` is not necessarily the virtual environment's pip. If
#     ~/.local/bin precedes it on PATH, a plain `pip install` puts both
#     projects into whatever Python owns that directory — 3.10 on the
#     machine this was written for, which cannot satisfy either
#     `requires-python`. So every call here goes through
#     "$VENV/bin/python" -m pip, which cannot be shadowed.
#   - The converter's [ner] extra is mandatory. Without it the Services
#     check refuses every run, and the reason given is about services
#     rather than about a missing install.
#   - Without a GPU, installing torch after the [ner] line pulls the CUDA
#     build: several gigabytes nobody asked for.
#
# Safe to re-run: every step is idempotent, and a second pass on a
# working machine is a no-op that ends by verifying it.
#
# Linux, macOS and WSL. On Windows, run it inside WSL — which is what the
# README's mount instructions assume anyway.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONVERTER_REPO="https://github.com/Grand-Siecle/TEIlle-douce.git"

editable=""          # --editable [path]: install the converter from a
                     # local checkout instead of the pinned tag
with_dev=0           # --dev: also install the test dependencies

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
die()  { printf '\n\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }

usage() {
    cat <<'USAGE'
usage: scripts/install.sh [--editable [PATH]] [--dev]

  (no flags)          install the converter from the tag pinned in
                      pyproject.toml — for a machine that runs batches
  --editable [PATH]   install the converter from a local checkout
                      instead, so edits to it count — for developing the
                      converter (default PATH: ../TEIlle-douce)
  --dev               also install pytest and coverage
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --editable)
            # The path is optional, but must not swallow the next flag.
            if [ $# -ge 2 ] && [ "${2#-}" = "$2" ]; then
                editable="$2"; shift 2
            else
                editable="$REPO/../TEIlle-douce"; shift
            fi
            ;;
        --dev)          with_dev=1; shift ;;
        -h|--help)      usage; exit 0 ;;
        *)              usage >&2; die "unknown argument: $1" ;;
    esac
done

# -- 1. An interpreter new enough for both projects --------------------------

say "1. Python"
python_bin=""
for candidate in python3.13 python3.12 python3; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
        python_bin="$candidate"
        break
    fi
done
[ -n "$python_bin" ] || die "no Python 3.12+ found. teille-sync and teille-douce both
require it. Install python3.12, then run this again."
info "$($python_bin -V) at $(command -v "$python_bin")"

# -- 2. The virtual environment ----------------------------------------------
#
# An existing one is reused whatever it is called: the README says
# `.venv`, machines in the wild have `venv`, and silently creating a
# second one beside the first is how a machine ends up with the converter
# in one and teille-sync in the other.

say "2. Virtual environment"
VENV=""
for existing in "$REPO/.venv" "$REPO/venv"; do
    if [ -x "$existing/bin/python" ]; then VENV="$existing"; break; fi
done
if [ -n "$VENV" ]; then
    info "reusing $(basename "$VENV")/"
else
    VENV="$REPO/.venv"
    "$python_bin" -m venv "$VENV"
    info "created .venv/"
fi
PY="$VENV/bin/python"
"$PY" -m pip install --quiet --upgrade pip
info "pip $("$PY" -m pip --version | cut -d' ' -f2)"

# -- 3. torch, before the converter, when there is no GPU --------------------
#
# pip resolves torch for the [ner] extra to the CUDA build by default.
# On a machine with no GPU that is gigabytes of wheels that will never be
# used, so the CPU build goes in first and the extra then finds its
# requirement already satisfied.

say "3. torch"
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    info "GPU present ($(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)) — taking the default CUDA build"
elif "$PY" -c 'import torch' 2>/dev/null; then
    info "already installed — left alone"
else
    info "no GPU — installing the CPU build first, so the converter does not pull CUDA"
    "$PY" -m pip install --quiet torch --index-url https://download.pytorch.org/whl/cpu
fi

# -- 4. The converter --------------------------------------------------------

say "4. Converter (teille-douce)"
if [ -n "$editable" ]; then
    [ -d "$editable" ] || die "no converter checkout at $editable

Clone it first, then run this again:
  git clone $CONVERTER_REPO \"$editable\""
    info "editable, from $editable"
    "$PY" -m pip install -e "${editable}[ner]"
else
    # The tag lives in pyproject.toml's [converter] extra, installed
    # together with teille-sync in step 5 — nothing to do here.
    info "from the tag pinned in pyproject.toml (installed with teille-sync below)"
fi

# -- 5. teille-sync ----------------------------------------------------------

say "5. teille-sync"
extras=""
[ -z "$editable" ] && extras="converter"
[ "$with_dev" -eq 1 ] && extras="${extras:+$extras,}dev"
target="."
[ -n "$extras" ] && target=".[$extras]"
info "installing $target"
( cd "$REPO" && "$PY" -m pip install -e "$target" )

# -- 6. Verify ---------------------------------------------------------------
#
# That pip reported success is not the same as the batch being able to
# find these: teille-sync calls `teille-douce` by name, so what matters
# is that both console scripts exist in this environment's bin.

say "6. Verification"
failed=0
for tool in teille-sync teille-douce; do
    if [ -x "$VENV/bin/$tool" ]; then
        info "$tool -> $VENV/bin/$tool"
    else
        printf '  \033[1;31mmissing: %s\033[0m\n' "$tool"; failed=1
    fi
done
[ "$failed" -eq 0 ] || die "installation incomplete — see above."
info "$("$VENV/bin/teille-douce" --version 2>&1 | head -1)"
"$VENV/bin/teille-sync" --help >/dev/null 2>&1 || die "teille-sync is installed but will not run."
if ! "$PY" -c 'import torch, transformers, gliner, flair' 2>/dev/null; then
    die "the converter is installed without its NER dependencies.
The Services check refuses every run without them. Re-run this script."
fi
info "NER dependencies present"

# -- 7. What is left, which is not installation ------------------------------

say "Installed. What this script cannot do for you:"
cat <<NEXT

  source $(basename "$VENV")/bin/activate

  1. Configuration      cp config.example.toml config.local.toml
                        then set nas_root and project_url
  2. GitHub token       export GITHUB_TOKEN=<token>          (needs the
                        \`project\` scope; never printed or logged)
                        With the gh CLI:
                          gh auth token                      (gh >= 2.16)
                          awk '/oauth_token:/{print \$2}' ~/.config/gh/hosts.yml
                        Check it took: a real token is 40 characters. A
                        failed command substitution exports its own error
                        text and the board then answers \`Illegal header
                        value\`.
  3. VPN and share      mount it, then confirm: ls <nas_root>
  4. The two services   start PyHellen and VieuxParler
  5. The id file        teille-sync ids refresh

  Then: teille-sync check    — it tells you which of these is not done.

NEXT
