"""Request bodies of the HTTP API."""

from __future__ import annotations

import math
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from pipeline import corrections
from schema.borehole import LabParameter, Lithology

Reviewer = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]


class Body(BaseModel):
    # A non-finite number would be stored as JSON null, which the record then refuses on every read.
    model_config = ConfigDict(allow_inf_nan=False)


class Review(Body):
    decision: Literal["approved", "rejected", "pending_review"]
    reviewer: Reviewer
    comment: str | None = Field(None, max_length=2000)
    content_hash: str = Field(min_length=1, description="Version the reviewer saw; refused if it changed since")
    review_revision: int = Field(description="Review state the reviewer saw; refused if another decision was "
                                 "stored since")


class Correction(Body):
    kind: Literal["crs", "position", "layer", "layer_add", "field", "groundwater", "test"]
    scope: Literal["borehole", "document"] = "borehole"
    payload: dict
    reviewer: Reviewer
    comment: str | None = Field(None, max_length=2000)


class CrsFix(Body):
    epsg: int


class PositionFix(Body):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    accuracy_m: float | None = Field(None, gt=0)


class LayerFix(Body):
    orig_top: float
    orig_bottom: float
    top: float | None = None
    bottom: float | None = None
    primary: Lithology | None = None
    delete: bool = False
    description_original: str | None = Field(None, max_length=4000)
    stratigraphic_code: str | None = Field(None, max_length=80)
    stratigraphic_name: str | None = Field(None, max_length=200)
    geologic_age: str | None = Field(None, max_length=200)

    @model_validator(mode="after")
    def _check(self):
        if (self.top is None) != (self.bottom is None):
            raise ValueError("give both top and bottom")
        if self.top is not None and not 0 <= self.top < self.bottom:
            raise ValueError("top must be at least 0 and above bottom")
        texts = (self.description_original, self.stratigraphic_code, self.stratigraphic_name, self.geologic_age)
        if not self.delete and self.top is None and self.primary is None and all(t is None for t in texts):
            raise ValueError("nothing to change")
        return self


class LayerAddFix(Body):
    top: float = Field(ge=0)
    bottom: float
    primary: Lithology
    description_original: str | None = Field(None, max_length=4000)

    @model_validator(mode="after")
    def _check(self):
        if self.bottom <= self.top:
            raise ValueError("bottom must be below top")
        return self


class FieldFix(Body):
    field: Literal[tuple(corrections.FIELDS)]  # type: ignore[valid-type]
    value: str | float | None

    @model_validator(mode="after")
    def _check(self):
        kind = corrections.FIELDS[self.field]
        if self.value is None or self.value == "":
            self.value = None
        elif kind is float:
            self.value = float(self.value)
        elif kind is date:
            self.value = date.fromisoformat(str(self.value)).isoformat()
        else:
            self.value = str(self.value).strip()[:4000 if self.field == "notes" else 300]
        if self.field == "borehole_name" and not self.value:
            raise ValueError("a borehole name cannot be empty")
        if kind is float and self.value is not None and not math.isfinite(self.value):
            raise ValueError("numeric fields must be finite")
        if self.field == "depth_m" and self.value is not None and self.value < 0:
            raise ValueError("a depth cannot be negative")
        return self


class GroundwaterFix(Body):
    depth_m: float | None = Field(None, ge=0)
    dry: bool = False

    @model_validator(mode="after")
    def _check(self):
        if self.depth_m is None and not self.dry:
            raise ValueError("give a depth or mark the hole as dry")
        return self


class ResultFix(Body):
    test: Literal["spt", "core", "lab"]
    orig_depth: float
    parameter: LabParameter | None = None
    delete: bool = False
    value: float | None = None
    rqd: float | None = Field(None, ge=0, le=100)

    @model_validator(mode="after")
    def _check(self):
        if (self.test == "lab") != (self.parameter is not None):
            raise ValueError("a parameter is given for lab results only, and always")
        if not self.delete and self.value is None:
            raise ValueError("give a value or delete the result")
        if not self.delete and self.test != "lab" and not (0 <= self.value <= (100 if self.test == "core" else 1000)):
            raise ValueError("value out of range")
        if not self.delete and self.test == "spt" and self.value != int(self.value):
            raise ValueError("an SPT N value is a whole number")
        return self


# The payload model per Correction.kind.
FIXES: dict[str, type[Body]] = {
    "crs": CrsFix, "position": PositionFix, "layer": LayerFix, "layer_add": LayerAddFix, "field": FieldFix,
    "groundwater": GroundwaterFix, "test": ResultFix,
}


class CancelBody(Body):
    files: list[str] | None = None  # paths as the upload reported them; none: the whole batch
