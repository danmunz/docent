# Docent — Agent Guidelines

## Pre-commit Checklist

Before every commit, follow these steps in order:

### 1. Run tests

```bash
PYTHONPATH=. uv run pytest -x -q
```

All 73+ tests must pass. Do not commit with failing tests.

### 2. Check for system library dependencies

If a new Python dependency was added that requires native/system libraries (the way Pillow needs `libjpeg-dev` and `zlib1g-dev`):

- Add the required package to the `apt-get install` line in `Dockerfile`
- Add a note in the README Quick Start Linux callout block
- Update `requirements.txt` if `pyproject.toml` changed: `uv lock && uv export --no-hashes --no-dev > requirements.txt`

### 3. Evaluate documentation impact

Consider whether the changes warrant updates to:

- **README.md** (main branch) — new features, changed configuration, new env vars, updated instructions
- **gh-pages landing page** — significant user-facing features worth marketing, or changes to app functionality that require corresponding updates to content

If updates seem appropriate, propose them and obtain user approval before committing.

### 4. Evaluate versioning

Assess whether the commit (alone or combined with recent unreleased commits) warrants a new version tag:

- **Patch** (v0.1.x): bug fixes, dependency updates, documentation
- **Minor** (v0.x.0): new features, new configuration options, Docker changes
- **Major** (vx.0.0): breaking changes to configuration, API, or data formats

If a release is warranted, propose the version bump and tag after the commit. Tagging a `v*` release triggers the Docker CI workflow to build and push images to GHCR.

## Commit Discipline

- **Atomic commits**: each commit should represent one self-contained change. Do not mix backend logic, frontend UI work, and documentation churn in the same commit unless they are inseparable.
- **Descriptive messages**: use clear, imperative commit messages such as `fix: resolve thumbnail race condition` or `feat: add Wikipedia links to metadata fields`. Prefix with `fix:`, `feat:`, `docs:`, `refactor:`, or `chore:` as appropriate.
- **Branches for non-trivial work**: create a feature or fix branch for meaningful changes. Use descriptive names such as `feat/docker-support`, `fix/dependencies`, or `fix/crop-zoom`. Push the branch and open a pull request. Do not merge into `main` without validation.
- **Do not commit speculative architecture**: if a design decision is still open, settle it explicitly before committing code that depends on it.
- **No commits to `main` without passing tests**: every change intended for `main` must pass the full test suite. The pre-commit hook enforces this, but agents should verify independently.
- **README must stay current**: any change that affects setup steps, configuration, environment variables, or user-facing behavior must update `README.md` in the same commit or PR.
- **Do not leave knowledge trapped in PR descriptions**: if something is important enough to explain in a PR body, it belongs in the README, code comments, or this file.

## Project Context

- **Stack**: Python 3.13, FastAPI, single-page vanilla HTML/CSS/JS frontend
- **Package manager**: uv (lockfile: `uv.lock`)
- **Test command**: `PYTHONPATH=. uv run pytest -x -q`
- **Server**: `uv run python3 server.py` (port 8000)
- **Docker**: `Dockerfile` on main, CI builds on version tags, `DOCENT_DATA_DIR=/data` for container data
- **Branches**: `main` for development, `gh-pages` for landing page
- **Pre-commit hook**: already runs tests + blocks secrets (`.env`, `ai_config.json`, `.tv-token`, API key patterns)

## Practical Guardrails

- The frontend is a single `index.html` file (vanilla HTML/CSS/JS). Do not introduce a build step, bundler, or framework.
- Do not add a database. All state lives in JSON files and the TV's own storage.
- Do not add authentication or multi-user support unless explicitly requested.
- Prefer small, reviewable changes over large monolithic PRs.
- Keep test coverage focused on the highest-risk paths: TV communication mocking, file persistence, API endpoints, and AI pipeline plumbing.
