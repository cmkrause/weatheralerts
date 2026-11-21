"""Private matching, map delivery, and disk-cache regression cases."""

import asyncio
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType, SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from shapely.geometry import Point, Polygon, box, mapping, shape

from custom_components.weatheralerts import area_api, config_flow
from custom_components.weatheralerts.geometry import (
    Area,
    GeometryService,
    _polygon,
    candidates,
)
from custom_components.weatheralerts.sensor import (
    WeatherAlertsCoordinator,
    WeatherAlertsSensor,
)

ZONE = "https://api.weather.gov/zones/"
ENTITY = "sensor.weatheralerts_demo"
FIXTURES = Path(__file__).parent / "fixtures"


class PublicNws:
    def __init__(self, geometries):
        self.geometries = geometries
        self.alerts = []
        self.calls = []
        self.status = 200

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = MagicMock()
        result.__aenter__.return_value = result
        result.status = self.status
        result.headers = {}
        if "/alerts/" in url:
            result.json = AsyncMock(return_value={"features": self.alerts})
            return result
        key = url.removeprefix(ZONE)
        geo = self.geometries.get(key)
        if geo is None and self.status == 200:
            result.status = 404
        body = json.dumps(
            {"properties": {"id": key.split("/")[-1]}, "geometry": geo}
        ).encode()

        async def chunks(_size):
            yield body

        result.content.iter_chunked = chunks
        return result


class GeometryTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = self.enterContext(TemporaryDirectory())
        self.hass = HomeAssistant(self.tmp)
        self.target = Point(-119.29, 36.33)  # Public Visalia city center.
        self.home(self.target)
        self.near = mapping(box(-120, 36, -119, 37))
        self.far = mapping(box(-118, 37, -117, 38))
        self.http = PublicNws(
            {
                "county/CAC107": self.near,
                "county/CAC019": self.far,
                "forecast/CAZ312": self.near,
            }
        )
        self.service = GeometryService(self.hass, self.http)
        self.entry = ConfigEntry(
            domain="weatheralerts",
            data={"zone": "CAZ312", "county": "CAC107"},
            options={"coverage": "private"},
            version=2,
            minor_version=1,
            source="user",
            title="Public",
            unique_id="weatheralerts_caz312_cac107",
            discovery_keys=MappingProxyType({}),
            subentries_data=[],
            state=ConfigEntryState.SETUP_IN_PROGRESS,
        )
        self.co = WeatherAlertsCoordinator(
            self.hass, self.http, "CAZ312,CAC107", self.entry, timedelta(seconds=90)
        )
        self.entry.runtime_data = self.co
        self.hass.config_entries = Mock()
        self.hass.config_entries.async_get_entry.return_value = self.entry

    def home(self, point):
        self.hass.states.async_set(
            "zone.home", "0", {"longitude": point.x, "latitude": point.y}
        )

    async def test_index_and_dynamic_feeds(self):
        self.assertIn("CAC107", candidates(self.target.x, self.target.y))
        self.assertIn("AMZ712", candidates(-67.2836, 18.5098))
        self.assertEqual(await self.service.discover(self.target, 10), "CAC107")
        with self.assertRaises(UpdateFailed):
            await self.service.discover(Point(10, 50), 10)
        self.http.geometries["marine/AMZ712"] = mapping(box(-68, 18, -67, 19))
        self.home(Point(-67.2836, 18.5098))
        data = await self.co._async_update_data()
        self.assertIn("AMZ712", data["monitored_codes"])
        self.assertNotIn("CAC107", data["monitored_codes"])

    async def test_county_boundary_polls_both(self):
        self.http.geometries["county/CAC019"] = mapping(box(-119.29, 36, -119, 37))
        self.http.geometries["county/CAC107"] = mapping(box(-120, 36, -119.29, 37))
        with patch(
            "custom_components.weatheralerts.geometry.candidates",
            return_value=["CAC019", "CAC107"],
        ):
            self.assertEqual(
                await self.service.discover(self.target, 10), "CAC019,CAC107"
            )

    async def test_visalia_replay_and_on_demand_map(self):
        cold, fog = (
            json.loads((FIXTURES / f"{name}.json").read_bytes())
            for name in ("cold", "fog")
        )
        for f in (cold, fog):
            for url in f["properties"]["affectedZones"]:
                self.http.geometries.setdefault(url.removeprefix(ZONE), self.far)
        self.http.alerts = [cold, fog]
        with patch(
            "custom_components.weatheralerts.sensor.dt_util.now",
            return_value=datetime.fromisoformat(fog["properties"]["sent"]),
        ):
            await self.co.async_refresh()
        self.assertTrue(self.co.last_update_success)
        entity = WeatherAlertsSensor(self.co, "weatheralerts_demo", "CAZ312,CAC107")
        data = entity.extra_state_attributes
        self.assertEqual(entity.state, 1)
        self.assertEqual(data["alerts"][0]["event"], "Dense Fog Advisory")
        self.assertEqual(data["alert_stats"]["active_advisory"], 1)
        self.assertEqual(data["monitored_codes"], "CAC107")
        self.assertEqual(data["unresolved_alerts"], 0)
        self.assertNotIn("coordinates", json.dumps(data))
        self.assertNotIn("geometry", json.dumps(self.co.data))
        self.assertNotIn(
            fog["properties"]["affectedZones"][0], [u for u, _ in self.http.calls]
        )
        ident = fog["properties"]["id"]
        result = await self.co.map_area(ident)
        self.assertEqual((result["resolved"], result["total"]), (12, 12))
        before = len(self.http.calls)
        self.assertEqual(await self.co.map_area(ident), result)
        self.assertEqual(before, len(self.http.calls))
        for url, kwargs in self.http.calls:
            self.assertNotIn(str(self.target.x), url)
            self.assertNotIn("/points", url)
            self.assertIsNone(kwargs.get("params"))
        self.assertEqual(entity.extra_state_attributes, data)

    async def test_native_multipolygon_holes_boundary(self):
        shell = [(0, 0), (5, 0), (5, 5), (0, 5), (0, 0)]
        hole = [(1, 1), (1, 3), (3, 3), (3, 1), (1, 1)]
        native = {
            "type": "MultiPolygon",
            "coordinates": [[shell, hole], [list(box(8, 8, 9, 9).exterior.coords)]],
        }
        area = Area(native, (ZONE + "county/CAC107",))
        for point, expected in [
            (Point(2, 2), False),
            (Point(1, 2), True),
            (Point(0, 2), True),
            (Point(8.5, 8.5), True),
            (Point(6, 2), False),
        ]:
            with self.subTest(point=point):
                self.assertIs(await self.service.applies(area, point, 10), expected)
        self.assertEqual(self.http.calls, [])

    async def test_bad_data_fails_open(self):
        for area in (
            Area(None, ()),
            Area(None, ("https://example.com/zones/forecast/CAZ312",)),
            Area({"type": "LineString", "coordinates": [[1, 1], [2, 2]]}, ()),
            Area(None, (ZONE + "fire/CAZ999",)),
            Area(None, ("garbage", ZONE + "forecast/CAZ999")),
        ):
            self.assertIsNone(await self.service.applies(area, self.target, 10))
        self.assertTrue(all(url.startswith(ZONE) for url, _ in self.http.calls))
        invalid = {"type": "Polygon", "coordinates": [[(0, 0), (1, 1), (1, 0)]]}
        self.assertIsNone(_polygon(invalid))
        crossed = {
            "type": "Polygon",
            "coordinates": [[(179, 0), (-179, 0), (-179, 1), (179, 0)]],
        }
        self.assertIsNone(_polygon(crossed))

    async def test_unresolved_alert_retained_and_move_does_not_reuse_old_feed(self):
        fog = json.loads((FIXTURES / "fog.json").read_bytes())
        fog["properties"]["affectedZones"] = [ZONE + "forecast/CAZ999"]
        self.http.alerts = [fog]
        data = await self.co._async_update_data()
        self.assertEqual(data["unresolved_alerts"], 1)
        self.assertEqual(data["alerts"][0]["id"], fog["properties"]["id"])
        self.home(Point(10, 50))
        calls = len(self.http.calls)
        with self.assertRaises(UpdateFailed):
            await self.co._async_update_data()
        self.assertEqual(calls, len(self.http.calls))

    async def test_reference_type_is_preserved(self):
        self.http.geometries["fire/CAZ312"] = self.far
        refs = (ZONE + "fire/CAZ312", ZONE + "forecast/CAZ312")
        self.assertTrue(await self.service.applies(Area(None, refs), self.target, 10))
        self.assertEqual({url for url, _ in self.http.calls}, set(refs))

    async def test_dissolve_partial_and_disjoint(self):
        refs = tuple(ZONE + f"forecast/CAZ31{i}" for i in range(4))
        hole = list(box(0.2, 0.2, 0.7, 0.7).exterior.coords)
        self.http.geometries.update(
            {
                "forecast/CAZ310": mapping(Polygon(box(0, 0, 1, 1).exterior, [hole])),
                "forecast/CAZ311": mapping(box(1, 0, 2, 1)),
                "forecast/CAZ312": mapping(box(5, 5, 6, 6)),
            }
        )
        result = await self.service.footprint(Area(None, refs), 10)
        dissolved = shape(result["geometry"])
        self.assertEqual((result["resolved"], result["total"]), (3, 4))
        self.assertEqual(dissolved.geom_type, "MultiPolygon")
        self.assertEqual(len(dissolved.geoms), 2)
        self.assertFalse(dissolved.boundary.intersects(Point(1, 0.5)))
        self.assertFalse(dissolved.covers(Point(0.5, 0.5)))

    async def test_cache_restart_concurrency_and_expiry(self):
        ref = ZONE + "county/CAC107"
        await asyncio.gather(*(self.service.zone(ref, 10) for _ in range(3)))
        self.assertEqual(len(self.http.calls), 1)
        second = GeometryService(self.hass, self.http)
        self.assertTrue((await second.zone(ref, 10)).covers(self.target))
        self.assertEqual(len(self.http.calls), 1)
        cache = next(second.directory.glob("*.wkb"))
        os.utime(cache, (0, 0))
        self.http.status = 503
        self.assertIsNone(await second.zone(ref, 10))
        self.assertIsNone(await second.zone(ref, 10))
        self.assertEqual(len(self.http.calls), 2)

    async def test_private_setup_never_looks_up_home(self):
        flow = config_flow.WeatheralertsConfigFlow()
        flow.hass = self.hass
        with (
            patch.object(
                config_flow,
                "async_get_clientsession",
                side_effect=AssertionError("network"),
            ),
            patch.object(flow, "async_set_unique_id", AsyncMock()),
            patch.object(flow, "_abort_if_unique_id_configured"),
        ):
            form = await flow.async_step_user({"coverage": "private"})
            result = await flow.async_step_update_options(form["data_schema"]({}))
        self.assertEqual(result["data"]["coverage"], "private")
        self.assertNotIn("latitude", result["data"])
        self.assertNotIn("longitude", result["data"])

    async def test_ws_permissions_current_alert_and_state_isolation(self):
        marine = json.loads((FIXTURES / "marine.json").read_bytes())
        marine["geometry"] = self.near
        self.http.alerts = [marine]
        await self.co.async_refresh()
        entity = WeatherAlertsSensor(self.co, "weatheralerts_demo", "CAZ312,CAC107")
        self.hass.states.async_set(ENTITY, entity.state, entity.extra_state_attributes)
        before = self.hass.states.get(ENTITY)
        connection = Mock()
        message = {"id": 1, "entity_id": ENTITY, "alert_id": marine["properties"]["id"]}
        with patch.object(area_api.er, "async_get") as registry:
            registry.return_value.async_get.return_value = SimpleNamespace(
                platform="weatheralerts", config_entry_id=self.entry.entry_id
            )
            handler = area_api.alert_area.__wrapped__
            connection.user.permissions.check_entity.return_value = False
            await handler(self.hass, connection, message)
            connection.send_error.assert_called_once()
            connection.send_result.assert_not_called()
            connection.user.permissions.check_entity.return_value = True
            await handler(self.hass, connection, message)
            self.assertEqual(connection.send_result.call_args.args[1]["resolved"], 1)
            await handler(self.hass, connection, {**message, "alert_id": "wrong"})
            self.assertEqual(connection.send_error.call_args.args[1], "not_found")
        self.assertIs(before, self.hass.states.get(ENTITY))

    async def test_map_requests_do_not_write_geometry_or_rows_to_recorder(self):
        from homeassistant import loader
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.tasks import SynchronizeTask
        from homeassistant.config_entries import ConfigEntries
        from homeassistant.helpers import (
            device_registry,
            entity_registry,
            frame,
            issue_registry,
        )
        from homeassistant.helpers.recorder import async_initialize_recorder
        from homeassistant.setup import async_setup_component

        hass = self.hass
        hass.config_entries = ConfigEntries(hass, {})
        await hass.config_entries.async_initialize()
        frame.async_setup(hass)
        loader.async_setup(hass)
        await hass.config.async_set_time_zone("UTC")
        device_registry.async_setup(hass)
        await device_registry.async_load(hass)
        await entity_registry.async_load(hass)
        await issue_registry.async_load(hass)
        database = Path(self.tmp) / "recorder.db"
        async_initialize_recorder(hass)
        self.addAsyncCleanup(hass.async_stop, force=True)
        self.assertTrue(
            await async_setup_component(
                hass,
                "recorder",
                {
                    "recorder": {
                        "db_url": "sqlite:///" + database.as_posix(),
                        "commit_interval": 0,
                        "auto_purge": False,
                        "auto_repack": False,
                    },
                },
            )
        )
        recorder = get_instance(hass)
        recorder._async_hass_started(hass)
        await asyncio.wait_for(recorder.async_recorder_ready.wait(), 30)
        marine = json.loads((FIXTURES / "marine.json").read_bytes())
        marine["geometry"] = self.near
        self.http.alerts = [marine]
        await self.co.async_refresh()
        entity = WeatherAlertsSensor(self.co, "weatheralerts_demo", "CAZ312,CAC107")
        hass.states.async_set(ENTITY, entity.state, entity.extra_state_attributes)

        def read_rows():
            with closing(sqlite3.connect(database)) as db:
                return (
                    db.execute("SELECT COUNT(*) FROM states").fetchone()[0],
                    db.execute(
                        "SELECT shared_attrs FROM state_attributes ORDER BY attributes_id"
                    ).fetchall(),
                )

        async def committed_rows():
            await hass.async_block_till_done()
            # Always queue a commit barrier. A worker may already have taken
            # the state event off its queue while SQLite has yet to commit it.
            committed = hass.loop.create_future()
            recorder.queue_task(SynchronizeTask(committed))
            await asyncio.wait_for(committed, 30)
            return await hass.async_add_executor_job(read_rows)

        before = await committed_rows()
        self.assertIn(marine["properties"]["id"], str(before))
        self.assertNotIn("coordinates", str(before))
        for _ in range(2):
            self.assertTrue(
                (await self.co.map_area(marine["properties"]["id"]))["geometry"]
            )
        self.assertEqual(await committed_rows(), before)
