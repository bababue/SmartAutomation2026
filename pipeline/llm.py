"""Claude calls for page classification and log extraction.

- Classification: strict structured output (small schema).
- Extraction: JSON validated locally with Pydantic; the schema is too large for strict structured
  outputs (max. 16 nullable fields per request).
- Server-side refusal fallback enabled (`fallbacks: "default"`).
- Other models: any OpenAI-compatible chat API (OpenRouter, a local Ollama server), same prompts and schemas.
- Responses are cached on disk by input hash, so re-runs cost nothing and a crash mid-run
  loses no work. Each entry also stores the call's duration and, for API calls, its token usage.
- A run can register a CallGuard for its documents: it stops calls once the run is cancelled or its
  call limit is reached, and records what each call sent (sizes, never the content).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import TypeVar

import anthropic
import pymupdf
from pydantic import BaseModel, ValidationError

from . import config
from .extraction_models import CoordinateReading, PageClassification, PageExtraction, PageLabResults
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
        # The key only: a subscription token in the environment (ANTHROPIC_AUTH_TOKEN) belongs to the CLI backend.
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=4)
    return _client


CLASSIFY_SYSTEM = """You sort pages of geotechnical site investigation documents from all over the world \
(any language, scans, handwriting, CAD exports). For each page decide what it is and list every \
borehole / test pit / probe whose log or coordinates appear on it, with names exactly as printed. \
A borehole log is a page showing depth-wise layer descriptions of one hole (often with SPT, samples, \
core recovery). Report prose that merely mentions boreholes is "text". Summary tables listing \
coordinates of several holes are "coordinate_table". If the page states the coordinate system, grid, datum \
or zone anywhere (title block, notes, legend, report text), copy that statement into `crs_text`."""

EXTRACT_SYSTEM = """You transcribe geotechnical borehole and test pit logs into structured data for a global subsurface \
database used to design tunnel boring machines. Accuracy matters more than completeness: never invent \
values. If something is printed but cannot be read reliably (faint, cut off, overwritten, handwriting in \
doubt), use null and list it in `unreadable`. A field that is blank or not printed on this page is null \
and not listed. Depths that a log only draws against its depth scale, and the top of a unit continued from \
the previous sheet, are normal readings and are not listed either. Start each `unreadable` entry with the \
field it concerns: `depth:` (layer boundaries, final depth), `lithology:` (layer description or soil/rock \
class), `name:` (hole name), `coordinates:` (coordinates, grid, datum, zone), `header:` (other header and \
title block fields: dates, method, contractor, elevation, diameter), `tests:` (samples, SPT, core runs, \
groundwater, lab values).

Rules:
- Report every log visible on the page (a page can contain several). Use the hole name exactly as printed.
- Depths are metres below ground surface along the hole. Convert feet to metres and say so in `notes`.
  If the log only shows elevations (levels), convert to depth using the ground level and say so in `notes`.
- Layers: one entry per described stratum with its top and bottom depth. On continuation sheets only \
include what is visible on this page. If the last layer continues onto the next sheet, set its bottom_m to \
the deepest depth shown on this sheet's depth scale and write "continues on next sheet" into `notes`.
- Check every layer boundary against the depth scale and the drawn boundary line, not only the printed \
digits. Small or faint digits are easily misread (1.60 vs 1.80, 3.5 vs 8.5). If the printed value and the \
scale position disagree, use the value that matches the scale and list the boundary in `unreadable`. Keep \
`description_original` verbatim in the original language and add `description_en`.
- Lithology: map the main constituent to the closest DIN 4023 class (`primary`), e.g. made ground, \
asphalt, concrete, rubble → fill; caliche, calcarenite → limestone; granite → plutonic_rock; basalt → \
volcanic_rock; core loss / no recovery → no_recovery. Secondary components with qualifiers: 'trace of' → \
trace, 'slightly'/'some' → slightly, '-y' (sandy, silty) → moderately, 'very' → very.
- Stratigraphy: fill `stratigraphic_name`/`stratigraphic_code` only if printed. `geologic_age` may be \
filled when printed or unambiguous (e.g. 'Quaternary' alluvium). `stratigraphic_color` only if the log \
shows a coloured stratigraphy column.
- Coordinates: copy the coordinate text exactly as printed into `coords_raw`, including labels (X/Y, N/E, \
Easting/Northing) and separators. Copy any datum / grid / zone text into `crs_raw`. Do not convert.
- Place: from the project and location text, give your best estimate of the latitude/longitude of the \
named site or town in `place_lat`/`place_lon` with a realistic `place_precision_km`. This is used to \
sanity-check the printed coordinates, so base it on the place names, not on the coordinates.
- SPT: keep the printed notation in `raw`; blow counts per increment in `blows`.
- Elevation: copy the printed ground level text into `elevation_raw`; `elevation_m` positive up as \
printed.
- Hole name, place and date fields carry the value as printed without the field label in front of it \
('Borehole: BH 4' → 'BH 4', 'Date: 03.05.2019' → '03.05.2019'). Copy `coords_raw`, `crs_raw` and \
`elevation_raw` exactly as printed, including any datum, grid or system text, even when it reads like a \
label.
- Country: `country` from evidence on the sheet (language, coordinate grid or datum, postcode or phone \
number format, standards cited, place names); these usually decide. Only when a place name exists in \
several countries and nothing on the sheet decides, set `country` null and give the place estimate a \
coarse `place_precision_km`.
- Header, title block, footnotes and legend: read them completely for the coordinate system / grid / datum \
/ zone (→ `crs_raw`, also when it is printed away from the coordinates, e.g. 'All coordinates MGA94 Zone \
55'), the elevation datum (→ `elevation_datum`, e.g. 'AHD', 'mOD', 'NN', 'MSL') and the groundwater \
reference point (→ `reference` of each groundwater entry: below ground, below top of casing, other).
- Evidence: for every layer and for the coordinates give `bbox`/`coords_bbox`, the region of the page \
image where the value is printed (layer: its depth labels and description row), as [x0, y0, x1, y1] \
fractions of the image width and height with the origin top left. Approximate is fine; null if you cannot \
locate it.
- Previous sheet: a short summary of the previous sheet of the same document may be given. Use it only to \
continue a log across a sheet break: a unit that started on the previous sheet gets this sheet's top depth \
(the top of its depth scale), not the original top; if the soil or rock name of such a unit is printed \
only on the previous sheet, use it for `primary` and say so in `notes`; if the hole name on this sheet is \
illegible or overwritten and the sheet clearly continues the previous one, use the previous name and list \
it in `unreadable`. Everything else comes from this page only.
- A text layer extracted from the PDF may be provided. It can be scrambled or wrong; the image is \
authoritative."""


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


class Cancelled(RuntimeError):
    pass


class CallLimitReached(RuntimeError):
    pass


@dataclass
class CallGuard:
    """The model calls of one run. cancelled(doc_id) is asked before every call; record gets one row per call."""
    cancelled: Callable[[str], bool] = lambda doc_id: False
    max_calls: int | None = None
    record: Callable[[dict], None] | None = None
    calls: int = 0
    exhausted: bool = False
    dropped: set[str] = field(default_factory=set)  # documents the run left out, set before it stores
    lock: threading.Lock = field(default_factory=threading.Lock)

    def admit(self, page: PageImage) -> None:
        if self.cancelled(page.doc_id):
            raise Cancelled(f"{page.doc_id} was cancelled")
        with self.lock:
            if self.max_calls is not None and self.calls >= self.max_calls:
                self.exhausted = True
                raise CallLimitReached(f"the limit of {self.max_calls} model calls is reached")
            self.calls += 1


_guards: dict[str, CallGuard] = {}  # by doc_id, so concurrent runs keep their own guard


@contextmanager
def guarded(doc_ids: Iterable[str], guard: CallGuard) -> Iterator[CallGuard]:
    ids = list(doc_ids)
    _guards.update(dict.fromkeys(ids, guard))
    try:
        yield guard
    finally:
        for doc_id in ids:
            _guards.pop(doc_id, None)


def endpoint() -> str:
    """Where model calls go, as recorded with each call."""
    if config.LLM_BACKEND == "claude-code":
        return "claude-code CLI"
    if config.LLM_BACKEND == "openai":
        return urllib.parse.urlsplit(config.OPENAI_BASE_URL).netloc
    return "api.anthropic.com"


def _image_block(path: Path) -> dict:
    data = base64.standard_b64encode(path.read_bytes()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def cache_path(stage: str, page: PageImage, part: str = "") -> Path:
    """part: one of several entries of a page in the stage (e.g. one per hole)."""
    return config.CACHE_DIR / stage / page.doc_id / f"{page.page:04d}{'-' + part if part else ''}.json"


def _cache_key(*parts: str | bytes) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part if isinstance(part, bytes) else part.encode())
    return h.hexdigest()


def _classify_key(page: PageImage, model: str, effort: str) -> str:
    thumb = config.PAGES_DIR / page.thumb
    return _cache_key(config.CLASSIFY_PROMPT_VERSION, model, effort, thumb.read_bytes(), page.text[:1500])


def _extract_key(page: PageImage, kind: str, context: str, model: str, effort: str) -> str:
    image = config.PAGES_DIR / page.image
    return _cache_key(config.PROMPT_VERSION, model, effort, image.read_bytes(), page.text[:8000], kind, context)


def _retry_key(page: PageImage, kind: str, context: str, model: str, effort: str) -> str:
    return _cache_key("retry", _extract_key(page, kind, context, model, effort))


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
    _anthropic_usage(response)
    _check_stop(response)
    if response.parsed_output is None:
        raise RuntimeError("no structured output")
    return response.parsed_output


def _record_usage(model: str, tokens: dict[str, float]) -> None:
    """Add a response's tokens to the usage of the current page (repair rounds add up). Keyed by the
    model that answered, which differs from the requested one after a refusal fallback."""
    acc = getattr(_usage, "acc", None)
    if acc is None:
        return
    totals = acc.setdefault(model, {})
    for field, value in tokens.items():
        totals[field] = totals.get(field, 0) + value


def _anthropic_usage(response) -> None:
    _record_usage(response.model, {f: getattr(response.usage, f, None) or 0 for f in USAGE_FIELDS})


def cost_usd(usage: dict[str, dict[str, float]]) -> float | None:
    """Price of one page's usage: as billed where the API reports it (OpenRouter), else from PRICES. None if
    a model has neither (e.g. a local model)."""
    total = 0.0
    for model, tokens in usage.items():
        if "cost_usd" in tokens:
            total += tokens["cost_usd"]
            continue
        if model not in PRICES:
            return None
        inp, out = PRICES[model]
        total += (tokens.get("input_tokens", 0) * inp + tokens.get("cache_creation_input_tokens", 0) * inp * 1.25
                  + tokens.get("cache_read_input_tokens", 0) * inp * 0.1 + tokens.get("output_tokens", 0) * out) / 1e6
    return total


def measured_costs() -> dict[str, tuple[int, float]]:
    """Per stage: pages with recorded usage and their total cost in USD (from the cache)."""
    out: dict[str, tuple[int, float]] = {}
    for stage in ("classify", "extract", "extract-retry", "lab", "coords"):
        n, total = 0, 0.0
        for path in (config.CACHE_DIR / stage).glob("*/*.json"):
            usage = (_entry(path) or {}).get("usage")
            cost = cost_usd(usage) if usage else None
            if cost is not None:
                n, total = n + 1, total + cost
        if n:
            out[stage] = (n, total)
    return out


def _json_text(response) -> str:
    return _strip_fences("".join(b.text for b in response.content if b.type == "text"))


def _json_instruction(out: type[BaseModel]) -> str:
    schema = json.dumps(out.model_json_schema(), ensure_ascii=False)
    return ("Answer with one JSON object only, no prose and no code fences. It must validate against this JSON "
            f"Schema (all listed properties are required; use null where a value is not printed):\n{schema}")


def _repair_prompt(exc: ValidationError) -> str:
    return f"The JSON did not validate:\n{str(exc)[:3000]}\nReturn the corrected JSON object only."


def _call_json(model: str, effort: str, system: str, content: list[dict], out: type[T], max_tokens: int) -> T:
    """JSON output validated locally. Used for the large extraction schema, which exceeds the
    structured-output complexity limits. One repair round with the validation error."""
    system_blocks = [
        {"type": "text", "text": system},
        {"type": "text", "text": _json_instruction(out), "cache_control": {"type": "ephemeral"}},
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
        _anthropic_usage(response)
        _check_stop(response)
        try:
            return out.model_validate_json(_json_text(response))
        except ValidationError as exc:
            if attempt:
                raise
            messages += [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": _repair_prompt(exc)},
            ]
    raise AssertionError("unreachable")


def _call_openai(model: str, system: str, image: Path, text: str, out: type[T], max_tokens: int, strict: bool) -> T:
    """OpenAI-compatible chat completions. The schema is in the prompt for its field descriptions. strict: also
    in `response_format`, without the `strict` flag, so whether decoding follows it is up to the provider; the
    large extraction schema gets JSON mode, as providers reject it there (as Anthropic's structured outputs do).
    Every answer is validated locally, with one repair round."""
    data = base64.standard_b64encode(image.read_bytes()).decode()
    messages: list[dict] = [
        {"role": "system", "content": f"{system}\n\n{_json_instruction(out)}"},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}},
            {"type": "text", "text": text},
        ]},
    ]
    response_format = ({"type": "json_schema", "json_schema": {"name": out.__name__, "schema": out.model_json_schema()}}
                       if strict else {"type": "json_object"})
    body = {"model": model, "max_tokens": max_tokens, "response_format": response_format}
    if config.OPENROUTER_ZDR and "openrouter.ai" in config.OPENAI_BASE_URL:
        body["provider"] = {"zdr": True}
    for attempt in range(2):
        response = _post_chat({**body, "messages": messages})
        usage = response.get("usage") or {}
        tokens = {"input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0)}
        if usage.get("cost") is not None:
            tokens["cost_usd"] = usage["cost"]
        _record_usage(response.get("model") or model, tokens)
        choice = response["choices"][0]
        if choice.get("finish_reason") == "length":
            raise RuntimeError("output truncated (max_tokens)")
        answer = _strip_fences(choice["message"].get("content") or "")
        try:
            return out.model_validate_json(answer)
        except ValidationError as exc:
            if attempt:
                raise
            messages += [{"role": "assistant", "content": answer}, {"role": "user", "content": _repair_prompt(exc)}]
    raise AssertionError("unreachable")


def _post_chat(body: dict) -> dict:
    headers = {"Content-Type": "application/json"}
    if key := os.getenv("OPENAI_API_KEY"):
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(f"{config.OPENAI_BASE_URL.rstrip('/')}/chat/completions",
                                     data=json.dumps(body).encode(), headers=headers)
    for attempt in range(4):
        try:
            # A local model on a laptop needs minutes for a dense page.
            with urllib.request.urlopen(request, timeout=1800) as resp:
                result = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 3:
                raise RuntimeError(f"chat API HTTP {exc.code}: {exc.read()[:500].decode(errors='replace')}") from None
            time.sleep(5 * 2**attempt)
            continue
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if attempt == 3:
                raise RuntimeError(f"chat API unreachable: {exc}") from None
            time.sleep(5 * 2**attempt)
            continue
        if "error" in result:  # OpenRouter reports upstream failures in a 200 response
            raise RuntimeError(f"chat API error: {str(result['error'])[:500]}")
        return result
    raise AssertionError("unreachable")


def _call_claude_code(model: str, system: str, image: Path, text: str, out: type[T]) -> T:
    """Same task through the local Claude Code CLI in print mode. The page image is read with the Read tool,
    the only tool, which `--restricted` confines to the working directory: a temporary folder holding nothing
    but a copy of the image, so text on a page cannot make it read other files. The result is validated
    against the Pydantic model."""
    schema = json.dumps(out.model_json_schema(), ensure_ascii=False)
    with tempfile.TemporaryDirectory(prefix="page-") as folder:
        workdir = Path(folder).resolve()
        page = workdir / f"page{image.suffix}"
        shutil.copyfile(image, page)
        prompt = (
            f"Read the page image at {page} and process it as instructed.\n\n{text}\n\n"
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
            "--restricted",  # also no user, project or local settings: no plugins, hooks or memory in the worker
            "--strict-mcp-config",
            "--no-session-persistence",
        ]
        last_error = ""
        for _ in range(2):
            run_cmd = cmd if not last_error else cmd[:2] + [cmd[2] + f"\n\nA previous answer failed validation:\n{last_error}"] + cmd[3:]
            proc = subprocess.run(run_cmd, capture_output=True, text=True, encoding="utf-8", timeout=900,
                                  cwd=workdir, env=_cli_env())
            try:
                result = json.loads(proc.stdout)
            except json.JSONDecodeError:
                raise RuntimeError(f"claude CLI failed (exit {proc.returncode}): {(proc.stderr or proc.stdout)[:500]}")
            if result.get("is_error"):
                raise RuntimeError(f"claude CLI error: {str(result.get('result'))[:500]}")
            tokens = {f: result.get("usage", {}).get(f) or 0 for f in USAGE_FIELDS}
            if any(tokens.values()):  # the subscription is not billed per token: tokens only, priced from PRICES
                _record_usage(model, tokens)
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
        env["ANTHROPIC_AUTH_TOKEN"] = Path(token_file).expanduser().read_text(encoding="utf-8").strip()
    return env


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return text


def _cached(stage: str, page: PageImage, key_for: Callable[[str, str], str], model: str, effort: str, out: type[T],
            produce: Callable[[], T], part: str = "") -> T:
    """The cached answer of the configured model, else a new call. key_for(model, effort) is the cache key."""
    path = cache_path(stage, page, part)
    key = key_for(model, effort)
    cached = _entry(path)
    if cached and cached.get("key") == key:
        return out.model_validate(cached["result"])
    guard = _guards.get(page.doc_id)
    if guard:
        guard.admit(page)
    _usage.acc = {}
    start = time.monotonic()
    outcome, usage = "error", {}
    try:
        result = produce()
        outcome, usage = "ok", _usage.acc
    finally:
        _usage.acc = None
        if guard and guard.record:
            sent = getattr(produce, "sent", {})
            tokens = lambda f: int(sum(t.get(f, 0) for t in usage.values()))
            guard.record({"at": datetime.now(timezone.utc).isoformat(), "doc_id": page.doc_id, "page": page.page,
                          "stage": stage, "model": model, "endpoint": endpoint(), **sent,
                          "input_tokens": tokens("input_tokens"), "output_tokens": tokens("output_tokens"),
                          "cost_usd": cost_usd(usage) if usage else None,
                          "seconds": round(time.monotonic() - start, 1), "outcome": outcome})
    entry = {"key": key, "model": model, "effort": effort, "result": result.model_dump(mode="json"),
             "seconds": round(time.monotonic() - start, 1)}
    if usage:
        entry["usage"] = usage
    path.parent.mkdir(parents=True, exist_ok=True)
    part_path = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.part")
    part_path.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    os.replace(part_path, path)
    return result


def _entry(path: Path) -> dict | None:
    """The cache entry at path; a missing or unreadable one (cut short by a crash) is a miss."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def _from_cache(stage: str, page: PageImage, key_for: Callable[[str, str], str], model: str, effort: str,
                out: type[T], part: str = "") -> T | None:
    """Cached result if it was produced from the same input and prompt, checked with the model and effort recorded
    in the entry: a rebuild keeps the pages extracted before the configured model changed (a run re-extracts
    them, see _cached). Entries without a record count as made with the configured settings."""
    cached = _entry(cache_path(stage, page, part))
    if cached is None:
        return None
    key = key_for(cached.get("model", model), cached.get("effort", effort))
    return out.model_validate(cached["result"]) if cached.get("key") == key else None


def _lab_key(page: PageImage, model: str, effort: str) -> str:
    image = config.PAGES_DIR / page.image
    return _cache_key(config.LAB_PROMPT_VERSION, model, effort, image.read_bytes(), page.text[:8000])


def _produce(model: str, effort: str, system: str, image: Path, hint: str, out: type[T], max_tokens: int,
             strict: bool = False) -> Callable[[], T]:
    """The call for the configured backend. strict: a small schema, for strict structured output. The call
    carries what it sends (`sent`) for the record a CallGuard keeps."""
    if config.LLM_BACKEND == "claude-code":
        call = lambda: _call_claude_code(model, system, image, hint, out)
    elif config.LLM_BACKEND == "openai":
        call = lambda: _call_openai(model, system, image, hint, out, max_tokens, strict)
    else:
        content = [_image_block(image), {"type": "text", "text": hint}]
        call = partial(_call_structured if strict else _call_json, model, effort, system, content, out, max_tokens)
    call.sent = {"image_bytes": image.stat().st_size, "text_chars": len(hint)}
    return call


def cached_lab_results(page: PageImage) -> PageLabResults | None:
    return _from_cache("lab", page, partial(_lab_key, page), config.EXTRACT_MODEL, config.EXTRACT_EFFORT,
                       PageLabResults)


def extract_lab_results(page: PageImage) -> PageLabResults:
    image = config.PAGES_DIR / page.image
    hint = f"Text layer (may be empty or scrambled):\n{page.text[:8000]}"
    produce = _produce(config.EXTRACT_MODEL, config.EXTRACT_EFFORT, LAB_SYSTEM, image, hint, PageLabResults, 32000)
    return _cached("lab", page, partial(_lab_key, page), config.EXTRACT_MODEL, config.EXTRACT_EFFORT,
                   PageLabResults, produce)


def cached_classification(page: PageImage) -> PageClassification | None:
    return _from_cache("classify", page, partial(_classify_key, page), config.CLASSIFY_MODEL, config.CLASSIFY_EFFORT,
                       PageClassification)


def cached_extraction(page: PageImage, kind: str, context: str = "", expected: int = 0) -> PageExtraction | None:
    """expected: logs the classification counted; a cached retry with more logs wins (see extract_page)."""
    x = _from_cache("extract", page, partial(_extract_key, page, kind, context), config.EXTRACT_MODEL,
                    config.EXTRACT_EFFORT, PageExtraction)
    if x is None or len(x.logs) >= expected:
        return x
    retry = _from_cache("extract-retry", page, partial(_retry_key, page, kind, context), config.EXTRACT_MODEL,
                        config.EXTRACT_EFFORT, PageExtraction)
    return retry if retry is not None and len(retry.logs) > len(x.logs) else x


def classify_page(page: PageImage) -> PageClassification:
    thumb = config.PAGES_DIR / page.thumb
    hint = f"Text layer (may be empty or scrambled):\n{page.text[:1500]}"
    produce = _produce(config.CLASSIFY_MODEL, config.CLASSIFY_EFFORT, CLASSIFY_SYSTEM, thumb, hint,
                       PageClassification, 2000, strict=True)
    return _cached("classify", page, partial(_classify_key, page), config.CLASSIFY_MODEL, config.CLASSIFY_EFFORT,
                   PageClassification, produce)


def extract_page(page: PageImage, kind: str, context: str = "", knowledge: str = "",
                 expected: int = 0, retry: bool = True) -> PageExtraction:
    """knowledge: what reviewers taught about this document or region. Deliberately not part of the cache
    key: it helps the first extraction of a page, and new knowledge must not trigger re-extracting
    everything (it is also applied in code on every run, see pipeline/knowledge.py).
    expected: logs the classification counted. A page that comes back with fewer is read once more: the same
    request sometimes returns no log at all. The retry has its own cache stage, so the first answer stays.
    retry=False uses a cached retry only (no call)."""
    image = config.PAGES_DIR / page.image
    hint = f"Page type (from a quick pre-check): {kind}\n\nText layer (may be empty or scrambled):\n{page.text[:8000]}"
    if context:
        hint += f"\n\nPrevious sheet of this document (context only):\n{context}"
    if knowledge:
        hint += f"\n\n{knowledge}"
    produce = _produce(config.EXTRACT_MODEL, config.EXTRACT_EFFORT, EXTRACT_SYSTEM, image, hint, PageExtraction, 32000)
    x = _cached("extract", page, partial(_extract_key, page, kind, context), config.EXTRACT_MODEL,
                config.EXTRACT_EFFORT, PageExtraction, produce)
    if len(x.logs) >= expected:
        return x
    if not retry:
        return cached_extraction(page, kind, context, expected) or x
    again = _cached("extract-retry", page, partial(_retry_key, page, kind, context), config.EXTRACT_MODEL,
                    config.EXTRACT_EFFORT, PageExtraction, produce)
    return again if len(again.logs) > len(x.logs) else x


COORDS_SYSTEM = """You check the printed coordinates of one borehole on a geotechnical log or coordinate table: \
the image is a cut-out around them (or the whole page). Copy them verbatim. `coords_raw`: the coordinate \
text exactly as printed, including axis labels (X/Y, N/E, Easting/Northing, R/H), separators, units and \
hemisphere letters. `crs_raw`: any coordinate system, grid, datum or zone text printed with them or on the \
page. Do not convert, round or correct anything. If a hole name is given, read that hole's coordinates only. \
If no coordinates are legible, both fields are null."""

# Margin around the model's coordinate box, as a share of the page: the box is approximate.
COORDS_MARGIN = 0.04
COORDS_LONG_EDGE = 1600


def coordinate_image(page: PageImage, bbox: tuple[float, float, float, float] | None) -> Path:
    """The region of the page image around `bbox` (fractions of the page), enlarged; the whole page without one."""
    image = config.PAGES_DIR / page.image
    if bbox is None:
        return image
    x0, y0, x1, y1 = bbox
    m = COORDS_MARGIN
    box = (max(0.0, x0 - m), max(0.0, y0 - m), min(1.0, x1 + m), min(1.0, y1 + m))
    name = _cache_key(image.read_bytes(), repr(box))[:16]
    out = config.PAGES_DIR / "crops" / page.doc_id / f"{page.page:04d}-{name}.jpg"
    if not out.exists():
        with pymupdf.open(image) as doc:
            rect = doc[0].rect
            clip = pymupdf.Rect(box[0] * rect.width, box[1] * rect.height, box[2] * rect.width, box[3] * rect.height)
            zoom = min(4.0, COORDS_LONG_EDGE / max(clip.width, clip.height, 1))
            pix = doc[0].get_pixmap(clip=clip, matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        out.parent.mkdir(parents=True, exist_ok=True)
        part = out.with_name(f"{out.stem}.{threading.get_ident()}.part.jpg")  # parallel readings of one box
        pix.save(part, jpg_quality=90)
        os.replace(part, out)
    return out


def _coords_key(page: PageImage, bbox: tuple | None, hole: str, model: str, effort: str) -> str:
    image = config.PAGES_DIR / page.image
    return _cache_key(config.COORDS_PROMPT_VERSION, model, effort, image.read_bytes(), repr(bbox), hole)


def _coords_part(hole: str, bbox: tuple | None) -> str:
    return _cache_key(hole, repr(bbox))[:12]


def cached_coordinates(page: PageImage, bbox: tuple | None, hole: str) -> CoordinateReading | None:
    return _from_cache("coords", page, partial(_coords_key, page, bbox, hole), config.EXTRACT_MODEL,
                       config.COORDS_EFFORT, CoordinateReading, _coords_part(hole, bbox))


def read_coordinates(page: PageImage, bbox: tuple | None, hole: str) -> CoordinateReading:
    """A second, independent reading of one hole's coordinates (its own prompt and cache key, so no
    PROMPT_VERSION change re-reads them)."""
    hint = f"Hole: {hole}" if hole else "Read the coordinates printed on this page."
    produce = _produce(config.EXTRACT_MODEL, config.COORDS_EFFORT, COORDS_SYSTEM, coordinate_image(page, bbox),
                       hint, CoordinateReading, 2000, strict=True)
    return _cached("coords", page, partial(_coords_key, page, bbox, hole), config.EXTRACT_MODEL,
                   config.COORDS_EFFORT, CoordinateReading, produce, _coords_part(hole, bbox))
