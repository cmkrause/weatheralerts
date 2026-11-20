"""Authenticated, read-only access to the current alert footprint."""

import asyncio

from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.components import websocket_api
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN


@callback
def register_area_api(hass):
    key = "weatheralerts_area_api"
    if key not in hass.data:
        websocket_api.async_register_command(hass, alert_area)
        hass.data[key] = True


@websocket_api.websocket_command(
    {"type": "weatheralerts/area", "entity_id": str, "alert_id": str}
)
@websocket_api.async_response
async def alert_area(hass, connection, msg):
    entity_id = msg.get("entity_id", "")
    if not connection.user.permissions.check_entity(entity_id, POLICY_READ):
        connection.send_error(msg["id"], "unauthorized", "Sensor access required")
        return
    entity = er.async_get(hass).async_get(entity_id)
    entry = (
        hass.config_entries.async_get_entry(entity.config_entry_id)
        if entity and entity.platform == DOMAIN
        else None
    )
    coordinator = getattr(entry, "runtime_data", None)
    state = hass.states.get(entity_id)
    alert_id = msg.get("alert_id")
    if (
        not coordinator
        or not state
        or state.state in ("unavailable", "unknown")
        or not any(a.get("id") == alert_id for a in state.attributes.get("alerts", []))
    ):
        connection.send_error(
            msg["id"], "not_found", "This alert is no longer available"
        )
        return
    try:
        async with asyncio.timeout(120):
            result = await coordinator.map_area(alert_id)
    except KeyError:
        connection.send_error(msg["id"], "not_found", "This alert has changed")
        return
    except TimeoutError:
        connection.send_error(msg["id"], "timeout", "Area lookup timed out")
        return
    connection.send_result(msg["id"], result)
