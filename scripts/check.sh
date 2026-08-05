#!/usr/bin/env bash
# Run exactly what CI runs, in one command, before you push.
#
# Why this exists: `cargo test` passing tells you nothing about whether
# CI will go green — the clippy gate is a separate command that's easy
# to forget, and it has burned pushes. This is the one thing to run.
#
# Deliberately does NOT stop at the first failure. A doc-indentation
# lint aborting the run before the tests execute would hide a real test
# failure behind a cosmetic one, so every step runs and the summary at
# the end lists everything that needs fixing.
#
# Usage:
#   scripts/check.sh           # everything
#   scripts/check.sh rust      # skip the Python/GUI half
#   scripts/check.sh python    # skip the Rust half

set -uo pipefail

cd "$(dirname "$0")/.."

WHICH="${1:-all}"
FAILED=()

# Colour only when stdout is a terminal — keeps CI/pipe output clean.
if [[ -t 1 ]]; then
    R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; B=$'\e[1m'; N=$'\e[0m'
else
    R=""; G=""; Y=""; B=""; N=""
fi

step() {
    local name="$1"; shift
    printf '%s==>%s %s\n' "$B" "$N" "$name"
    if "$@"; then
        printf '%s  ok%s\n\n' "$G" "$N"
    else
        printf '%s  FAILED%s\n\n' "$R" "$N"
        FAILED+=("$name")
    fi
}

if [[ "$WHICH" == "all" || "$WHICH" == "rust" ]]; then
    step "cargo build --release --locked" \
        cargo build --release --locked
    step "cargo test --locked --all-targets" \
        cargo test --locked --all-targets
    # -D warnings promotes rustc's own warnings too, which the
    # Cargo.toml [lints.clippy] block does not cover. Matches CI.
    step "cargo clippy (warnings are errors)" \
        cargo clippy --all-targets --no-deps --locked -- -D warnings
fi

if [[ "$WHICH" == "all" || "$WHICH" == "python" ]]; then
    # Find an interpreter that actually has PySide6. The dev box has no
    # system PySide6, so a venv is the normal case; skipping with a loud
    # notice beats failing on a missing dependency that CI supplies.
    PY=""
    for candidate in \
        "${VIRTUAL_ENV:-}/bin/python" \
        "./venv/bin/python" \
        "./.venv/bin/python" \
        "python3"
    do
        [[ -x "$candidate" || "$candidate" == "python3" ]] || continue
        if "$candidate" -c 'import PySide6' >/dev/null 2>&1; then
            PY="$candidate"
            break
        fi
    done

    if [[ -n "$PY" ]]; then
        step "pytest (offscreen GUI)" \
            env QT_QPA_PLATFORM=offscreen "$PY" -m pytest tests/ -q
    else
        printf '%s==>%s pytest (offscreen GUI)\n' "$B" "$N"
        printf '%s  SKIPPED — no interpreter with PySide6.%s\n' "$Y" "$N"
        printf '  python3 -m venv .venv && .venv/bin/pip install PySide6-Essentials pytest\n\n'
    fi

    step "validate translation .ts XML" bash -c \
        "find gui/translations -name '*.ts' -print0 \
         | xargs -0 -n1 python3 -c 'import sys, xml.etree.ElementTree as ET; ET.parse(sys.argv[1])'"
fi

if (( ${#FAILED[@]} )); then
    printf '%s%d step(s) failed:%s\n' "$R" "${#FAILED[@]}" "$N"
    printf '  - %s\n' "${FAILED[@]}"
    printf '\nMechanical clippy lints are often auto-fixable:\n'
    printf '  cargo clippy --all-targets --fix --allow-dirty\n'
    exit 1
fi

printf '%sAll checks passed — safe to push.%s\n' "$G" "$N"
