"""NWS geometry shared by private matching and the on-demand map."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from weakref import WeakValueDictionary

from aiohttp import ClientError
from aiohttp.helpers import parse_http_date
from homeassistant.helpers.update_coordinator import UpdateFailed
from shapely import from_wkb, get_num_coordinates, to_wkb, union_all
from shapely.errors import ShapelyError
from shapely.geometry import mapping, shape

from .const import HEADERS

ZONE = re.compile(
    r"https://api\.weather\.gov/zones/(county|forecast|public|fire|marine|coastal|offshore|land)/([A-Z]{2}[CZ][0-9]{3})"
)
DAY = 86400
MAX_CACHE = 16 * 1024 * 1024
MAX_RESPONSE = 8 * 1024 * 1024


@dataclass
class Area:
    native: dict | None
    zones: tuple

    @classmethod
    def from_feature(cls, feature):
        refs = feature["properties"].get("affectedZones")
        # Preserve an unknown component: a partially malformed list cannot prove exclusion.
        refs = refs if isinstance(refs, list) else []
        return cls(
            feature.get("geometry"),
            tuple(dict.fromkeys(r if isinstance(r, str) else "" for r in refs)),
        )


def _polygon(raw):
    if not isinstance(raw, dict) or raw.get("type") not in ("Polygon", "MultiPolygon"):
        return None
    try:
        polygons = (
            [raw["coordinates"]] if raw["type"] == "Polygon" else raw["coordinates"]
        )
        if any(
            not rings or any(len(ring) < 4 or ring[0] != ring[-1] for ring in rings)
            for rings in polygons
        ):
            return None
        g = shape(raw)
        if g.is_empty or not g.is_valid:
            return None
        for part in getattr(g, "geoms", (g,)):
            w, s, e, n = part.bounds
            if not (-180 <= w <= e <= 180 and -90 <= s <= n <= 90 and e - w <= 180):
                return None  # Unsplit antimeridian or invalid coordinate range.
        return g
    except (ShapelyError, TypeError, ValueError, KeyError, IndexError, OverflowError):
        return None


@lru_cache(maxsize=2)
def _index(name):
    return json.loads(Path(__file__).with_name(name).read_bytes())


@lru_cache(maxsize=16)
def candidates(lon, lat, index="zone_index.json"):
    return sorted(
        {code for code, w, s, e, n in _index(index) if w <= lon <= e and s <= lat <= n}
    )


def _read(path):
    try:
        stat = path.stat()
        if time.time() - stat.st_mtime < DAY and stat.st_size <= MAX_CACHE:
            polygon = from_wkb(path.read_bytes())
            if (
                polygon.geom_type in ("Polygon", "MultiPolygon")
                and not polygon.is_empty
                and polygon.is_valid
            ):
                return polygon
    except (OSError, ShapelyError, ValueError):
        pass
    return None


def _save(path, polygon):
    data = to_wkb(polygon)
    if len(data) > MAX_CACHE:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        tmp = Path(temp.name)
        try:
            temp.write(data)
        except OSError:
            temp.close()
            tmp.unlink(missing_ok=True)
            raise
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    files = sorted(
        (f.stat().st_mtime, f.stat().st_size, f) for f in path.parent.glob("*.wkb")
    )
    size = sum(row[1] for row in files)
    for _, length, file in files:
        if size <= MAX_CACHE:
            break
        file.unlink(missing_ok=True)
        size -= length


def _decode(body, code):
    feature = json.loads(body)
    if (
        not isinstance(feature, dict)
        or (feature.get("properties") or {}).get("id") != code
    ):
        return None
    return _polygon(feature.get("geometry"))


def _union(polygons):
    if not polygons:
        return None
    try:
        return mapping(union_all(polygons))
    except ShapelyError:
        return None


class GeometryService:
    """All exact Home coordinates stay in process. Only validated NWS zone URLs leave it."""

    def __init__(self, hass, session):
        self.hass = hass
        self.session = session
        self.directory = Path(hass.config.path(".storage", "weatheralerts_boundaries"))
        self.locks = WeakValueDictionary()
        self.disk_lock = asyncio.Lock()
        self.network = asyncio.Semaphore(2)
        self.retry_at = 0.0
        self.failures = {}

    async def zone(self, url, timeout):
        match = ZONE.fullmatch(url)
        if not match:
            return None
        kind, code = match.groups()
        path = self.directory / f"{kind}-{code}.wkb"
        lock = self.locks.setdefault(url, asyncio.Lock())
        async with lock:
            job = self.hass.async_add_executor_job
            cached = await job(_read, path)
            if cached is not None:
                return cached
            async with self.network:
                now = time.monotonic()
                if max(self.retry_at, self.failures.get(url, 0)) > now:
                    return None
                try:
                    async with (
                        asyncio.timeout(timeout),
                        self.session.get(
                            url, headers=HEADERS, allow_redirects=False
                        ) as response,
                    ):
                        if response.status == 429:
                            self.retry_at = now + 60
                            value = response.headers.get("Retry-After", "")
                            date = parse_http_date(value)
                            delay = (
                                date.timestamp() - time.time()
                                if date
                                else float(value or 60)
                            )
                            self.retry_at = now + max(
                                60, delay if math.isfinite(delay) else 60
                            )
                        if response.status != 200:
                            raise ValueError("Zone unavailable")
                        body = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            body.extend(chunk)
                            if len(body) > MAX_RESPONSE:
                                raise ValueError("Oversize zone")
                    polygon = await job(_decode, body, code)
                    if polygon is None:
                        raise ValueError("Invalid zone geometry")
                except (
                    ClientError,
                    OSError,
                    ValueError,
                    TypeError,
                    AttributeError,
                    TimeoutError,
                ):
                    self.failures = {k: v for k, v in self.failures.items() if v > now}
                    self.failures[url] = now + 60
                    return None
            try:
                async with self.disk_lock:
                    await job(_save, path, polygon)
            except OSError:
                pass  # Read-only or full disk: the live polygon remains usable.
            return polygon

    async def discover(self, point, timeout):
        codes = await self.hass.async_add_executor_job(candidates, point.x, point.y)
        matched, unknown = [], []
        for code in codes:
            kind = "county" if code[2] == "C" else "marine"
            polygon = await self.zone(
                f"https://api.weather.gov/zones/{kind}/{code}", timeout
            )
            if polygon is None:
                unknown.append(code)
            elif await self.hass.async_add_executor_job(polygon.covers, point):
                matched.append(code)
        if not matched:
            raise UpdateFailed("Unable to establish NWS coverage for Home")
        # An unresolved neighbouring candidate may also cover the point.
        return ",".join(sorted(matched + unknown))

    async def applies(self, area, point, timeout):
        """True inside, False outside, None when at least one component is uncertain."""
        if area.native is not None:
            polygon = await self.hass.async_add_executor_job(_polygon, area.native)
            return (
                None
                if polygon is None
                else await self.hass.async_add_executor_job(polygon.covers, point)
            )
        uncertain = not area.zones
        likely = await self.hass.async_add_executor_job(
            candidates, point.x, point.y, "zone_priority.json"
        )
        # Approximate rectangles only order work. They never exclude an alert.
        for url in sorted(
            area.zones, key=lambda ref: ref.rsplit("/", 1)[-1] not in likely
        ):
            polygon = await self.zone(url, timeout)
            if polygon is None:
                uncertain = True
            elif await self.hass.async_add_executor_job(polygon.covers, point):
                return True
        return None if uncertain else False

    async def footprint(self, area, timeout):
        if area.native is not None:
            g = await self.hass.async_add_executor_job(_polygon, area.native)
            return {
                "geometry": area.native if g is not None else None,
                "resolved": int(g is not None),
                "total": 1,
            }
        parts = []
        vertices = 0
        deadline = self.hass.loop.time() + 90
        for url in area.zones:
            if self.hass.loop.time() >= deadline:
                break
            try:
                async with asyncio.timeout_at(deadline):
                    g = await self.zone(url, timeout)
            except TimeoutError:
                break
            if g is not None:
                vertices += int(get_num_coordinates(g))
                if vertices > 250_000:
                    break
                parts.append(g)
        geometry = await self.hass.async_add_executor_job(_union, parts)
        return {"geometry": geometry, "resolved": len(parts), "total": len(area.zones)}


def geometry_service(hass, session):
    key = "weatheralerts_geometry_service"
    if key not in hass.data:
        hass.data[key] = GeometryService(hass, session)
    return hass.data[key]
