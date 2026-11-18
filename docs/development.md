# Development

This page is for working on kistitch itself: the development commands and how releases are made. For using kistitch, see the [README](../README.md).

Development needs [uv](https://docs.astral.sh/uv/). The `run` script in the repository root holds the development commands:

```bash
git clone https://github.com/hatlabs/kistitch
cd kistitch
./run sync     # dependencies and the gui extra
./run test     # the tests
./run check    # lint and the tests, as CI does
```

## Commands

| Command | What it does |
|---|---|
| `./run sync` | Create or update the virtualenv with the dev dependencies and the `gui` extra. |
| `./run test` | Run the tests. Extra arguments go to pytest. |
| `./run lint` | Check linting and formatting with ruff. |
| `./run format` | Format the code and apply ruff's safe lint fixes. |
| `./run check` | Run lint and the tests, as CI does. Run it before committing. |
| `./run pcm-package v<VERSION>+<N>` | Build the KiCad Plugin and Content Manager package and repository files for that stable release tag into `dist/pcm`. |
| `./run bumpversion patch\|minor\|major` | Bump the version in `VERSION`, `pyproject.toml`, `uv.lock` and the version the plugin's launcher requires. Needs bump2version (`uv tool install bump2version`). |
| `./run help` | List the commands. |

Tests never need a running KiCad. The panel's widget tests run on Qt's offscreen platform and are skipped without the `gui` extra.

`tools/make_icons.py` draws the plugin icons in `pcm/`. Its output is committed: edit the script, run it with `.venv/bin/python`, and commit the regenerated files.

## Releasing

Every merge to main stages a release: a pre-release tagged `v<VERSION>+<N>_pre` and a draft `v<VERSION>+<N>`, where N counts the builds of that version.

Publishing the draft on GitHub builds the wheel and the sdist and uploads them to PyPI as version `<VERSION>`. PyPI takes each version once, so a release of a version that is already there with both its wheel and its sdist uploads nothing. A version with only one of them on PyPI fails the release instead; use Re-run failed jobs on the release run that uploaded it, which uploads the rest from that run's build.

Once its version is on PyPI, the release also publishes the KiCad Plugin and Content Manager files that `./run pcm-package` builds, unless the index already lists that version or a newer one:

- the plugin's zip, `packages.json` and the resources archive go on the release;
- its `repository.json`, which points at them, replaces the previous one in the pre-release tagged `pcm`, which users add as `https://github.com/hatlabs/kistitch/releases/download/pcm/repository.json`.

A rerun of a release whose plugin step failed publishes the plugin then. A change to the plugin ships only with a version bump: bump the version with `./run bumpversion` before the release.
