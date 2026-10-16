"""Claude calls for page classification and log extraction.

- Classification: strict structured output (small schema).
- Extraction: JSON validated locally with Pydantic; the schema is too large for strict structured
  outputs (max. 16 nullable fields per request).
- Server-side refusal fallback enabled (`fallbacks: "default"`).
- Responses are cached on disk by input hash, so re-runs cost nothing and a crash mid-run
  loses no work.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from . import config
from .extraction_models import PageClassification, PageExtraction
from .ingest import PageImage

T = TypeVar("T", bound=BaseModel)

_client: anthropic.Anthropic | None = None


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
coordinates of several holes are "coordinate_table"."""

EXTRACT_SYSTEM = """You transcribe geotechnical borehole and test pit logs into structured data for a \
global subsurface database used to design tunnel boring machines. Accuracy matters more than \
completeness: never invent values. If something is not printed or not readable, use null and list it \
in `unreadable`.

Rules:
- Report every log visible on the page (a page can contain several). Use the hole name exactly as printed.
- Depths are metres below ground surface along the hole. Convert feet to metres and say so in `notes`.
  If the log only shows elevations (levels), convert to depth using the ground level and say so in `notes`.
- Layers: one entry per described stratum with its top and bottom depth. On continuation sheets only \
include what is visible on this page. Keep `description_original` verbatim in the original language and \
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
- A text layer extracted from the PDF may be provided. It can be scrambled or wrong; the image is authoritative."""


def _image_block(path: Path) -> dict:
    data = base64.standard_b64encode(path.read_bytes()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def _cache_path(stage: str, page: PageImage) -> Path:
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


def _extract_key(page: PageImage, kind: str) -> str:
    image = config.PAGES_DIR / page.image
    return _cache_key(config.PROMPT_VERSION, config.EXTRACT_MODEL, config.EXTRACT_EFFORT, image.read_bytes(),
                      page.text[:8000], kind)


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
    _check_stop(response)
    if response.parsed_output is None:
        raise RuntimeError("no structured output")
    return response.parsed_output


def _json_text(response) -> str:
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return text


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


def _cached(stage: str, page: PageImage, key: str, out: type[T], produce) -> T:
    path = _cache_path(stage, page)
    if path.exists():
        cached = json.loads(path.read_text())
        if cached.get("key") == key:
            return out.model_validate(cached["result"])
    result = produce()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"key": key, "result": result.model_dump(mode="json")}, ensure_ascii=False))
    return result


def _from_cache(stage: str, page: PageImage, key: str, out: type[T]) -> T | None:
    """Cached result only if it was produced from the same input and settings."""
    path = _cache_path(stage, page)
    if not path.exists():
        return None
    cached = json.loads(path.read_text())
    return out.model_validate(cached["result"]) if cached.get("key") == key else None


def cached_classification(page: PageImage) -> PageClassification | None:
    return _from_cache("classify", page, _classify_key(page), PageClassification)


def cached_extraction(page: PageImage, kind: str) -> PageExtraction | None:
    return _from_cache("extract", page, _extract_key(page, kind), PageExtraction)


def classify_page(page: PageImage) -> PageClassification:
    thumb = config.PAGES_DIR / page.thumb
    content = [_image_block(thumb), {"type": "text", "text": f"Text layer (may be empty or scrambled):\n{page.text[:1500]}"}]
    return _cached(
        "classify",
        page,
        _classify_key(page),
        PageClassification,
        lambda: _call_structured(config.CLASSIFY_MODEL, config.CLASSIFY_EFFORT, CLASSIFY_SYSTEM, content,
                                 PageClassification, 2000),
    )


def extract_page(page: PageImage, kind: str) -> PageExtraction:
    image = config.PAGES_DIR / page.image
    content = [
        _image_block(image),
        {
            "type": "text",
            "text": f"Page type (from a quick pre-check): {kind}\n\nText layer (may be empty or scrambled):\n{page.text[:8000]}",
        },
    ]
    return _cached(
        "extract",
        page,
        _extract_key(page, kind),
        PageExtraction,
        lambda: _call_json(config.EXTRACT_MODEL, config.EXTRACT_EFFORT, EXTRACT_SYSTEM, content, PageExtraction, 32000),
    )
