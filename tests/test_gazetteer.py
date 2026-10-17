"""Place lookup in a mini gazetteer of public places, and its use in georeferencing."""

import io
import zipfile

import pytest
from pyproj import Transformer

from pipeline import config, gazetteer
from pipeline.georef import georeference

# GeoNames columns: id, name, ascii name, alternate names, lat, lon, class, code, country, cc2, admin1-4, population
PLACES = [
    (2925177, "Freiburg", "Freiburg", "Freiburg im Breisgau,Fribourg-en-Brisgau", 47.9959, 7.85222, "DE", "01", 215966),
    (2857798, "Offenburg", "Offenburg", "", 48.47377, 7.94495, "DE", "01", 59646),
    (2950159, "Berlin", "Berlin", "", 52.52437, 13.41053, "DE", "16", 3426354),
    (4250542, "Springfield", "Springfield", "", 39.80172, -89.64371, "US", "IL", 116250),
    (4951788, "Springfield", "Springfield", "", 42.10148, -72.58981, "US", "MA", 155929),
    (2849647, "Bad Reichenhall", "Bad Reichenhall", "Bavaria", 47.72947, 12.87819, "DE", "02", 17000),  # alias invented
]
REGIONS = "DE.01\tBaden-Wuerttemberg\tBaden-Wuerttemberg\t2953481\nDE.02\tBavaria\tBavaria\t2951839\n"
MINSTER = (47.9955, 7.8529)  # Freiburg Minster


@pytest.fixture()
def gaz(tmp_path, monkeypatch):
    rows = ["\t".join(map(str, (i, n, a, alt, lat, lon, "P", "PPL", cc, "", adm, "", "", "", pop, "", "", "", "")))
            for i, n, a, alt, lat, lon, cc, adm, pop in PLACES]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("cities1000.txt", "\n".join(rows) + "\n")
    path = tmp_path / "gazetteer.sqlite"
    gazetteer.build(buf.getvalue(), REGIONS, path)
    monkeypatch.setattr(config, "GAZETTEER_PATH", path)


def utm32(lat: float, lon: float) -> str:
    e, n = Transformer.from_crs(4326, 25832, always_xy=True).transform(lon, lat)
    return f"E {e:.1f} N {n:.1f}"


def test_lookup_takes_the_town_not_the_street_or_region(gaz):
    assert gazetteer.lookup("Hauptstrasse 4, Freiburg im Breisgau", "DE").place.name == "Freiburg"
    assert gazetteer.lookup("Offenburg, Baden-Wuerttemberg", "DE").place.name == "Offenburg"
    assert gazetteer.lookup("Site near the river, Bavaria", "DE").place is None  # a state, not the town aliased so
    assert gazetteer.lookup("Rua Berlin / Rua Offenburg", "DE").place is None  # a crossing of streets
    assert [p.country for p in gazetteer.lookup("Offenburg", "FR").elsewhere] == ["DE"]
    assert len(gazetteer.lookup("Springfield", "US").ambiguous) == 2


def test_without_gazetteer_nothing_changes():
    r = georeference(utm32(*MINSTER), None, "DE", None, None, None, place_name="Freiburg im Breisgau")
    assert r.location.lat is None and "not georeferenced" in r.issues


def test_gazetteer_place_resolves_coordinates_without_an_estimate(gaz):
    r = georeference(utm32(*MINSTER), None, "DE", None, None, None, place_name="Freiburg im Breisgau")
    assert r.location.crs_epsg in (25832, 32632)
    assert r.location.lat == pytest.approx(MINSTER[0], abs=1e-4)


def test_far_estimate_loses_to_the_place_the_coordinates_fit(gaz):
    berlin = (52.52, 13.40, 5.0)
    r = georeference(utm32(*MINSTER), None, "DE", *berlin, place_name="Freiburg im Breisgau")
    assert r.location.lat == pytest.approx(MINSTER[0], abs=1e-4)
    assert r.issues[0].startswith("place estimate 6") and "where the printed coordinates fit" in r.issues[0]
    assert r.location.confidence <= 0.5


def test_far_estimate_stays_for_a_position_from_the_place_alone(gaz):
    r = georeference(None, None, "DE", 52.52, 13.40, 5.0, place_name="Freiburg im Breisgau")
    assert r.location.lat == pytest.approx(52.52)  # the estimate is not replaced: the record would move
    assert r.issues[0].endswith("estimate kept")


def test_namesakes_need_the_estimate_to_pick_one(gaz):
    alone = georeference(None, None, "US", None, None, None, place_name="Springfield")
    assert alone.issues[0].startswith("place name fits several places") and alone.location.lat is None
    picked = georeference(None, None, "US", 42.1, -72.6, 10.0, place_name="Springfield")
    assert not any(i.startswith("place name") for i in picked.issues)


def test_place_findings_are_warnings_whatever_the_place_is_called():
    from pipeline.scoring import finding_severity
    assert finding_severity("place name fits several places: Gap (FR), Gap (US)") == "warning"


def test_a_namesake_abroad_is_a_finding_only_where_the_estimate_lies(gaz):
    at_namesake = georeference(None, None, "FR", 48.47, 7.94, 5.0, place_name="Offenburg")
    assert at_namesake.issues[0] == "place name not in FR, only in: Offenburg (DE)"
    in_country = georeference(None, None, "FR", 48.857, 2.352, 5.0, place_name="Offenburg")  # Paris
    assert not any(i.startswith("place name") for i in in_country.issues)
