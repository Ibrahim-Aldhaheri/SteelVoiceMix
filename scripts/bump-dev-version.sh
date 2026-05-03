#!/usr/bin/env bash
# Bump the dev-channel version in lockstep across every file that
# hard-codes it. Single source of truth = this script's idea of the
# version (default: increment the trailing betaN number by 1; pass an
# explicit version to override).
#
# Why a script instead of a centralised VERSION file:
#   - rpkg / COPR's build chroot has %{_sourcedir} subtleties around
#     reading external files at spec-eval time. A self-contained spec
#     ships fewer surprises.
#   - The Python fallback constant is only consulted when `rpm -q` is
#     silent (source-checkout / non-RPM installs); the runtime path
#     already derives from RPM, so we don't need a runtime hook here.
#   - Cargo.toml's `version` is only meaningful for crates.io publishes
#     (we don't). Cargo doesn't accept `~beta20` syntax (uses
#     `-beta.20` semver pre-release), so we deliberately leave it
#     alone — the crate version isn't user-facing.
#
# Usage:
#   scripts/bump-dev-version.sh              # auto-increment betaN
#   scripts/bump-dev-version.sh 0.4.2~beta1  # explicit version
#   scripts/bump-dev-version.sh --check      # print current, no change
#
# After bumping you still need to commit + push yourself — the script
# never auto-commits so it composes cleanly with whatever change you're
# already about to commit alongside it.

set -euo pipefail

cd "$(dirname "$0")/.."

SPEC="steelvoicemix-dev.spec"
PY="gui/settings.py"
CARGO="Cargo.toml"

# Pull the current version from the canonical spec file.
current=$(awk '/^Version:[[:space:]]/ {print $2; exit}' "$SPEC")
if [[ -z "$current" ]]; then
    echo "Could not read current Version: from $SPEC" >&2
    exit 1
fi

if [[ "${1:-}" == "--check" ]]; then
    echo "$current"
    exit 0
fi

if [[ -n "${1:-}" ]]; then
    new="$1"
else
    # Auto-increment trailing betaN. Anything else (no `~betaN` suffix
    # at all, or a non-numeric one) requires an explicit version arg.
    if [[ "$current" =~ ^(.+~beta)([0-9]+)$ ]]; then
        prefix="${BASH_REMATCH[1]}"
        n="${BASH_REMATCH[2]}"
        new="${prefix}$((n + 1))"
    else
        echo "Cannot auto-increment $current (no ~betaN suffix)." >&2
        echo "Pass an explicit version, e.g. $0 0.4.2~beta1" >&2
        exit 1
    fi
fi

if [[ "$new" == "$current" ]]; then
    # Spec/Py already at target — but Cargo.toml may have drifted
    # (existed in the repo before the bump script grew Cargo
    # support). Re-sync it without re-bumping the others.
    cargo_new="${new/~beta/-beta.}"
    cur_cargo=$(awk -F'"' '/^version = / {print $2; exit}' "$CARGO")
    if [[ "$cur_cargo" != "$cargo_new" ]]; then
        sed -i "s/^version = \".*\"/version = \"$cargo_new\"/" "$CARGO"
        echo "Re-synced $CARGO: $cur_cargo → $cargo_new"
        exit 0
    fi
    echo "Already at $current — nothing to do."
    exit 0
fi

# Sanity-check the requested version. RPM accepts a wide range, but
# our convention is `MAJOR.MINOR.PATCH` optionally followed by
# `~betaN`. Reject anything that wouldn't fit so a typo doesn't
# silently land in the spec.
if ! [[ "$new" =~ ^[0-9]+\.[0-9]+\.[0-9]+(~beta[0-9]+)?$ ]]; then
    echo "Refusing to set unrecognised version: $new" >&2
    echo "Expected MAJOR.MINOR.PATCH or MAJOR.MINOR.PATCH~betaN" >&2
    exit 1
fi

# Atomic-ish updates — write to .tmp, mv into place.
# Cargo doesn't accept RPM's `~betaN` pre-release syntax; it wants
# semver `-beta.N`. Translate before writing to Cargo.toml so
# `cargo build` doesn't reject the file and `--version` reflects the
# bump. RPM still reads its own copy from the spec.
cargo_new="${new/~beta/-beta.}"

tmp_spec="$SPEC.tmp.$$"
tmp_py="$PY.tmp.$$"
tmp_cargo="$CARGO.tmp.$$"
trap 'rm -f "$tmp_spec" "$tmp_py" "$tmp_cargo"' EXIT

sed "s/^Version:.*/Version:        $new/" "$SPEC" > "$tmp_spec"
sed "s/^_APP_VERSION_FALLBACK = \".*\"/_APP_VERSION_FALLBACK = \"$new\"/" "$PY" > "$tmp_py"
sed "s/^version = \".*\"/version = \"$cargo_new\"/" "$CARGO" > "$tmp_cargo"

# Verify each file actually changed exactly the line we wanted, and
# only one line. Catches a regex mismatch silently doing nothing.
spec_diff=$(diff "$SPEC" "$tmp_spec" | grep -c '^[<>]' || true)
py_diff=$(diff "$PY" "$tmp_py" | grep -c '^[<>]' || true)
cargo_diff=$(diff "$CARGO" "$tmp_cargo" | grep -c '^[<>]' || true)
if [[ "$spec_diff" -ne 2 ]]; then
    echo "Unexpected diff in $SPEC ($spec_diff lines changed, expected 2)" >&2
    diff "$SPEC" "$tmp_spec" >&2 || true
    exit 1
fi
if [[ "$py_diff" -ne 2 ]]; then
    echo "Unexpected diff in $PY ($py_diff lines changed, expected 2)" >&2
    diff "$PY" "$tmp_py" >&2 || true
    exit 1
fi
# Cargo diff is allowed to be 0 (already at target after a no-op
# bump) or 2 (real change). Anything else is a regex miss.
if [[ "$cargo_diff" -ne 0 && "$cargo_diff" -ne 2 ]]; then
    echo "Unexpected diff in $CARGO ($cargo_diff lines changed)" >&2
    diff "$CARGO" "$tmp_cargo" >&2 || true
    exit 1
fi

mv "$tmp_spec" "$SPEC"
mv "$tmp_py" "$PY"
mv "$tmp_cargo" "$CARGO"
trap - EXIT

echo "Bumped: $current → $new"
echo "  $SPEC"
echo "  $PY"
echo "  $CARGO  (cargo: $cargo_new)"
echo
echo "Next: commit alongside whatever change motivated this bump."
