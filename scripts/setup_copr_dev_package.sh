#!/usr/bin/env bash
# One-shot: create the `steelvoicemix` package inside the
# `abokhalil/steelvoicemix-dev` COPR project. Run once after
# creating the project in the COPR web UI; after that, the GitHub
# Actions workflow (.github/workflows/copr-dev.yml) re-runs the
# package's source script on every push to dev.
#
# Requires:
#   - copr-cli installed (`sudo dnf install copr-cli`).
#   - Your COPR API token already configured at ~/.config/copr (or
#     run `copr-cli`'s prompt-on-first-use to grab one from
#     https://copr.fedorainfracloud.org/api).
#
# Idempotent: if the package already exists, this just prints a
# notice and exits clean. Re-run any time to refresh the script
# contents (e.g. after editing the build steps below).

set -euo pipefail

PROJECT="abokhalil/steelvoicemix-dev"
PACKAGE="steelvoicemix"

# Build script COPR runs to produce the SRPM. Clones dev branch,
# uses the dev spec's git_dir_version macro for auto-versioning.
#
# Note the unshallow + tags fetch: rpkg's `git_dir_version` macro
# expands via `git describe --tags`, which needs the latest stable
# tag (e.g. `v0.3.1`) to be reachable from the dev HEAD. A shallow
# clone (50 commits) often misses that tag, in which case rpkg
# falls back to `0.0` as the version — and `0.0.git.<sha>` sorts
# BELOW the stable 0.3.1 in RPM vercmp, breaking the alpha-replaces-
# stable upgrade flow. The unshallow + tags fetch costs ~2 MB extra
# but guarantees correct versioning.
read -r -d '' BUILD_SCRIPT <<'EOF' || true
#!/bin/bash
set -eu
dnf -y install rpkg-util git
git clone --depth=50 https://github.com/Ibrahim-Aldhaheri/SteelVoiceMix.git
cd SteelVoiceMix
git fetch --unshallow --tags 2>/dev/null || git fetch --tags
git checkout dev
rpkg srpm --spec steelvoicemix-dev.spec --outdir "$outdir"
EOF

if ! command -v copr-cli >/dev/null 2>&1; then
    echo "error: copr-cli not found. sudo dnf install copr-cli" >&2
    exit 1
fi

# Stash the build script in a temp file — copr-cli's --script flag
# wants a path, not stdin.
script_path=$(mktemp --suffix=.sh)
trap 'rm -f "$script_path"' EXIT
printf '%s\n' "$BUILD_SCRIPT" > "$script_path"

# Determine current package state. We need to know not just whether
# the package exists, but ALSO its source type — copr-cli won't let
# us convert an SCM-source package into a Custom-source one in
# place; if the type is wrong, we have to delete + recreate.
existing_type=""
if copr-cli get-package "$PROJECT" --name "$PACKAGE" --with-latest-build \
       >/tmp/copr-pkg.json 2>/dev/null; then
    existing_type=$(python3 -c "import json,sys; print(json.load(open('/tmp/copr-pkg.json')).get('source_type',''))" 2>/dev/null || true)
fi
rm -f /tmp/copr-pkg.json

case "$existing_type" in
    "")
        action=add
        echo "Package $PACKAGE not found — creating as Custom-script source."
        ;;
    custom)
        action=edit
        echo "Package $PACKAGE already exists as Custom — refreshing script."
        ;;
    *)
        echo "Package $PACKAGE exists with source type '$existing_type', not 'custom'."
        echo "Recreating it as Custom — required for our build script to run."
        copr-cli delete-package "$PROJECT" --name "$PACKAGE"
        action=add
        ;;
esac

copr-cli "${action}-package-custom" "$PROJECT" \
    --name "$PACKAGE" \
    --script "$script_path" \
    --script-builddeps "rpkg-util git" \
    --script-resultdir "result"

echo
echo "Done. Trigger a build to verify:"
echo "  copr-cli build-package $PROJECT --name $PACKAGE"
echo
echo "Or push a commit to dev — .github/workflows/copr-dev.yml will"
echo "fire on its own."
