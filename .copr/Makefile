# COPR `source_build_method: make` entrypoint. Lives in `.copr/`
# because COPR looks there first; falls back to repo-root Makefile
# otherwise. Used only by the dev (alpha) COPR project — the stable
# project hardcodes its Version in steelvoicemix.spec and doesn't
# need any of this.
#
# The whole point of this file is fixing a versioning bug we hit
# with `source_build_method: rpkg`: COPR clones the repo with
# `--depth 500 --no-single-branch` which limits commit history but
# does NOT auto-fetch tag refs, so `rpkg`'s `git_dir_version` macro
# sees no tag and falls back to `0.0.git.<count>.<sha>` — a version
# string that sorts ABOVE 0.3.1 in some interpretations and is
# generally meaningless to read.
#
# Here we explicitly unshallow + fetch tags before invoking rpkg,
# guaranteeing the latest stable tag (e.g. v0.3.1) is reachable
# from dev's HEAD when `git describe --tags` runs. The result is a
# clean `0.3.1.<commits-since-tag>.g<sha>` version that vercmp's
# correctly between stable releases.

.PHONY: srpm

srpm:
	# Tooling. `rpkg` provides `rpkg srpm`; `git` is needed for the
	# describe-and-fetch dance below. F44 chroot ships only dnf5 —
	# `dnf` is symlinked to `dnf5`, so this works on F43/F44/rawhide.
	dnf -y install rpkg git
	# Unshallow + fetch tags. Both lines are best-effort: if the
	# clone is already complete (e.g. from a manual rebuild), the
	# unshallow returns non-zero — swallow that with `|| true`.
	# `--force` on the tag fetch overwrites any local tags that
	# disagree with origin (shouldn't happen on COPR, but cheap).
	git fetch --unshallow 2>/dev/null || true
	git fetch --tags --force
	rpkg srpm --spec steelvoicemix-dev.spec --outdir $(outdir)
