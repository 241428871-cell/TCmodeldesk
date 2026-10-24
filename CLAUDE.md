# Claude Code entry

Follow `AGENTS.md` (project rules), `docs/GIT_WORKFLOW.md` and `docs/CROSS_HOST_FEATURES.md`.

## Python environment

Development checks use an Anaconda-managed environment declared in `environment.yml`
(versions mirror `control-plane/uv.lock` and `requirements.txt`). Do not use the system
Python, a bare `python`/`pip`, or ad-hoc virtualenvs for these checks.

The environment name is `aad-control`. Resolve its installation through Conda on the current
machine; never copy a maintainer's absolute interpreter path into source or automation. The
pinned versions include CPython 3.12, LangGraph 1.2.16, checkpoint-sqlite 3.1.2, Pydantic
2.14.1, pytest 9.1.1, Ruff 0.17.1 and basedpyright 1.40.2, with `control-plane` installed
editable.

Create or restore (the default Anaconda channels require a Terms of Service acceptance that
only the user may give, so the environment is built from conda-forge):

```sh
conda env create --file environment.yml
# Existing environment:
conda env update --name aad-control --file environment.yml --prune
```

Run checks (lint and type checks from `control-plane` so per-file settings resolve):

```sh
cd control-plane
conda run --name aad-control python -m pytest tests -q
conda run --name aad-control python -m ruff check src tests typings
conda run --name aad-control basedpyright
cd ..
conda run --name aad-control python -m pytest .local-worker/tests -q
```

`control-plane/.venv` is a historical uv environment from an earlier session; it is not the
recorded environment for new checks.
