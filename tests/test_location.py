"""WeatherAlerts polling tests. HTTP responses are replayed, never fetched."""

import json
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from aiohttp import ClientConnectionError
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.restore_state import RestoredExtraData
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from custom_components.weatheralerts import config_flow, sensor
from custom_components.weatheralerts.const import (
    ALERTS_API,
    CONF_DEDUPLICATE_ALERTS_BY_ID,
    CONF_USE_HOME,
    DOMAIN,
    HEADERS,
    POINT_ALERTS_API,
)

FEED = "CAZ312,CAC107"
# Public Visalia city center and two public points in marine zone AMZ712.
VISALIA = (36.33, -119.29)
MARINE_INSIDE = (18.5098, -67.2836)
MARINE_OUTSIDE = (18.5034, -67.3536)
FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def response(*features, status=200, payload=None, headers=None):
    result = MagicMock()
    result.__aenter__.return_value = result
    result.status = status
    result.headers = headers or {}
    result.json = AsyncMock(
        return_value=payload
        if payload is not None
        else {
            "type": "FeatureCollection",
            "features": deepcopy(list(features)),
        }
    )
    return result


class LocationTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hass = HomeAssistant(".")
        self.http = Mock()
        self.fog, self.cold, self.marine = map(fixture, ("fog", "cold", "marine"))
        self.now = datetime.fromisoformat("2026-11-20T13:55:00-08:00")
        self.enterContext(patch.object(dt_util, "now", side_effect=lambda: self.now))
        self.set_home(VISALIA)
        self.setup_coordinator({CONF_USE_HOME: True})

    def set_home(self, point):
        self.hass.states.async_set(
            "zone.home", "0", {"latitude": point[0], "longitude": point[1]}
        )

    def setup_coordinator(self, options, data=None):
        self.entry = ConfigEntry(
            domain=DOMAIN,
            data={"zone": "CAZ312", "county": "CAC107", **(data or {})},
            options=options,
            version=2,
            minor_version=1,
            source="user",
            title="Visalia",
            unique_id=FEED,
            subentries_data=[],
            discovery_keys=MappingProxyType({}),
            state=ConfigEntryState.SETUP_IN_PROGRESS,
        )
        self.hass.config_entries = Mock()
        self.hass.config_entries.async_entries.return_value = [self.entry]
        self.co = sensor.WeatherAlertsCoordinator(
            self.hass, self.http, FEED, self.entry, timedelta(seconds=90)
        )

    async def poll(self, reply):
        self.http.get.return_value = reply
        await self.co.async_refresh()
        self.assertTrue(self.co.last_update_success, str(self.co.last_exception))
        return self.co.data

    def assert_point(self, point):
        self.http.get.assert_called_with(
            POINT_ALERTS_API,
            params={"point": f"{point[0]},{point[1]}"},
            headers=HEADERS,
        )

    async def test_visalia_and_existing_entity(self):
        self.http.get.return_value = response(self.fog)
        add = Mock()
        with patch.object(sensor, "async_get_clientsession", return_value=self.http):
            await sensor.async_setup_entry(self.hass, self.entry, add)
        ((entity,),) = add.call_args.args
        entity.hass = self.hass
        self.assert_point(VISALIA)
        self.http.get.assert_called_once()
        self.assertEqual(entity.state, 1)
        self.assertEqual(entity.unique_id, "weatheralerts_caz312cac107")
        attrs = entity.extra_state_attributes
        self.assertEqual(attrs["alert_stats"]["active_advisory"], 1)
        self.assertEqual(attrs["alerts"][0]["id"], self.fog["properties"]["id"])
        self.assertEqual(
            [t["id"] for t in attrs["alert_tracking"]], [attrs["alerts"][0]["id"]]
        )
        self.assertEqual(attrs["location_mode"], "home")
        self.assertNotIn("geometry", str(attrs))
        self.assertNotIn(str(VISALIA[0]), str(attrs))

    async def test_existing_zone_entry_does_not_need_home(self):
        self.setup_coordinator({})
        self.hass.states.async_remove("zone.home")
        data = await self.poll(response(self.fog, self.cold))
        self.http.get.assert_called_once_with(
            ALERTS_API.format(FEED), params=None, headers=HEADERS
        )
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["alert_stats"]["active_advisory"], 2)
        self.assertNotIn("location_mode", data)
        self.assertEqual(
            {a["id"] for a in data["alerts"]},
            {f["properties"]["id"] for f in (self.fog, self.cold)},
        )

    async def test_polygon_outside_and_inside_at_two_points_in_same_marine_zone(self):
        self.set_home(MARINE_OUTSIDE)
        self.assertEqual((await self.poll(response()))["count"], 0)
        self.assert_point(MARINE_OUTSIDE)
        self.set_home(MARINE_INSIDE)
        data = await self.poll(response(self.marine))
        self.assert_point(MARINE_INSIDE)
        self.assertEqual(data["alert_stats"]["active_warning"], 1)
        self.assertEqual(data["alerts"][0]["id"], self.marine["properties"]["id"])
        self.set_home(MARINE_OUTSIDE)
        data = await self.poll(response())
        self.assertEqual(data["alert_tracking"], [])

    async def test_zone_kinds_and_unresolvable_metadata_are_retained(self):
        for zones in (
            ["county/CAC107", "forecast/CAZ312", "fire/CAZ580"],
            [],
            ["invalid"],
        ):
            f = deepcopy(self.fog)
            f["properties"]["affectedZones"] = [
                "https://api.weather.gov/zones/" + z for z in zones
            ]
            self.assertIsNone(f["geometry"])
            with self.subTest(zones=zones):
                data = await self.poll(response(f))
                self.assertEqual(data["count"], 1)
                self.assert_point(VISALIA)
        self.assertEqual(self.http.get.call_count, 3)

    async def test_deduplication_and_lifecycle(self):
        self.setup_coordinator(
            {CONF_USE_HOME: True, CONF_DEDUPLICATE_ALERTS_BY_ID: True}
        )
        for expected in ("new", "old"):
            data = await self.poll(response(self.fog, self.fog))
            self.assertEqual(data["count"], 1)
            self.assertEqual([t["status"] for t in data["alert_tracking"]], [expected])
        data = await self.poll(response())
        self.assertEqual(data["count"], 0)
        self.assertEqual([t["status"] for t in data["alert_tracking"]], ["delete"])
        self.now += timedelta(minutes=10)
        self.assertEqual((await self.poll(response()))["alert_tracking"], [])

    async def test_outage_and_expiry_grace(self):
        await self.poll(response(self.fog))
        data = await self.poll(response(status=503))
        self.assertEqual(data["count"], 1)
        self.assertEqual([e["type"] for e in data["error"]], ["http_error", "success"])
        self.assertEqual(data["alert_tracking"][0]["status"], "old")
        self.now = datetime.fromisoformat(
            self.fog["properties"]["expires"]
        ) + timedelta(minutes=59)
        self.assertEqual((await self.poll(response(status=503)))["count"], 1)
        self.now += timedelta(minutes=1)
        data = await self.poll(response(status=503))
        self.assertEqual(
            (data["count"], data["alerts"], data["alert_tracking"]), (0, [], [])
        )
        self.assertEqual(data["alert_stats"]["total_active_alerts"], 0)

    async def test_no_point_baseline_and_moved_home_outage(self):
        self.http.get.return_value = response(status=503)
        with self.assertRaises(ConfigEntryNotReady):
            await self.co.async_config_entry_first_refresh()
        await self.poll(response(self.fog))
        self.set_home(MARINE_INSIDE)
        self.http.get.return_value = response(status=503)
        await self.co.async_refresh()
        self.assertFalse(self.co.last_update_success)
        data = await self.poll(response(self.marine))
        self.assertEqual([t["status"] for t in data["alert_tracking"]], ["new"])

    async def test_move_during_request(self):
        reply = response(self.fog)

        def move():
            self.set_home(MARINE_INSIDE)
            return {"features": [self.fog]}

        reply.json.side_effect = move
        self.http.get.return_value = reply
        with self.assertRaises(UpdateFailed):
            await self.co._async_update_data()
        self.assertIsNone(self.co._last_good_data)

    async def test_missing_and_invalid_home(self):
        for value in ("unknown", "unavailable", "0"):
            self.hass.states.async_set(
                "zone.home", value, {"latitude": 999, "longitude": 0}
            )
            with self.assertRaises(UpdateFailed):
                await self.co._async_update_data()
        self.hass.states.async_remove("zone.home")
        with self.assertRaises(UpdateFailed):
            await self.co._async_update_data()
        self.http.get.assert_not_called()

    async def test_transport_errors_and_invalid_responses(self):
        await self.poll(response(self.fog))
        for error in (TimeoutError(), ClientConnectionError("private request URL")):
            self.http.get.side_effect = error
            data = await self.poll(None)
            self.assertEqual(data["count"], 1)
            self.assertNotIn("private request URL", str(data["error"]))
        self.http.get.side_effect = None
        for body in (
            {},
            {"features": None},
            {"features": [{}]},
            {"features": [], "pagination": {"next": "more"}},
        ):
            data = await self.poll(response(payload=body))
            self.assertEqual(data["count"], 1)
            self.assertEqual(data["error"][0]["type"], "exception")
        data = await self.poll(response())
        self.assertEqual(data["error"][0]["type"], "success")
        self.assertEqual(data["count"], 0)

    async def test_rate_limit_skips_requests_until_retry_after(self):
        await self.poll(response(self.fog))
        await self.poll(response(status=429, headers={"Retry-After": "300"}))
        self.assertGreater(self.co._retry_at - self.hass.loop.time(), 299)
        data = await self.poll(response())
        self.assertEqual(data["count"], 1)
        self.assertEqual(self.http.get.call_count, 2)

    async def test_restore_reconciles_first_publication(self):
        # The previous zone feed knew both IDs. On enabling Home only fog is old.
        self.setup_coordinator({})
        previous = (await self.poll(response(self.fog, self.cold)))["alert_tracking"]
        self.setup_coordinator({CONF_USE_HOME: True})
        await self.poll(response(self.fog))
        entity = sensor.WeatherAlertsSensor(self.co, "existing", FEED)
        entity.hass = self.hass
        entity.platform = MagicMock()
        last_state = Mock(attributes={"alert_tracking": previous})
        with (
            patch.object(
                entity, "async_get_last_state", AsyncMock(return_value=last_state)
            ),
            patch.object(
                entity, "async_get_last_extra_data", AsyncMock(return_value=None)
            ),
        ):
            await entity.async_added_to_hass()
        self.assertEqual(
            [t["status"] for t in entity.extra_state_attributes["alert_tracking"]],
            ["old"],
        )
        self.assertEqual(
            self.co._last_good_data["alert_tracking"], self.co.data["alert_tracking"]
        )
        self.assertIsInstance(entity.extra_restore_state_data, RestoredExtraData)
        self.co.restore_tracking(
            previous, entity.extra_restore_state_data.as_dict()["home_point"]
        )
        self.assertEqual(
            {t["status"] for t in self.co.data["alert_tracking"]}, {"old", "delete"}
        )
        await entity.async_will_remove_from_hass()

    async def test_options_default_and_persistence(self):
        self.setup_coordinator(
            {}, {CONF_USE_HOME: True, CONF_DEDUPLICATE_ALERTS_BY_ID: True}
        )
        flow = config_flow.WeatheralertsOptionsFlow(self.entry)
        form = await flow.async_step_update_options()
        fields = form["data_schema"]({})
        self.assertTrue(fields[CONF_USE_HOME])
        self.assertTrue(fields[CONF_DEDUPLICATE_ALERTS_BY_ID])
        fields[CONF_USE_HOME] = False
        await flow.async_step_update_options(fields)
        result = await flow.async_step_icon_config({})
        self.assertFalse(result["data"][CONF_USE_HOME])
        self.assertTrue(result["data"][CONF_DEDUPLICATE_ALERTS_BY_ID])

    async def test_setup_option_defaults_off_and_saves_home(self):
        flow = config_flow.WeatheralertsConfigFlow()
        flow.hass = self.hass
        flow._zone, flow._county, flow._marine = "CAZ312", "CAC107", ""
        form = await flow.async_step_update_options()
        fields = form["data_schema"]({})
        self.assertFalse(fields[CONF_USE_HOME])
        fields[CONF_USE_HOME] = True
        with (
            patch.object(
                config_flow, "async_get_clientsession", return_value=self.http
            ),
            patch.object(
                config_flow, "_get_zone_name", AsyncMock(return_value="Visalia")
            ),
            patch.object(flow, "async_set_unique_id", AsyncMock()),
            patch.object(flow, "_abort_if_unique_id_configured"),
        ):
            result = await flow.async_step_update_options(fields)
        self.assertTrue(result["data"][CONF_USE_HOME])

    async def test_outage_preserves_unreadable_expiry(self):
        alert = deepcopy(self.marine)
        alert["properties"]["expires"] = "unknown"
        await self.poll(response(alert))
        data = await self.poll(response(status=502))
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["alerts"][0]["id"], alert["properties"]["id"])

    async def test_outage_preserves_existing_deletion_window(self):
        await self.poll(response(self.fog))
        gone = await self.poll(response())
        self.now += timedelta(minutes=9)
        cached = await self.poll(response(status=503))
        self.assertEqual(cached["alert_tracking"], gone["alert_tracking"])
        self.now += timedelta(minutes=1)
        self.assertEqual((await self.poll(response(status=503)))["alert_tracking"], [])
