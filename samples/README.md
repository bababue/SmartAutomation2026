# Public sample logs

Two borehole logs to run real documents through the pipeline without the confidential challenge dataset.
Both are public records from the borehole portal of the Landesamt für Geologie, Rohstoffe und Bergbau
Baden-Württemberg (LGRB, [bohrungen.lgrb-bw.de](https://bohrungen.lgrb-bw.de)), exported as PDF from the
portal. Source: © LGRB Baden-Württemberg.

| File | Content |
|---|---|
| `BO_7418_00676_Bohrprofil.pdf` | one-sheet log of a geothermal well, 98 m, northing and easting, not yet checked by LGRB |
| `BO_7418_01197_Bohrprofil.pdf` | one-sheet log of a geothermal well, 100 m, northing and easting, checked by LGRB |

`cache/` holds the answers Claude Opus 5.5 gave for these pages (classification, extraction, second
coordinate reading), so every step after the model runs without a key:

```bash
make samples                                          # from cache/: no key, free
make samples-extract                                  # fresh model calls with the key in .env
DATA_DIR=data/samples .venv/bin/uvicorn api.main:app --port 8000
```

Both write pages and database to `data/samples/`; fresh answers go to `data/samples/cache/`, so `cache/`
here stays as committed. The Docker build and `tests/test_samples.py` rebuild the samples from `cache/`.

A cached answer holds only while its prompt version and page rendering are unchanged. After a
`PROMPT_VERSION`, `CLASSIFY_PROMPT_VERSION` or `COORDS_PROMPT_VERSION` bump the test fails; refresh the
answers with

```bash
DATASET_DIR=samples DATA_DIR=data/samples CACHE_DIR=samples/cache CLASSIFY_MODEL=anthropic/claude-opus-5.5 \
  EXTRACT_MODEL=anthropic/claude-opus-5.5 .venv/bin/python -m pipeline run --all
```

and commit the changed files in `cache/` (about $0.25 through OpenRouter).
