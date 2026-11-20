"""Build the shipped indexes from official zip files. See documentation/local_geometry.md."""

import argparse
import hashlib
import json
import math
from pathlib import Path

import shapefile
from shapely.geometry import shape

ROOT = Path(__file__).resolve().parents[1] / "custom_components/weatheralerts"
SOURCES = {
    "c_16ap26": "734d75df3791bdc0cbb29fd6ed75a387",
    "mz16ap26": "f191552ac0810b4bc4d11b46a87c2aeb",
    "oz16ap26": "aa39c4bca11fcb4d949078cef292fd7d",
    "z_16ap26": "004dc6501dc3d50e7b36652cb9d02bd3",
    "fz16ap26": "17862cbdb414d7b0807ac1765b2af3d0",
}


def save(path, value):
    path.write_text(
        json.dumps(value, separators=(",", ":"), ensure_ascii=True) + "\n",
        encoding="ascii",
    )


def build(directory):
    zones = []
    priority = []
    for source, checksum in SOURCES.items():
        archive = directory / (source + ".zip")
        if (
            hashlib.md5(archive.read_bytes(), usedforsecurity=False).hexdigest()
            != checksum
        ):
            raise ValueError(f"Unexpected NOAA archive: {source}")
        with shapefile.Reader(str(archive), encoding="latin1") as reader:
            for record in reader.iterShapeRecords():
                attrs = record.record.as_dict()
                if source.startswith("c_"):
                    code = attrs["STATE"] + "C" + attrs["FIPS"][-3:]
                elif "STATE_ZONE" in attrs:
                    code = attrs["STATE"] + "Z" + attrs["ZONE"]
                else:
                    code = attrs["ID"]
                # Round outwards. Multiple rectangles may represent one county.
                w, s, e, n = record.shape.bbox
                rows = priority if "STATE_ZONE" in attrs else zones
                rows.append(
                    [
                        code,
                        math.floor(w * 100) / 100,
                        math.floor(s * 100) / 100,
                        math.ceil(e * 100) / 100,
                        math.ceil(n * 100) / 100,
                    ]
                )
    save(ROOT / "zone_index.json", sorted(zones))
    save(ROOT / "zone_priority.json", sorted(priority))

    # Cartographic context only. Never use these simplified outlines for containment.
    land = []
    with shapefile.Reader(str(directory / "ne_50m_land.zip")) as reader:
        for item in reader.iterShapes():
            g = shape(item.__geo_interface__).simplify(0.04)
            for polygon in getattr(g, "geoms", [g]):
                if polygon.area < 0.008:
                    continue
                ring = [[round(x, 3), round(y, 3)] for x, y in polygon.exterior.coords]
                land.append([list(polygon.bounds), ring])
    with shapefile.Reader(
        str(directory / "ne_10m_populated_places_simple.zip")
    ) as reader:
        towns = sorted(
            (
                [
                    r["pop_max"],
                    r["name"],
                    round(r["longitude"], 3),
                    round(r["latitude"], 3),
                ]
                for r in reader.iterRecords()
                if r["iso_a2"] in {"US", "CA", "MX", "PR", "GU", "VI", "MP", "AS"}
            ),
            reverse=True,
        )
    save(ROOT / "frontend/map-context.json", {"land": land, "towns": towns})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("archive_directory", type=Path)
    build(parser.parse_args().archive_directory)
