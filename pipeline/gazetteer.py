"""Offline place lookup in GeoNames (cities with 1,000+ inhabitants, CC BY 4.0, geonames.org).

The place a document names is looked up here instead of trusting the extraction model's own estimate, which
can be a guess for small or ambiguous places. Without the index (not downloaded) every lookup returns
nothing and georeferencing uses the model's estimate as before.

    python -m pipeline.gazetteer download    # fetch cities1000.zip and the region names, build data/gazetteer.sqlite
"""

from __future__ import annotations

import io
import re
import sqlite3
import sys
import unicodedata
import urllib.request
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import config

SOURCE_URL = "https://download.geonames.org/export/dump/cities1000.zip"
REGIONS_URL = "https://download.geonames.org/export/dump/admin1CodesASCII.txt"  # states, provinces
# Hits of one name closer than this are one place (a town listed with its districts).
SAME_PLACE_KM = 25.0


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    return re.sub(r"[^\w]+", " ", text).strip()


@dataclass
class Place:
    name: str
    country: str
    admin1: str
    lat: float
    lon: float
    population: int

    @property
    def precision_km(self) -> float:
        """Roughly the radius of the settlement: where in it a site is, is unknown."""
        p = self.population
        return 15.0 if p >= 1_000_000 else 8.0 if p >= 100_000 else 4.0 if p >= 10_000 else 2.0


@dataclass
class Lookup:
    place: Place | None = None  # the one place the text names
    ambiguous: list[Place] | None = None  # the text fits places this far apart; nothing is picked
    elsewhere: list[Place] | None = None  # the name exists, but not in the stated country


def build(zip_bytes: bytes, regions: str, path: Path) -> int:
    """Index the GeoNames cities file and the first-level region names; returns the number of places."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript("""
        CREATE TABLE places (id INTEGER PRIMARY KEY, name TEXT, country TEXT, admin1 TEXT, lat REAL, lon REAL,
                             population INTEGER);
        CREATE TABLE names (name TEXT, id INTEGER, PRIMARY KEY (name, id)) WITHOUT ROWID;
        CREATE TABLE regions (name TEXT, country TEXT, PRIMARY KEY (name, country)) WITHOUT ROWID;
    """)
    for line in regions.splitlines():  # 'MY.12<TAB>Selangor<TAB>Selangor<TAB>1733037'
        f = line.split("\t")
        if len(f) >= 3:
            con.executemany("INSERT OR IGNORE INTO regions VALUES (?, ?)",
                            [(normalize(n), f[0].split(".")[0]) for n in {f[1], f[2]}])
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        member = next(n for n in z.namelist() if n.endswith(".txt"))
        n = 0
        for line in io.TextIOWrapper(z.open(member), encoding="utf-8"):
            f = line.rstrip("\n").split("\t")
            gid, name, ascii_name, alternates = int(f[0]), f[1], f[2], f[3]
            con.execute("INSERT INTO places VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (gid, name, f[8], f[10], float(f[4]), float(f[5]), int(f[14] or 0)))
            keys = {normalize(v) for v in (name, ascii_name, *alternates.split(","))}
            con.executemany("INSERT OR IGNORE INTO names VALUES (?, ?)",
                            [(k, gid) for k in keys if len(k) >= 3 and not k.isdigit()])
            n += 1
    con.commit()
    con.close()
    tmp.replace(path)
    return n


@lru_cache(maxsize=1)
def _connection(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        print(f"  ! no gazetteer at {path}: place checks use the model's place estimate "
              "(install it with `python -m pipeline.gazetteer download`)", file=sys.stderr)
        return None
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)


def _places(con: sqlite3.Connection, key: str) -> list[Place]:
    rows = con.execute("SELECT p.name, p.country, p.admin1, p.lat, p.lon, p.population FROM names n "
                       "JOIN places p ON p.id = n.id WHERE n.name = ? ORDER BY p.population DESC", (key,))
    return [Place(*r) for r in rows]


def _is_region(con: sqlite3.Connection, key: str, country: str | None) -> bool:
    sql, params = "SELECT 1 FROM regions WHERE name = ?", [key]
    if country:
        sql, params = sql + " AND country = ?", [key, country.upper()]
    return con.execute(sql, params).fetchone() is not None


def _distinct(places: list[Place]) -> list[Place]:
    """One place per cluster of hits (most populous first), so a town with listed districts counts once."""
    from .georef import haversine_km  # georef imports this module
    out: list[Place] = []
    for p in places:
        if all(haversine_km(p.lat, p.lon, q.lat, q.lon) > SAME_PLACE_KM for q in out):
            out.append(p)
    return out


def lookup(text: str | None, country: str | None, path: Path | None = None) -> Lookup:
    """The place a location text names: its comma- or dash-separated parts are tried from the last
    (usually the town) to the first; the first part with any hit decides."""
    con = _connection(path or config.GAZETTEER_PATH)
    if con is None or not text:
        return Lookup()
    # Not split at '/': 'Street A / City B' names a crossing, often of streets named after towns.
    parts = [normalize(p) for p in re.split(r"[,;()|]| - ", text)]
    for key in reversed([p for p in parts if len(p) >= 3 and not p.isdigit()]):
        if _is_region(con, key, country):  # 'Hulu Langat, Selangor': the state, not the town named after it
            continue
        hits = _distinct(_places(con, key))
        if not hits:
            continue
        local = [h for h in hits if h.country == country.upper()] if country else hits
        if not local:
            return Lookup(elsewhere=hits[:5])
        if len(local) == 1:
            return Lookup(place=local[0])
        return Lookup(ambiguous=local[:5])
    return Lookup()


def main(argv: list[str]) -> None:
    if argv[:1] != ["download"]:
        sys.exit(__doc__)
    print(f"Download {SOURCE_URL}")
    with urllib.request.urlopen(SOURCE_URL, timeout=300) as resp:
        data = resp.read()
    with urllib.request.urlopen(REGIONS_URL, timeout=300) as resp:
        regions = resp.read().decode("utf-8")
    n = build(data, regions, config.GAZETTEER_PATH)
    print(f"{n} places in {config.GAZETTEER_PATH} (GeoNames, CC BY 4.0)")


if __name__ == "__main__":
    main(sys.argv[1:])
