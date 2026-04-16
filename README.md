# ArrowLab

Audio + video archery telemetry. Work in progress.

See [docs/initial_idea.md](docs/initial_idea.md) for the original concept.

## Status

Scaffolding only. No pipeline yet.

## Layout

- `src/arrowlab/` — package (`audio/`, `video/`, `dashboard/`)
- `tests/` — pytest
- `data/raw/` — recorded videos (gitignored)
- `data/processed/` — derived artifacts (gitignored)
- `notebooks/` — prototyping
- `docs/` — design notes

## Dev setup

```bash
uv venv
uv sync
```
