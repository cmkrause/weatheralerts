"""
An integration which allows you to get weather alerts from weather.gov.
For more details about this integration, please refer to the documentation at
https://github.com/custom-components/weatheralerts
"""
import asyncio
import logging
import re
from datetime import timedelta
from decimal import Decimal

from aiohttp import ClientError
from aiohttp.helpers import parse_http_date
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.restore_state import RestoredExtraData, RestoreEntity
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util
from shapely.geometry import Point

from .const import (
    ALERTS_API,
    CONF_API_TIMEOUT,
    CONF_COUNTY,
    CONF_DEDUPLICATE_ALERTS,
    CONF_DEDUPLICATE_ALERTS_BY_ID,
    CONF_DEFAULT_ICON,
    CONF_ENTITY_NAME,
    CONF_EVENT_ICONS,
    CONF_MARINE_ZONES,
    CONF_NAME,
    CONF_UPDATE_INTERVAL,
    CONF_ZONE,
    CONF_ZONE_NAME,
    DEFAULT_API_TIMEOUT,
    DEFAULT_DEDUPLICATE_ALERTS,
    DEFAULT_DEDUPLICATE_ALERTS_BY_ID,
    DEFAULT_EVENT_ICON,
    DEFAULT_EVENT_ICONS,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    HEADERS,
    MODE_HOME,
    MODE_PRIVATE,
    MODE_ZONES,
    POINT_ALERTS_API,
    coverage,
)
from .geometry import Area, geometry_service

_LOGGER = logging.getLogger(__name__)

def _clean_for_entity_id(text):
    return re.sub(r'[^a-z0-9_]', '', re.sub(r'[-\s,]+', '_', text.lower())).strip('_')

def _get_icon_for_event(event, entry_options=None):
    event_icons = None
    default_icon = None
    if entry_options:
        event_icons = entry_options.get(CONF_EVENT_ICONS)
        default_icon = entry_options.get(CONF_DEFAULT_ICON)
        if isinstance(event_icons, str):
            import json
            try:
                event_icons = json.loads(event_icons)
            except ValueError:
                event_icons = None
    if not event_icons or not isinstance(event_icons, dict):
        event_icons = DEFAULT_EVENT_ICONS
    if not default_icon or not isinstance(default_icon, str):
        default_icon = DEFAULT_EVENT_ICON
    lower_map = {k.lower(): v for k, v in event_icons.items()}
    icon = lower_map.get(str(event or "").strip().lower())
    return icon if icon else default_icon

def _compute_active_alert_stats(alerts):
    now_ts = dt_util.as_timestamp(dt_util.now())
    def is_active(alert):
        ends_str = alert.get("expires")
        if not ends_str or ends_str == "null":
            return False
        try:
            ends_ts = dt_util.as_timestamp(ends_str)
            if not ends_ts:
                return False
            return (ends_ts - now_ts) > 0
        except (ValueError, TypeError, OverflowError):
            _LOGGER.debug(f"weatheralerts: Could not parse 'expires' for active_alert_stats: {ends_str}")
            return False

    categories = {
        "active_warning": "warning",
        "active_watch": "watch",
        "active_advisory": "advisory",
        "active_statement": "statement",
        "active_outlook": "outlook",
        "active_alert": "alert",
        "active_message": "message",
        "active_important": "important",
        "active_test": "test",
        "active_outage": "outage",
        "active_emergency": "emergency",
        "active_immediate": "immediate",
        "active_forecast": "forecast",
    }

    stats = {k: 0 for k in categories}
    stats["total_active_alerts"] = 0

    for alert in alerts:
        if not alert.get("event"):
            continue
        event_text = alert["event"].lower()
        if is_active(alert):
            stats["total_active_alerts"] += 1
            for k, substr in categories.items():
                if substr in event_text:
                    stats[k] += 1
    return stats

def _deduplicate_alerts_by_id(alerts):
    """Deduplicate alerts by NWS alert ID while preserving alerts without a usable ID."""
    seen_ids = set()
    deduped = []
    for alert in alerts:
        alert_id = alert.get("id")
        if not alert_id or alert_id == "null":
            deduped.append(alert)
            continue
        if alert_id in seen_ids:
            continue
        seen_ids.add(alert_id)
        deduped.append(alert)
    return deduped

def _dedup_key(text):
    return re.sub(r'\s+', '', text).lower()  # Remove all whitespace, make lowercase

def _deduplicate_alerts_by_description(alerts):
    """Deduplicate alerts by description, keeping only the newest sent, then latest expires, then top reverse-sorted id."""
    from collections import defaultdict
    grouped = defaultdict(list)
    for alert in alerts:
        key = _dedup_key(alert.get("description", ""))
        grouped[key].append(alert)
    deduped = []
    for group in grouped.values():
        if len(group) == 1:
            deduped.append(group[0])
        else:
            # Sort by sent DESC (newest first), then expires DESC, then id DESC
            group.sort(key=lambda a: (
                a.get("sent", ""),
                a.get("expires", ""),
                a.get("id", ""),
            ), reverse=True)
            # Find the best "sent"
            best_sent = group[0].get("sent", "")
            sent_group = [a for a in group if a.get("sent", "") == best_sent]
            if len(sent_group) == 1:
                deduped.append(sent_group[0])
            else:
                # Further filter by expires DESC
                best_expires = max(a.get("expires", "") for a in sent_group)
                expires_group = [a for a in sent_group if a.get("expires", "") == best_expires]
                if len(expires_group) == 1:
                    deduped.append(expires_group[0])
                else:
                    # Reverse sort by id and keep the first one
                    expires_group.sort(key=lambda a: a.get("id", ""), reverse=True)
                    deduped.append(expires_group[0])
    return deduped

class WeatherAlertsCoordinator(DataUpdateCoordinator):
    """Coordinator for fetching weather alerts and tracking alert_tracking."""

    def __init__(self, hass, session, feedid, entry, update_interval):
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"WeatherAlerts ({feedid})",
            update_interval=update_interval,
        )
        self.session = session
        self.feedid = feedid
        self._retry_at = 0.0
        self._last_point = None
        self.geometry = geometry_service(hass, session)
        self.areas = {}
        self._maps = {}
        self._map_lock = asyncio.Lock()
        self._feed_cache = None
        self._matches = {}
        self._last_good_data = None
        self._last_error = []
        self._alert_tracking_list = []
        _LOGGER.info(
            "weatheralerts: WeatherAlertsCoordinator initialized for feedid: %s", feedid
        )

    def _query_point(self):
        """Read the current target. None selects the configured zone feed."""
        entry = self.config_entry
        mode = coverage({**entry.data, **entry.options})
        if mode == MODE_ZONES:
            return None
        home = self.hass.states.get("zone.home")
        if home and home.state not in ("unknown", "unavailable"):
            try:
                lat = float(home.attributes["latitude"])
                lon = float(home.attributes["longitude"])
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    # NWS PointString uses decimal notation, including near zero.
                    point = f"{Decimal(str(lat)):f},{Decimal(str(lon)):f}"
                    return "private/" + point if mode == MODE_PRIVATE else point
            except (KeyError, ValueError, TypeError, OverflowError):
                pass
        raise UpdateFailed("Home location is unavailable")

    def _fallback(self, point, error_type, status, message):
        if point != self._query_point():
            raise UpdateFailed("Location changed during the NWS request")
        error = {
            "type": error_type,
            "status": status,
            "message": message,
            "timestamp": dt_util.now().isoformat(),
        }
        errors = [error] + [e for e in self._last_error if e["type"] == "success"][:1]
        self._last_error = errors
        if point != self._last_point or (
            point is not None and self._last_good_data is None
        ):
            # A failed first poll or move is unknown, never an all-clear.
            delay = self._retry_at - self.hass.loop.time()
            raise UpdateFailed(
                message, retry_after=delay if delay > 0 else None
            ) from None
        if self._last_good_data is None:
            return {
                "alerts": [],
                "count": 0,
                "alert_stats": {},
                "integration": DOMAIN,
                "zone": self.feedid,
                "alert_tracking": self._alert_tracking_list,
                "error": errors,
            }

        cutoff = dt_util.now() - timedelta(minutes=60)
        alerts = []
        for alert in self._last_good_data["alerts"]:
            expires = alert.get("expires")
            try:
                end = dt_util.parse_datetime(expires) if expires else None
                if end is not None and end <= cutoff:
                    continue
            except (TypeError, ValueError, OverflowError):
                pass  # Keep an alert when its expiry cannot be established.
            alerts.append(alert)
        ids = {alert["id"] for alert in alerts}
        tracking = [
            t for t in self._alert_tracking_list
            if t["id"] in ids or (point is not None and t["status"] == "delete")
        ]
        if point is not None:
            # A cached observation must not replay a new-alert notification.
            tracking = self._track_alerts(alerts, tracking, same_scope=True)
            self._alert_tracking_list = tracking
        return {
            **self._last_good_data,
            "alerts": alerts,
            "count": len(alerts),
            "alert_stats": _compute_active_alert_stats(alerts),
            "error": errors,
            "alert_tracking": tracking,
        }

    async def _local_feed(self, scope, target, timeout):
        now = self.hass.loop.time()
        if self._feed_cache:
            previous, expires, codes = self._feed_cache
            if previous == scope and now < expires:
                return codes
        async with asyncio.timeout(timeout):
            codes = await self.geometry.discover(target, timeout)
        self._feed_cache = (scope, now + 3600, codes)
        return codes

    async def _filter_local(self, features, scope, target, timeout):
        retained = []
        decisions = {}
        unknown = 0
        deadline = self.hass.loop.time() + timeout
        for feature in features:
            area = Area.from_feature(feature)
            key = feature["properties"].get("id")
            cached = self._matches.get(key)
            now = self.hass.loop.time()
            if cached and cached[:2] == (scope, area) and now < cached[2]:
                decision = cached[3]
                decisions[key] = cached
            else:
                decision = None
                if now < deadline:
                    try:
                        async with asyncio.timeout_at(deadline):
                            decision = await self.geometry.applies(area, target, timeout)
                    except TimeoutError:
                        pass
                if key and decision is not None:
                    decisions[key] = (scope, area, now + 3600, decision)
            if decision is not False:
                retained.append(feature)
            if decision is None:
                unknown += 1
        self._matches = decisions
        return retained, unknown

    async def map_area(self, alert_id):
        async with self._map_lock:
            area = self.areas[alert_id]
            cached = self._maps.get(alert_id)
            if cached and cached[0] == area and self.hass.loop.time() < cached[1]:
                return cached[2]
            settings = {**self.config_entry.data, **self.config_entry.options}
            timeout = settings.get(CONF_API_TIMEOUT, DEFAULT_API_TIMEOUT)
            result = await self.geometry.footprint(area, timeout)
            if self.areas.get(alert_id) != area:
                raise KeyError(alert_id)
            if result["geometry"] and result["resolved"] == result["total"]:
                if len(self._maps) >= 4:
                    self._maps.pop(next(iter(self._maps)))
                self._maps[alert_id] = (area, self.hass.loop.time()+3600, result)
            return result

    async def _async_update_data(self):
        entry = self.config_entry
        settings = {**entry.data, **entry.options}
        timeout_seconds = settings.get(CONF_API_TIMEOUT, DEFAULT_API_TIMEOUT)
        deduplicate_alerts = settings.get(
            CONF_DEDUPLICATE_ALERTS, DEFAULT_DEDUPLICATE_ALERTS
        )
        deduplicate_alerts_by_id = settings.get(
            CONF_DEDUPLICATE_ALERTS_BY_ID, DEFAULT_DEDUPLICATE_ALERTS_BY_ID
        )
        point = self._query_point()
        mode = coverage(settings)
        feed = self.feedid
        target = None
        params = {"point": point} if mode == MODE_HOME else None
        if self.hass.loop.time() < self._retry_at:
            return self._fallback(
                point, "http_error", 429, "Waiting for NWS Retry-After"
            )
        try:
            if mode == MODE_PRIVATE:
                lat, lon = map(float, point.removeprefix("private/").split(","))
                target = Point(lon, lat)
                feed = await self._local_feed(point, target, timeout_seconds)
            url = POINT_ALERTS_API if mode == MODE_HOME else ALERTS_API.format(feed)
            async with (
                asyncio.timeout(timeout_seconds),
                self.session.get(url, params=params, headers=HEADERS) as response,
            ):
                if response.status == 429:
                    after = response.headers.get("Retry-After", "")
                    until = parse_http_date(after)
                    if until:
                        delay = (until - dt_util.utcnow()).total_seconds()
                    else:
                        delay = (
                            int(after)
                            if after.isascii() and after.isdigit() and len(after) < 12
                            else 0
                        )
                    self._retry_at = self.hass.loop.time() + max(
                        delay, self.update_interval.total_seconds()
                    )
                if response.status != 200:
                    return self._fallback(
                        point,
                        "http_error",
                        response.status,
                        f"Possible API outage (status {response.status})",
                    )
                data = await response.json()
                if not isinstance(data, dict) or not isinstance(
                    data.get("features"), list
                ):
                    raise TypeError("Invalid NWS alert collection")
                pagination = data.get("pagination") or {}
                if not isinstance(pagination, dict) or pagination.get("next") or any(
                    not isinstance(f, dict) or not isinstance(f.get("properties"), dict)
                    for f in data["features"]
                ):
                    raise ValueError("Incomplete NWS alert collection")
        except (ClientError, TimeoutError, ValueError, TypeError, OSError) as err:
            # Client exceptions can include the complete request URL.
            return self._fallback(
                point, "exception", "", f"NWS request failed ({type(err).__name__})"
            )

        if point != self._query_point():
            raise UpdateFailed("Location changed during the NWS request")
        try:
            features = data["features"]
            unknown = 0
            if mode == MODE_PRIVATE:
                features, unknown = await self._filter_local(features, point, target, timeout_seconds)
                if point != self._query_point():
                    raise UpdateFailed("Location changed during local matching")
            alerts = self._parse_alerts(features, entry.options, feed)
            if deduplicate_alerts_by_id:
                alerts = _deduplicate_alerts_by_id(alerts)
            if deduplicate_alerts:
                alerts = _deduplicate_alerts_by_description(alerts)
            alerts.sort(
                key=lambda x: (x.get("sent") or "", x.get("id") or ""), reverse=True
            )
            alert_stats = _compute_active_alert_stats(alerts)
        except (TypeError, AttributeError, ValueError):
            return self._fallback(
                point, "exception", "", "Invalid NWS alert properties"
            )

        self._alert_tracking_list = self._track_alerts(
            alerts, self._alert_tracking_list, same_scope=point == self._last_point
        )
        self._last_point = point
        ids = {alert["id"] for alert in alerts}
        self.areas = {f["properties"]["id"]: Area.from_feature(f) for f in features
                      if f["properties"].get("id") in ids and f["properties"].get("id")}
        self._maps = {k:v for k,v in self._maps.items() if k in self.areas and self.areas[k] == v[0]}

        # Build success + last failure error tracking
        current_entry = {
            "type": "success",
            "status": "OK",
            "message": "no errors",
            "timestamp": dt_util.now().isoformat(),
        }
        previous_entry = next(
            (e for e in reversed(self._last_error) if e.get("type") != "success"), None
        )
        update_error = [current_entry]
        if previous_entry:
            update_error.append(previous_entry)

        result = {
            "alerts": alerts,
            "count": len(alerts),
            "alert_stats": alert_stats,
            "integration": "weatheralerts",
            "zone": feed,
            "alert_tracking": self._alert_tracking_list,
            "error": update_error,
        }
        if point is not None:
            result["location_mode"] = mode
        if mode == MODE_PRIVATE:
            result["monitored_codes"] = feed
            result["unresolved_alerts"] = unknown
        self._last_good_data = dict(result)
        self._last_error = update_error
        _LOGGER.debug("weatheralerts: Returning update data: count=%d", len(alerts))
        return result

    def _parse_alerts(self, features, entry_options, feed):
        alerts = []
        for feature in features:
            props = feature.get("properties", {})
            ends_expires = (
                props.get("expires", "null")
                if props.get("ends") is None
                else props.get("ends", "null")
            )
            event_value = props.get("event", "null")
            params = props.get("parameters") or {}
            nws_headline = params.get("NWSheadline") or "null"
            if isinstance(nws_headline, list):
                nws_headline = nws_headline[0] if nws_headline else "null"
            nws_headlines = params.get("NWSheadline")
            alert = {
                "area": props.get("areaDesc", "null"),
                "certainty": props.get("certainty", "null"),
                "description": props.get("description", "null"),
                "ends": props.get("ends", "null"),
                "event": event_value,
                "instruction": props.get("instruction", "null"),
                "response": props.get("response", "null"),
                "sent": props.get("sent", "null"),
                "severity": props.get("severity", "null"),
                "title": (props.get("headline") or "null").split(" by ")[0],
                "urgency": props.get("urgency", "null"),
                "NWSheadline": nws_headline,
                "NWSheadlines": nws_headlines if isinstance(nws_headlines, list) and nws_headlines else ["null"],
                "hailSize": props.get("hailSize", "null"),
                "windGust": props.get("windGust", "null"),
                "waterspoutDetection": props.get("waterspoutDetection", "null"),
                "effective": props.get("effective", "null"),
                "expires": props.get("expires", "null"),
                "onset": props.get("onset", "null"),
                "endsExpires": ends_expires,
                "status": props.get("status", "null"),
                "messageType": props.get("messageType", "null"),
                "category": props.get("category", "null"),
                "sender": props.get("sender", "null"),
                "senderName": props.get("senderName", "null"),
                "id": props.get("id", "null"),
                "zoneid": feed,
            }
            alert["icon"] = _get_icon_for_event(event_value, entry_options)
            alerts.append(alert)
        _LOGGER.debug("weatheralerts: Parsed %d alerts", len(alerts))

        return alerts

    @staticmethod
    def _track_alerts(alerts, previous, *, same_scope):
        """Compare IDs; disappearance only means deletion within the same feed."""
        now = dt_util.now()

        # 1. Build lookups for current and previous
        current_alerts_by_id = {a["id"]: a for a in alerts if a.get("id")}
        prev_alerts_by_id = {e["id"]: e for e in previous if "id" in e}

        # 2. Build new alert_tracking list
        new_alert_tracking_list = []
        for alert_tracking, alert in current_alerts_by_id.items():
            expires = alert.get("expires")
            sent = alert.get("sent")
            prev_entry = prev_alerts_by_id.get(alert_tracking)

            if prev_entry is None:
                status = "new"
                status_ts = now.isoformat()
            else:
                # if it existed before—even if it was “delete”—treat as “old”
                status = "old"
                # keep old timestamp when status unchanged; bump when status changed
                if prev_entry.get("status") == status and prev_entry.get("status_timestamp"):
                    status_ts = prev_entry["status_timestamp"]
                else:
                    status_ts = now.isoformat()

            new_alert_tracking_list.append({
                "id": alert_tracking,
                "sent": sent,
                "expires": expires,
                "status": status,
                "status_timestamp": status_ts,
            })

        # 3. Mark as deleted if missing from live alerts
        for alert_tracking, prev_entry in prev_alerts_by_id.items():
            if same_scope and alert_tracking not in current_alerts_by_id:
                prev_status = prev_entry.get("status")
                # preserve timestamp if already 'delete', else bump to now
                status_ts = (
                    prev_entry.get("status_timestamp")
                    if prev_status == "delete" and prev_entry.get("status_timestamp")
                    else now.isoformat()
                )
                new_alert_tracking_list.append({
                    "id": alert_tracking,
                    "sent": prev_entry.get("sent"),
                    "expires": prev_entry.get("expires"),
                    "status": "delete",
                    "status_timestamp": status_ts,
                })

        # 4. Drop entries based on 'delete' age (delete_buffer window)
        cleaned_alert_tracking_list = []
        delete_buffer = timedelta(minutes=10)

        for entry in new_alert_tracking_list:
            # Parse status_timestamp
            status_ts_dt = None
            status_ts = entry.get("status_timestamp")
            if status_ts:
                try:
                    status_ts_dt = dt_util.parse_datetime(status_ts)
                except (ValueError, TypeError, OverflowError):
                    status_ts_dt = None

            drop_for_delete_age = (
                entry.get("status") == "delete"
                and status_ts_dt is not None
                and now >= (status_ts_dt + delete_buffer)
            )

            # Skip adding if removed from feed for delete_buffer time window
            if drop_for_delete_age:
                continue

            cleaned_alert_tracking_list.append(entry)

        cleaned_alert_tracking_list.sort(
            key=lambda x: (x.get("sent") or "", x.get("id") or ""), reverse=True
        )
        return cleaned_alert_tracking_list

    def restore_tracking(self, previous, previous_point):
        """Reconcile saved IDs with the successful first refresh before publishing."""
        if self._last_good_data is None:
            if previous_point is None:
                self._alert_tracking_list = previous
            return
        tracking = self._track_alerts(self.data["alerts"], previous,
                                      same_scope=previous_point == self._last_point)
        self.data["alert_tracking"] = self._last_good_data["alert_tracking"] = tracking
        self._alert_tracking_list = tracking

# ------------ RestoreEntity ADDED! ------------
class WeatherAlertsSensor(CoordinatorEntity, RestoreEntity, SensorEntity):
    """Representation of a weather alerts sensor with persistence."""

    def __init__(self, coordinator, entity_name, feedid):
        if not entity_name:
            entity_name = f"weatheralerts_{_clean_for_entity_id(feedid)}"
            _LOGGER.debug("weatheralerts: entity_name missing, using fallback %s", entity_name)
        else:
            entity_name = _clean_for_entity_id(entity_name)
        _LOGGER.debug("weatheralerts: WeatherAlertsSensor __init__ for %s", entity_name)
        super().__init__(coordinator)
        self.entity_id = f"sensor.{entity_name}"
        self._feedid = feedid
        self._attr_name = None

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self._update_friendly_name()
        entry = self._get_config_entry()
        if entry:
            self.async_on_remove(
                entry.add_update_listener(self._config_entry_updated)
            )
        # ------------- Restore alert_tracking after restart -------------
        last_state = await self.async_get_last_state()
        if last_state is not None:
            restored_alert_tracking = last_state.attributes.get("alert_tracking")
            if restored_alert_tracking and isinstance(self.coordinator, WeatherAlertsCoordinator):
                extra = await self.async_get_last_extra_data()
                previous_point = extra.as_dict().get("home_point") if extra else None
                self.coordinator.restore_tracking(restored_alert_tracking, previous_point)
                _LOGGER.info("weatheralerts: Restored alert_tracking list from last state")
        # ----------------------------------------------------------

    @property
    def extra_restore_state_data(self):
        # HA-private restore data, never exposed as a state attribute.
        if self.coordinator._last_point is not None:
            return RestoredExtraData({"home_point": self.coordinator._last_point})
        return None

    async def _config_entry_updated(self, hass, entry):
        self._update_friendly_name()
        self.async_write_ha_state()

    def _get_config_entry(self):
        return self.coordinator.config_entry

    def _update_friendly_name(self):
        entry = self._get_config_entry()
        if entry:
            self._attr_name = entry.data.get(CONF_NAME, self._attr_name)
            _LOGGER.debug("weatheralerts: Updated friendly name to: %s", self._attr_name)

    @property
    def name(self):
        return self._attr_name

    @property
    def unique_id(self):
        if self.coordinator.config_entry.unique_id == "weatheralerts_home":
            return "weatheralerts_home"
        uid = f"weatheralerts_{self._feedid.replace(',', '').lower()}"
        _LOGGER.debug("weatheralerts: WeatherAlertsSensor unique_id: %s", uid)
        return uid

    @property
    def state(self):
        if self.coordinator.data:
            _LOGGER.debug("weatheralerts: WeatherAlertsSensor state: %s", self.coordinator.data.get('count', None))
            return self.coordinator.data.get("count", None)
        _LOGGER.debug("weatheralerts: WeatherAlertsSensor state: coordinator.data is None")
        return None

    @property
    def unit_of_measurement(self):
        return "Alerts"

    @property
    def icon(self):
        return "mdi:alert-octagram"

    @property
    def extra_state_attributes(self):
        if self.coordinator.data:
            _LOGGER.debug("weatheralerts: WeatherAlertsSensor extra_state_attributes: %s", self.coordinator.data)
            entry = self._get_config_entry()
            zone_name = (
                entry.options.get(CONF_ZONE_NAME)
                or entry.data.get(CONF_ZONE_NAME)
                or None
            )
            return {
                **{k: self.coordinator.data[k] for k in ("location_mode", "monitored_codes", "unresolved_alerts") if k in self.coordinator.data},
                "integration": self.coordinator.data["integration"],
                "zone": self.coordinator.data["zone"],
                "zone_name": None if self.coordinator.data.get("location_mode") == MODE_PRIVATE else zone_name,
                "alert_stats": self.coordinator.data.get("alert_stats", {}),
                "alerts": self.coordinator.data["alerts"],
                "alert_tracking": self.coordinator.data.get("alert_tracking", []),
                "error": self.coordinator.data.get("error", []),
            }
        _LOGGER.debug("weatheralerts: WeatherAlertsSensor extra_state_attributes: coordinator.data is None")
        return {}

async def async_setup_entry(hass, entry, add_entities):
    _LOGGER.debug("weatheralerts: async_setup_entry (sensor.py) called for entry: %s", entry.entry_id)
    zone = entry.options.get(CONF_ZONE, entry.data.get(CONF_ZONE, ""))
    county = entry.options.get(CONF_COUNTY, entry.data.get(CONF_COUNTY, ""))
    marine_zones = entry.options.get(CONF_MARINE_ZONES, entry.data.get(CONF_MARINE_ZONES, ""))
    conf_name = entry.data.get(CONF_NAME, "NWS Weather Alerts")
    entity_name = entry.data.get(CONF_ENTITY_NAME)
    parts = [zone] if zone else []
    if county:
        parts.append(county)
    if marine_zones:
        parts += [z.strip() for z in marine_zones.split(",") if z.strip()]
    feedid = ",".join(parts) or "home"
    if not entity_name:
        entity_name = f"weatheralerts_{_clean_for_entity_id(feedid)}"
        _LOGGER.debug("weatheralerts: entity_name missing in setup_entry! Using fallback %s", entity_name)
    _LOGGER.debug(
        "weatheralerts: async_setup_entry (sensor.py): zone=%s, county=%s, marine_zones=%s, entity_name=%s, friendly_name=%s, feedid=%s",
        zone, county, marine_zones, entity_name, conf_name, feedid
    )
    session = async_get_clientsession(hass)
    update_interval_sec = entry.options.get(CONF_UPDATE_INTERVAL, entry.data.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL))
    update_interval = timedelta(seconds=update_interval_sec)
    coordinator = WeatherAlertsCoordinator(
        hass, session, feedid, entry, update_interval=update_interval
    )
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    _LOGGER.debug("weatheralerts: async_setup_entry (sensor.py): finished first refresh")
    add_entities([WeatherAlertsSensor(coordinator, entity_name, feedid)])
    _LOGGER.info("weatheralerts: async_setup_entry (sensor.py): sensor entity added (%s)", entity_name)

async def async_setup_platform(hass, config, add_entities, discovery_info=None):
    _LOGGER.warning(
        "weatheralerts: YAML configuration for weatheralerts is deprecated and no longer supported; "
        "please use the Home Assistant UI to set up the integration. "
        "See the documentation for migration details."
    )
    return True
