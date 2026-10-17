"""Synthetic boreholes at public places for UI development and tests (no challenge data).

    DB_PATH=data/demo.sqlite .venv/bin/python -m fixtures.demo_data
"""

from __future__ import annotations

import random

from pipeline import store
from pipeline.scoring import score
from schema.borehole import (
    SPT,
    Borehole,
    GeorefMethod,
    GroundwaterObservation,
    InvestigationType,
    Layer,
    Location,
    SecondaryComponent,
    SourceRef,
)

SITES = [
    ("Offenburg", "DE", 48.4730, 7.9440, 152.3),
    ("Freiburg", "DE", 47.9990, 7.8421, 278.0),
    ("Auckland", "NZ", -36.8485, 174.7622, 42.0),
    ("Sydney", "AU", -33.8568, 151.2153, 12.5),
    ("Kuwait City", "KW", 29.3759, 47.9774, 5.0),
    ("Busan", "KR", 35.1151, 129.0422, 6.1),
    ("London", "GB", 51.5007, -0.1246, 4.2),
    ("Baku", "AZ", 40.4093, 49.8671, -28.0),
    ("São Paulo", "BR", -23.5505, -46.6333, 760.0),
    ("Bergen", "NO", 60.3913, 5.3221, 12.0),
]

PROFILES = [
    [("fill", None, "Made ground, sandy gravel with brick fragments", None, None),
     ("silt", "sand", "Silt, slightly sandy, stiff, brown", "qhy", "Holocene"),
     ("gravel", "sand", "Gravel, sandy, medium dense, grey", "qORT", "Pleistocene"),
     ("sand", "gravel", "Coarse sand, slightly gravelly, dense", "qORT", "Pleistocene"),
     ("clay", "silt", "Clay, silty, stiff, dark grey", "tOM", "Oligocene")],
    [("topsoil", None, "Topsoil, dark brown", None, None),
     ("clay", "sand", "Sandy clay, medium plasticity", "Qa", "Quaternary"),
     ("sandstone", None, "Sandstone, slightly weathered, grey, jointed", "Tw", "Miocene"),
     ("siltstone", None, "Siltstone, fresh, strong", "Tw", "Miocene")],
    [("fill", None, "Fill, asphalt and crushed rock", None, None),
     ("sand", "silt", "Silty sand, loose, calcareous, light brown", "Qs", "Quaternary"),
     ("limestone", None, "Calcarenite, weak, fractured", "Ng", "Neogene")],
    [("sand", None, "Sand, fine, saturated, with shell debris", "Q", "Quaternary"),
     ("clay", None, "Clay, grey, firm", "Q", "Quaternary"),
     ("plutonic_rock", None, "Granite, slightly weathered, very strong", "Kg", "Cretaceous")],
]


# A synthetic tunnel route east of Offenburg: holes 15-25 m apart over a granite rock head that dips
# and rises again, for the section view. (offset east in m, rock head m, ground level m)
ROUTE = [(0, 16.0, 151.8), (18, 14.6, 152.0), (36, 12.4, 152.3), (55, 10.8, 152.9), (72, 9.1, 153.4),
         (90, 9.6, 153.6), (108, 11.2, 153.5), (125, 13.8, 153.1), (145, None, 152.8), (400, 7.5, 155.0)]


def tunnel_route(rnd: random.Random) -> list[Borehole]:
    lat0, lon0 = 48.4690, 7.9600
    out = []
    for k, (east, rock, elev) in enumerate(ROUTE):
        name = f"RB-{k + 1:02d}"
        sand = round(rnd.uniform(3.0, 5.0), 1)
        bottom = rock + 6 if rock is not None else 9.0  # one hole stops in the clay
        rows = [(0.0, 0.8, "fill", None, "Made ground, gravelly"),
                (0.8, sand, "gravel", None, "Gravel, sandy, medium dense"),
                (sand, rock - 1.2 if rock else bottom, "clay", None, "Clay, silty, stiff")]
        if rock is not None:
            rows += [(rock - 1.2, rock, "plutonic_rock", "completely weathered (W5)", "Granite, decomposed to sand"),
                     (rock, bottom, "plutonic_rock", "slightly weathered", "Granite, strong, jointed")]
        layers = [Layer(top_m=round(t, 2), bottom_m=round(b, 2), description_original=d, primary=lith,
                        weathering=w, source=SourceRef(file="demo", page=1)) for t, b, lith, w, d in rows]
        bh = Borehole(
            borehole_id=f"demo-route:{name}", borehole_name=name, investigation_type=InvestigationType.borehole,
            project_name="Demo tunnel route (synthetic)", location_name="Offenburg",
            location=Location(lat=lat0, lon=lon0 + east / (111_320 * 0.663), country="DE",
                              method=GeorefMethod.stated_coordinates, confidence=0.95, crs_epsg=4326,
                              coords_raw="synthetic"),
            surface_elevation_m=elev, elevation_datum="mASL", depth_m=bottom, layers=layers,
            groundwater=[GroundwaterObservation(depth_m=round(rnd.uniform(3.0, 4.2), 1), kind="standing")],
            sources=[SourceRef(file="demo", page=1)],
        )
        bh.issues = bh.validate_log()
        score(bh, {}, unreadable=0)
        out.append(bh)
    return out


def build(seed: int = 7) -> list[Borehole]:
    rnd = random.Random(seed)
    out = []
    for i, (place, country, lat, lon, elev) in enumerate(SITES):
        for j in range(rnd.randint(2, 5)):
            profile = PROFILES[(i + j) % len(PROFILES)]
            depth, layers = 0.0, []
            for primary, secondary, desc, code, age in profile:
                thickness = round(rnd.uniform(0.6, 6.0), 1)
                layers.append(Layer(
                    top_m=depth, bottom_m=round(depth + thickness, 2), description_original=desc,
                    primary=primary, secondary=[SecondaryComponent(lithology=secondary, qualifier="slightly")] if secondary else [],
                    stratigraphic_code=code, geologic_age=age, source=SourceRef(file="demo", page=1),
                ))
                depth = round(depth + thickness, 2)
            name = f"BH-{j + 1}"
            bh = Borehole(
                borehole_id=f"demo-{place.lower().replace(' ', '-')}:{name}",
                borehole_name=name,
                investigation_type=InvestigationType.borehole,
                project_name=f"Demo project {place}",
                location_name=place,
                location=Location(
                    lat=lat + rnd.uniform(-0.01, 0.01), lon=lon + rnd.uniform(-0.01, 0.01), country=country,
                    method=GeorefMethod.stated_coordinates if j else GeorefMethod.geocoded_place,
                    confidence=0.95 if j else 0.3, crs_epsg=4326, coords_raw="synthetic",
                ),
                surface_elevation_m=elev, elevation_datum="mASL", depth_m=depth,
                layers=layers,
                spt=[SPT(depth_m=d, blows=[rnd.randint(2, 12) for _ in range(3)], n_value=rnd.randint(5, 40),
                         raw="synthetic") for d in (1.5, 3.0, 4.5) if d < depth],
                groundwater=[GroundwaterObservation(depth_m=round(rnd.uniform(1, 4), 1), kind="standing")],
                sources=[SourceRef(file="demo", page=1)],
            )
            if j == 2 and i % 3 == 0 and len(bh.layers) > 2:
                bh.layers[1].bottom_m -= 0.4  # a gap, as a misread boundary would produce
            bh.issues = bh.validate_log() + (["position from place name only"] if not j else [])
            score(bh, {}, unreadable=rnd.choice([0, 0, 0, 0, 1, 3]))
            out.append(bh)
    return out + tunnel_route(rnd)


def main() -> None:
    con = store.connect()
    store.reset(con)
    for bh in build():
        store.save_borehole(con, bh)
    print(f"stored {con.execute('SELECT COUNT(*) FROM boreholes').fetchone()[0]} demo boreholes")


if __name__ == "__main__":
    main()
