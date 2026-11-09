# Release Plan

Release process for `rustyfarian-bench`.

> **Status:** Last exercised for `v0.4.0` (tagged 2026-11-09).
> Update this note only after the tag is pushed.
> All git write actions (commit, push, tag) are run by the maintainer, never by an agent.

## Versioning

- **Scheme:** SemVer, 0.x.y (pre-stable).
- **Compatibility:** no policy before 1.0; consumers pin a tag, so any TOML schema or CLI change may ship in a minor.
- **Snapshot convention:** none; the source stays at the released version between releases.
- **Who decides version:** maintainer.

## Branch and Tag Convention

- **Release branch:** `main`.
- **Tag format:** `vX.Y.Z` — consumers pin it in `uvx --from git+https://github.com/datenkollektiv/rustyfarian-bench@vX.Y.Z`.
- **Tagging:** manual annotated tag, pushed only after CI is green on the release commit.

## Pre-flight Checklist

Before finalizing the changelog:

- [ ] `just verify` passes (ruff, format check, `mypy --strict`, pytest).
- [ ] `just verify-python 3.12` passes (the declared `requires-python` floor).
- [ ] `just smoke-install` passes (built wheel via isolated `uvx`: `--help`, then `list` a fixture consumer).
- [ ] `CHANGELOG.md` has an `[Unreleased]` section with content.
- [ ] Version consistent across the files listed under Version Bump.
- [ ] At least one consumer bench run passed on hardware.

After pushing the release commit, before tagging:

- [ ] `git status --short` is empty and the branch is `main`.
- [ ] CI (`.github/workflows/verify.yml`) is green on the release commit.

## Version Bump

Files to update when bumping to `X.Y.Z`:

- `pyproject.toml` — `version`
- `src/rustyfarian_bench/__init__.py` — `__version__`
- `uv.lock` — the `rustyfarian-bench` package entry; regenerate with `just lock`, never by hand

Post-release version bump: none.

## Publish

**Target:** git tag on `github.com/datenkollektiv/rustyfarian-bench` (public).
**Registry:** none; PyPI is deferred (see `docs/features/remote-distribution-v1.md`).

## Changelog

**Location:** `CHANGELOG.md`.
**Format:** [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
**Process:**

1. Replace `## [Unreleased]` with `## [X.Y.Z] - YYYY-MM-DD`.
2. Add a fresh, empty `## [Unreleased]` above it.
3. Update the **Status** note at the top of this file.

## Tagging

Push `main` first and tag only once CI is green on that commit:

```sh
git push origin main
# wait for CI
git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push origin vX.Y.Z
```

## Tagged Install Check

Before the GitHub release, from a directory outside this checkout:

```sh
uvx --from git+https://github.com/datenkollektiv/rustyfarian-bench@vX.Y.Z rustyfarian-bench --help
uvx --from git+https://github.com/datenkollektiv/rustyfarian-bench@vX.Y.Z rustyfarian-bench --repo <consumer> list
```

## GitHub Release

- Select tag `vX.Y.Z` on the repository releases page.
- Title: `vX.Y.Z`.
- Body: the `## [X.Y.Z]` changelog section verbatim.

## Post-release Steps

- [ ] Bump the pinned tag in each consumer's `bench` recipe (pilots: `rustyfarian-ws2812`, `rustyfarian-peripherals`).
- [ ] Fresh-clone check: a fresh consumer clone runs `just bench-list` with no sibling checkout.

## Rollback Procedure

A pushed tag may already be cached by consumers' `uvx`; prefer a new patch release over moving a tag.
If the tag must go before any consumer pinned it:

1. Delete the remote tag: `git push --delete origin vX.Y.Z`.
2. Delete the local tag: `git tag -d vX.Y.Z`.
3. Revert the release commit: `git revert <sha>`.
4. Delete the GitHub release page, if created.

## Release Record Location

Each release produces files in `release/`, which is gitignored and kept for internal use only:

1. `YYYY-MM-DD-<version>-preflight.md` — pre-flight assessment
2. `YYYY-MM-DD-<version>-plan.md` — ordered execution plan
3. `YYYY-MM-DD-<version>-record.md` — what was done and what remains
