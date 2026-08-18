"""Diagnostics support for STIPS IRU1."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.redact import async_redact_data

from .const import (
    CONF_API_HOST,
    CONF_AUTO_UPDATE_IP,
    CONF_DEVICE_IPS,
    CONF_PASSWORD,
    CONF_SERVER,
    CONF_USERNAME,
)
from .local_http import entry_auto_update_ip_enabled

_TO_REDACT = {
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_DEVICE_IPS,
    "ipAddress",
    "ip_address",
    "macAddress",
    "mac_address",
    "mac",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return privacy-conscious diagnostics for a config entry."""
    devices = list(entry.data.get("devices", []))
    areas = list(entry.data.get("areas", []))
    remote_count = sum(len(device.get("remotes") or []) for device in devices)
    backend_ip_count = sum(
        1
        for device in devices
        if device.get("ipAddress") or device.get("ip_address") or device.get("ip")
    )

    safe_entry_data = {
        CONF_SERVER: entry.data.get(CONF_SERVER),
        CONF_API_HOST: entry.data.get(CONF_API_HOST),
        CONF_USERNAME: entry.data.get(CONF_USERNAME),
        CONF_PASSWORD: entry.data.get(CONF_PASSWORD),
        CONF_AUTO_UPDATE_IP: entry.data.get(CONF_AUTO_UPDATE_IP),
        CONF_DEVICE_IPS: entry.data.get(CONF_DEVICE_IPS, {}),
    }

    return {
        "entry_id": entry.entry_id,
        "entry_data": async_redact_data(safe_entry_data, _TO_REDACT),
        "entry_options": async_redact_data(dict(entry.options), _TO_REDACT),
        "reactive_ip_recovery_enabled": entry_auto_update_ip_enabled(entry),
        "catalog_summary": {
            "area_count": len(areas),
            "device_count": len(devices),
            "remote_count": remote_count,
            "devices_with_backend_ip": backend_ip_count,
        },
    }
