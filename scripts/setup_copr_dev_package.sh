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
read -r -d '' BUILD_SCRIPT <<'EOF' || true
#!/bin/bash
set -eu
dnf -y install rpkg-util git
git clone --depth=50 https://github.com/Ibrahim-Aldhaheri/SteelVoiceMix.git
cd SteelVoiceMix
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

action=add
if copr-cli list-packages "$PROJECT" 2>/dev/null \
   | grep -q "^- $PACKAGE\$\|name: $PACKAGE\b"; then
    echo "Package $PACKAGE already exists in $PROJECT — refreshing script."
    action=edit
fi

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
