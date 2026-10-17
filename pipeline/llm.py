"""Claude calls for page classification and log extraction.

- Classification: strict structured output (small schema).
- Extraction: JSON validated locally with Pydantic; the schema is too large for strict structured
  outputs (max. 16 nullable fields per request).
- Server-side refusal fallback enabled (`fallbacks: "default"`).
- Responses are cached on disk by input hash, so re-runs cost nothing and a crash mid-run
  loses no work. API calls also store their token usage there, for measured cost per page.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import threading
from pathlib import Path
from typing import TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from . import config
from .extraction_models import PageClassification, PageExtraction, PageLabResults
from .ingest import PageImage

T = TypeVar("T", bound=BaseModel)

_client: anthropic.Anthropic | None = None
_usage = threading.local()  # token usage of the call in progress on this thread (see _cached)

# USD per million tokens (input, output), Anthropic list prices. Cache writes cost 1.25x input,
# cache reads 0.1x input.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}
USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


class ExtractionRefused(RuntimeError):
    pass


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(max_retries=4)
    return _client


CLASSIFY_SYSTEM = """You sort pages of geotechnical site investigation documents from all over the world \
(any language, scans, handwriting, CAD exports). For each page decide what it is and list every \
borehole / test pit / probe whose log or coordinates appear on it, with names exactly as printed. \
A borehole log is a page showing depth-wise layer descriptions of one hole (often with SPT, samples, \
core recovery). Report prose that merely mentions boreholes is "text". Summary tables listing \
coordinates of several holes are "coordinate_table". If the page states the coordinate system, grid, datum \
or zone anywhere (title block, notes, legend, report text), copy that statement into `crs_text`."""

EXTRACT_SYSTEM = """You transcribe geotechnical borehole and test pit logs into structured data for a \
global subsurface database used to design tunnel boring machines. Accuracy matters more than \
completeness: never invent values. If something is not printed or not readable, use null and list it \
in `unreadable`.

Rules:
- Report every log visible on the page (a page can contain several). Use the hole name exactly as printed.
- Depths are metres below ground surface along the hole. Convert feet to metres and say so in `notes`.
  If the log only shows elevations (levels), convert to depth using the ground level and say so in `notes`.
- Layers: one entry per described stratum with its top and bottom depth. On continuation sheets only \
include what is visible on this page. If the last layer continues onto the next sheet, set its bottom_m to the \
deepest depth shown on this sheet's depth scale and write "continues on next sheet" into `notes`.
- Check every layer boundary against the depth scale and the drawn boundary line, not only the printed digits. \
Small or faint digits are easily misread (1.60 vs 1.80, 3.5 vs 8.5). If the printed value and the scale position \
disagree, use the value that matches the scale and list the boundary in `unreadable`. Keep `description_original` verbatim in the original language and \
add `description_en`.
- Lithology: map the main constituent to the closest DIN 4023 class (`primary`), e.g. made ground, \
asphalt, concrete, rubble → fill; caliche, calcarenite → limestone; granite → plutonic_rock; basalt → \
volcanic_rock; core loss / no recovery → no_recovery. Secondary components with qualifiers: \
'trace of' → trace, 'slightly'/'some' → slightly, '-y' (sandy, silty) → moderately, 'very' → very.
- Stratigraphy: fill `stratigraphic_name`/`stratigraphic_code` only if printed. `geologic_age` may be \
filled when printed or unambiguous (e.g. 'Quaternary' alluvium). `stratigraphic_color` only if the log \
shows a coloured stratigraphy column.
- Coordinates: copy the coordinate text exactly as printed into `coords_raw`, including labels (X/Y, \
N/E, Easting/Northing) and separators. Copy any datum / grid / zone text into `crs_raw`. Do not convert.
- Place: from the project and location text, give your best estimate of the latitude/longitude of the \
named site or town in `place_lat`/`place_lon` with a realistic `place_precision_km`. This is used to \
sanity-check the printed coordinates, so base it on the place names, not on the coordinates.
- SPT: keep the printed notation in `raw`; blow counts per increment in `blows`.
- Elevation: copy the printed ground level text into `elevation_raw`; `elevation_m` positive up as printed.
- Header, title block, footnotes and legend: read them completely for the coordinate system / grid / datum / \
zone (→ `crs_raw`, also when it is printed away from the coordinates, e.g. 'All coordinates MGA94 Zone 55'), the \
elevation datum (→ `elevation_datum`, e.g. 'AHD', 'mOD', 'NN', 'MSL') and the groundwater reference point \
(→ `reference` of each groundwater entry: below ground, below top of casing, other).
- Evidence: for every layer and for the coordinates give `bbox`/`coords_bbox`, the region of the page image \
where the value is printed (layer: its depth labels and description row), as [x0, y0, x1, y1] fractions of the \
image width and height with the origin top left. Approximate is fine; null if you cannot locate it.
- Previous sheet: a short summary of the previous sheet of the same document may be given. Use it only to \
continue a log across a sheet break: a unit that started on the previous sheet gets this sheet's top depth \
(the top of its depth scale), not the original top; if the soil or rock name of such a unit is printed only on \
the previous sheet, use it for `primary` and say so in `notes`; if the hole name on this sheet is illegible \
or overwritten and the sheet clearly continues the previous one, use the previous name and list it in \
`unreadable`. Everything else comes from this page only.
- A text layer extracted from the PDF may be provided. It can be scrambled or wrong; the image is authoritative."""


LAB_SYSTEM = """You transcribe laboratory test result sheets from geotechnical site investigations (any \
language, scans, tables) into structured data. Accuracy matters more than completeness: never invent values. \
If something is not printed or not readable, use null and list it in `unreadable`.

Rules:
- One row per test value: a sample with UCS, water content and unit weight gives three rows.
- `hole_name`: the borehole / pit of the sample exactly as printed (column, row label or sheet header).
- `sample_name` and depths as printed; depths in metres below ground (convert feet). A single depth goes \
into `top_m`.
- `parameter`: the closest class (UCS/unconfined compressive strength → ucs, Brazilian/indirect tensile → \
tensile_strength, point load Is(50) → point_load_index, Cerchar → cai, permeability → hydraulic_conductivity, \
LL/PL/PI → liquid_limit/plastic_limit/plasticity_index, natural moisture → water_content, bulk/dry density or \
unit weight → unit_weight/dry_unit_weight, cohesion/friction angle from shear tests, Cu/Su → \
undrained_shear_strength, % passing 0.063/0.075 mm → fines); anything else → other.
- `value_raw` exactly as printed (keep '<', '>', ranges, decimal commas); `unit_raw` as printed in the header \
or cell. Do not convert values.
- Skip empty cells, summary statistics (mean, min, max) and test method descriptions.
- A text layer extracted from the PDF may be provided. It can be scrambled or wrong; the image is authoritative."""


def _image_block(path: Path) -> dict:
    data = base64.standard_b64encode(path.read_bytes()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def cache_path(stage: str, page: PageImage) -> Path:
    return config.CACHE_DIR / stage / page.doc_id / f"{page.page:04d}.json"


def _cache_key(*parts: str | bytes) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part if isinstance(part, bytes) else part.encode())
    return h.hexdigest()


def _classify_key(page: PageImage) -> str:
    thumb = config.PAGES_DIR / page.thumb
    return _cache_key(config.PROMPT_VERSION, config.CLASSIFY_MODEL, config.CLASSIFY_EFFORT, thumb.read_bytes(),
                      page.text[:1500])


def _extract_key(page: PageImage, kind: str, context: str) -> str:
    image = config.PAGES_DIR / page.image
    return _cache_key(config.PROMPT_VERSION, config.EXTRACT_MODEL, config.EXTRACT_EFFORT, image.read_bytes(),
                      page.text[:8000], kind, context)


def sheet_context(prev: PageExtraction | None) -> str:
    """Compact summary of the previous sheet for continuation pages ('' if there is none). Part of the
    cache key, so `rebuild` has to rebuild it from the same cached extraction."""
    if prev is None or not prev.logs:
        return ""
    lines = []
    for log in prev.logs:
        head = f"- Hole '{log.name}'" + (f", sheet {log.sheet}" if log.sheet else "")
        if log.final_depth_m is not None:
            head += f", final depth {log.final_depth_m:g} m"
        lines.append(head)
        for layer in log.layers[-2:]:
            top = "?" if layer.top_m is None else f"{layer.top_m:g}"
            bottom = "?" if layer.bottom_m is None else f"{layer.bottom_m:g}"
            text = " ".join(layer.description_original.split())[:160]
            strat = ", ".join(filter(None, [layer.stratigraphic_code, layer.stratigraphic_name]))
            lines.append(f"  last layers: {top}-{bottom} m, {layer.primary.value}: {text}" + (f" [{strat}]" if strat else ""))
    return "\n".join(lines)


def _check_stop(response) -> None:
    if response.stop_reason == "refusal":
        raise ExtractionRefused(str(response.stop_details))
    if response.stop_reason == "max_tokens":
        raise RuntimeError("output truncated (max_tokens)")


def _call_structured(model: str, effort: str, system: str, content: list[dict], out: type[T], max_tokens: int) -> T:
    """Strict structured output. Only for small schemas: the API allows at most 16 union-typed
    (nullable) fields and 24 optional fields per request."""
    response = client().beta.messages.parse(
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": content}],
        output_config={"effort": effort},
        output_format=out,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    _record_usage(response)
    _check_stop(response)
    if response.parsed_output is None:
        raise RuntimeError("no structured output")
    return response.parsed_output


def _record_usage(response) -> None:
    """Add the response's tokens to the usage of the current page (repair rounds add up). Keyed by the
    model that answered, which differs from the requested one after a refusal fallback."""
    acc = getattr(_usage, "acc", None)
    if acc is None:
        return
    model = acc.setdefault(response.model, dict.fromkeys(USAGE_FIELDS, 0))
    for field in USAGE_FIELDS:
        model[field] += getattr(response.usage, field, None) or 0


def cost_usd(usage: dict[str, dict[str, int]]) -> float | None:
    """Price of one page's usage; None if a model has no known price."""
    total = 0.0
    for model, tokens in usage.items():
        if model not in PRICES:
            return None
        inp, out = PRICES[model]
        total += (tokens["input_tokens"] * inp + tokens["cache_creation_input_tokens"] * inp * 1.25
                  + tokens["cache_read_input_tokens"] * inp * 0.1 + tokens["output_tokens"] * out) / 1e6
    return total


def measured_costs() -> dict[str, tuple[int, float]]:
    """Per stage: pages with recorded usage and their total cost in USD (from the cache)."""
    out: dict[str, tuple[int, float]] = {}
    for stage in ("classify", "extract", "lab"):
        n, total = 0, 0.0
        for path in (config.CACHE_DIR / stage).glob("*/*.json"):
            usage = json.loads(path.read_text()).get("usage")
            cost = cost_usd(usage) if usage else None
            if cost is not None:
                n, total = n + 1, total + cost
        if n:
            out[stage] = (n, total)
    return out


def _json_text(response) -> str:
    return _strip_fences("".join(b.text for b in response.content if b.type == "text"))


def _call_json(model: str, effort: str, system: str, content: list[dict], out: type[T], max_tokens: int) -> T:
    """JSON output validated locally. Used for the large extraction schema, which exceeds the
    structured-output complexity limits. One repair round with the validation error."""
    schema = json.dumps(out.model_json_schema(), ensure_ascii=False)
    system_blocks = [
        {"type": "text", "text": system},
        {
            "type": "text",
            "text": "Answer with one JSON object only, no prose and no code fences. "
            "It must validate against this JSON Schema (all listed properties are required; use null "
            f"where a value is not printed):\n{schema}",
            "cache_control": {"type": "ephemeral"},
        },
    ]
    messages: list[dict] = [{"role": "user", "content": content}]
    for attempt in range(2):
        with client().beta.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=messages,
            output_config={"effort": effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            response = stream.get_final_message()
        _record_usage(response)
        _check_stop(response)
        try:
            return out.model_validate_json(_json_text(response))
        except ValidationError as exc:
            if attempt:
                raise
            messages += [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": f"The JSON did not validate:\n{str(exc)[:3000]}\nReturn the corrected JSON object only."},
            ]
    raise AssertionError("unreachable")


def _call_claude_code(model: str, system: str, image: Path, text: str, out: type[T]) -> T:
    """Same task through the local Claude Code CLI in print mode. The page image is read with the
    Read tool (the only tool allowed); the result is validated against the Pydantic model."""
    schema = json.dumps(out.model_json_schema(), ensure_ascii=False)
    prompt = (
        f"Read the page image at {image.resolve()} and process it as instructed.\n\n{text}\n\n"
        "Return the result as structured output. Use null where a value is not printed."
    )
    cmd = [
        config.CLAUDE_CLI, "-p", prompt,
        "--output-format", "json",
        "--model", model,
        "--system-prompt", system,
        "--json-schema", schema,
        "--tools", "Read",
        "--allowedTools", "Read",
        "--add-dir", str(config.PAGES_DIR.resolve()),
        "--setting-sources", "local",  # no user plugins, hooks or memory in the worker
        "--strict-mcp-config",
        "--no-session-persistence",
    ]
    last_error = ""
    for _ in range(2):
        run_cmd = cmd if not last_error else cmd[:2] + [cmd[2] + f"\n\nA previous answer failed validation:\n{last_error}"] + cmd[3:]
        proc = subprocess.run(run_cmd, capture_output=True, text=True, timeout=900, cwd=config.ROOT, env=_cli_env())
        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise RuntimeError(f"claude CLI failed (exit {proc.returncode}): {(proc.stderr or proc.stdout)[:500]}")
        if result.get("is_error"):
            raise RuntimeError(f"claude CLI error: {str(result.get('result'))[:500]}")
        data = result.get("structured_output")
        try:
            if data is not None:
                return out.model_validate(data)
            return out.model_validate_json(_strip_fences(result.get("result") or ""))
        except ValidationError as exc:
            last_error = str(exc)[:3000]
    raise RuntimeError(f"claude CLI output did not validate: {last_error[:500]}")


def _cli_env() -> dict[str, str]:
    """Environment for the CLI: without the pipeline's API credentials, so the CLI uses its own login.
    Another subscription: set CLAUDE_CONFIG_DIR (separate profile directory, passed through) or
    CLAUDE_CLI_TOKEN_FILE (file with that profile's OAuth token)."""
    drop = {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CLI_TOKEN_FILE"}
    env = {k: v for k, v in os.environ.items() if k not in drop}
    token_file = os.getenv("CLAUDE_CLI_TOKEN_FILE")
    if token_file:
        env["ANTHROPIC_AUTH_TOKEN"] = Path(token_file).expanduser().read_text().strip()
    return env


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return text


def _cached(stage: str, page: PageImage, key: str, out: type[T], produce) -> T:
    path = cache_path(stage, page)
    if path.exists():
        cached = json.loads(path.read_text())
        if cached.get("key") == key:
            return out.model_validate(cached["result"])
    _usage.acc = {}
    try:
        result = produce()
        usage = _usage.acc
    finally:
        _usage.acc = None
    entry = {"key": key, "result": result.model_dump(mode="json")}
    if usage:  # API backend only; the CLI backend reports no token counts
        entry["usage"] = usage
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entry, ensure_ascii=False))
    return result


def _from_cache(stage: str, page: PageImage, key: str, out: type[T]) -> T | None:
    """Cached result only if it was produced from the same input and settings."""
    path = cache_path(stage, page)
    if not path.exists():
        return None
    cached = json.loads(path.read_text())
    return out.model_validate(cached["result"]) if cached.get("key") == key else None


def _lab_key(page: PageImage) -> str:
    image = config.PAGES_DIR / page.image
    return _cache_key(config.LAB_PROMPT_VERSION, config.EXTRACT_MODEL, config.EXTRACT_EFFORT, image.read_bytes(),
                      page.text[:8000])


def cached_lab_results(page: PageImage) -> PageLabResults | None:
    return _from_cache("lab", page, _lab_key(page), PageLabResults)


def extract_lab_results(page: PageImage) -> PageLabResults:
    image = config.PAGES_DIR / page.image
    hint = f"Text layer (may be empty or scrambled):\n{page.text[:8000]}"
    if config.LLM_BACKEND == "claude-code":
        produce = lambda: _call_claude_code(config.EXTRACT_MODEL, LAB_SYSTEM, image, hint, PageLabResults)
    else:
        content = [_image_block(image), {"type": "text", "text": hint}]
        produce = lambda: _call_json(config.EXTRACT_MODEL, config.EXTRACT_EFFORT, LAB_SYSTEM, content,
                                     PageLabResults, 32000)
    return _cached("lab", page, _lab_key(page), PageLabResults, produce)


def cached_classification(page: PageImage) -> PageClassification | None:
    return _from_cache("classify", page, _classify_key(page), PageClassification)


def cached_extraction(page: PageImage, kind: str, context: str = "") -> PageExtraction | None:
    return _from_cache("extract", page, _extract_key(page, kind, context), PageExtraction)


def classify_page(page: PageImage) -> PageClassification:
    thumb = config.PAGES_DIR / page.thumb
    hint = f"Text layer (may be empty or scrambled):\n{page.text[:1500]}"
    if config.LLM_BACKEND == "claude-code":
        produce = lambda: _call_claude_code(config.CLASSIFY_MODEL, CLASSIFY_SYSTEM, thumb, hint, PageClassification)
    else:
        content = [_image_block(thumb), {"type": "text", "text": hint}]
        produce = lambda: _call_structured(config.CLASSIFY_MODEL, config.CLASSIFY_EFFORT, CLASSIFY_SYSTEM, content,
                                           PageClassification, 2000)
    return _cached("classify", page, _classify_key(page), PageClassification, produce)


def extract_page(page: PageImage, kind: str, context: str = "", knowledge: str = "") -> PageExtraction:
    """knowledge: what reviewers taught about this document or region. Deliberately not part of the cache
    key: it helps the first extraction of a page, and new knowledge must not trigger re-extracting
    everything (it is also applied in code on every run, see pipeline/knowledge.py)."""
    image = config.PAGES_DIR / page.image
    hint = f"Page type (from a quick pre-check): {kind}\n\nText layer (may be empty or scrambled):\n{page.text[:8000]}"
    if context:
        hint += f"\n\nPrevious sheet of this document (context only):\n{context}"
    if knowledge:
        hint += f"\n\n{knowledge}"
    if config.LLM_BACKEND == "claude-code":
        produce = lambda: _call_claude_code(config.EXTRACT_MODEL, EXTRACT_SYSTEM, image, hint, PageExtraction)
    else:
        content = [_image_block(image), {"type": "text", "text": hint}]
        produce = lambda: _call_json(config.EXTRACT_MODEL, config.EXTRACT_EFFORT, EXTRACT_SYSTEM, content,
                                     PageExtraction, 32000)
    return _cached("extract", page, _extract_key(page, kind, context), PageExtraction, produce)
