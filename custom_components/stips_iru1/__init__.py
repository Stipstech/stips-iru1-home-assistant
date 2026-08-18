"""STIPS IRU1 Home Assistant integration."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import StipsApiAuthError, StipsApiClient, StipsApiError
from .catalog import (
    async_fetch_catalog_devices,
    normalize_device_ip,
    normalize_device_mac,
)
from .const import (
    CONF_API_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    DOMAIN,
    LOCAL_HTTP_PASSWORD,
    LOCAL_HTTP_USERNAME,
    PLATFORMS,
)
from .local_http import (
    async_build_control_hosts,
    async_refresh_entry_device_ips,
    local_http_url,
    schedule_ip_recovery_after_mdns_success,
)

SERVICE_SEND_AC_STATUS = "send_ac_status"
SERVICE_FIELD_DEVICE_UNIQUE_NAME = "device_unique_name"
SERVICE_FIELD_REMOTE_ID = "remote_id"
SERVICE_FIELD_AC_STATUS = "ac_status"

SERVICE_SEND_AC_STATUS_SCHEMA = vol.Schema(
    {
        vol.Required(SERVICE_FIELD_DEVICE_UNIQUE_NAME): cv.string,
        vol.Optional(SERVICE_FIELD_REMOTE_ID): cv.string,
        vol.Required(SERVICE_FIELD_AC_STATUS): dict,
    }
)

SERVICE_REFRESH_CATALOG = "refresh_catalog"
SERVICE_FIELD_CONFIG_ENTRY_ID = "config_entry_id"
SERVICE_REFRESH_CATALOG_SCHEMA = vol.Schema(
    {vol.Optional(SERVICE_FIELD_CONFIG_ENTRY_ID): cv.string}
)

SERVICE_REFRESH_DEVICE_IPS = "refresh_device_ips"
SERVICE_REFRESH_DEVICE_IPS_SCHEMA = vol.Schema(
    {vol.Optional(SERVICE_FIELD_CONFIG_ENTRY_ID): cv.string}
)


def _async_service(hass: HomeAssistant, fn):
    """Build an async service handler Home Assistant will await."""

    async def _handler(service_call):
        await fn(hass, service_call)

    return _handler


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up integration-level actions once.

    Home Assistant's integration quality guidance recommends registering actions in
    ``async_setup`` rather than separately for every config entry.
    """
    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_AC_STATUS,
        _async_service(hass, _async_handle_send_ac_status_service),
        schema=SERVICE_SEND_AC_STATUS_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REFRESH_CATALOG,
        _async_service(hass, _async_handle_refresh_catalog),
        schema=SERVICE_REFRESH_CATALOG_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REFRESH_DEVICE_IPS,
        _async_service(hass, _async_handle_refresh_device_ips),
        schema=SERVICE_REFRESH_DEVICE_IPS_SCHEMA,
    )
    return True


def _register_catalog_devices(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Ensure every IR unit in the catalog has a device registry entry.

    Units with only protocol-AC remotes create no signal-based remote entities; without this,
    Home Assistant would not list them under the integration.
    """
    reg = dr.async_get(hass)
    for device in entry.data.get("devices", []):
        uid = device.get("uniqueName")
        if not uid:
            continue
        name = device.get("name") or uid
        sw = device.get("buildVersion")
        kwargs: dict[str, Any] = {
            "config_entry_id": entry.entry_id,
            "identifiers": {(DOMAIN, str(uid))},
            "name": name,
            "manufacturer": "STIPS",
            "model": "IRU1",
            "sw_version": str(sw) if sw is not None else None,
        }
        mac = normalize_device_mac(device)
        if mac:
            kwargs["connections"] = {(dr.CONNECTION_NETWORK_MAC, mac)}
        area_name = device.get("areaName")
        if area_name:
            kwargs["suggested_area"] = str(area_name)
        reg.async_get_or_create(**kwargs)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up STIPS IRU1 from a config entry.

    There is intentionally no periodic mDNS timer here. Automatic IP recovery is reactive:
    a command tries the saved IP first, and only after that IP cannot be reached but mDNS
    succeeds does the integration read /device_info and save the new address in background.
    """
    _register_catalog_devices(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_handle_refresh_device_ips(hass: HomeAssistant, service_call) -> None:
    """Force a manual mDNS /device_info IP discovery and save changed IPs."""
    target_id: str | None = service_call.data.get(SERVICE_FIELD_CONFIG_ENTRY_ID)
    entries = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if target_id is None or e.entry_id == target_id
    ]
    if not entries:
        raise HomeAssistantError("No STIPS IRU1 config entry matched")

    await asyncio.gather(
        *(async_refresh_entry_device_ips(hass, entry, force=True) for entry in entries)
    )


async def _async_handle_refresh_catalog(hass: HomeAssistant, service_call) -> None:
    """Re-download catalog from STIPS and reload the config entry."""
    target_id: str | None = service_call.data.get(SERVICE_FIELD_CONFIG_ENTRY_ID)
    entries = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if target_id is None or e.entry_id == target_id
    ]
    if not entries:
        raise HomeAssistantError("No STIPS IRU1 config entry matched")

    session = async_get_clientsession(hass)
    for entry in entries:
        client = StipsApiClient(host=entry.data[CONF_API_HOST], session=session)
        try:
            await client.login(entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])
            areas, devices = await async_fetch_catalog_devices(client)
        except StipsApiAuthError as err:
            # Start Home Assistant's linked reauthentication flow so the user can repair
            # expired credentials without deleting the integration.
            entry.async_start_reauth(hass)
            raise HomeAssistantError(
                "STIPS login failed. Home Assistant started reauthentication for this entry."
            ) from err
        except StipsApiError as err:
            raise HomeAssistantError(f"STIPS catalog refresh failed: {err}") from err

        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, "areas": areas, "devices": devices},
        )
        await hass.config_entries.async_reload(entry.entry_id)


async def _async_handle_send_ac_status_service(hass: HomeAssistant, service_call) -> None:
    """Send AC status locally to an IRU1 device endpoint."""
    device_unique_name: str = service_call.data[SERVICE_FIELD_DEVICE_UNIQUE_NAME]
    remote_id: str | None = service_call.data.get(SERVICE_FIELD_REMOTE_ID)
    ac_status: dict[str, Any] = dict(service_call.data[SERVICE_FIELD_AC_STATUS])

    device_ip = ""
    device_found = False
    for entry in hass.config_entries.async_entries(DOMAIN):
        for device in entry.data.get("devices", []):
            if device.get("uniqueName") != device_unique_name:
                continue
            device_found = True
            device_ip = normalize_device_ip(device)
            if remote_id:
                for rem in device.get("remotes") or []:
                    if str(rem.get("id")) != str(remote_id):
                        continue
                    model = rem.get("model") or {}
                    if "type" not in ac_status and model.get("protocol") is not None:
                        ac_status["type"] = int(model["protocol"])
                    if "model" not in ac_status:
                        ac_status["model"] = 0
                    break
            break
        if device_found:
            break

    if not device_found:
        raise HomeAssistantError(f"STIPS IRU1 device not found: {device_unique_name}")

    device_hosts, preferred_ip = await async_build_control_hosts(
        hass,
        device_unique_name=device_unique_name,
        backend_ip=device_ip,
    )

    if not device_hosts:
        raise HomeAssistantError(f"Device is missing a local host: {device_unique_name}")

    session = async_get_clientsession(hass)
    auth = aiohttp.BasicAuth(LOCAL_HTTP_USERNAME, LOCAL_HTTP_PASSWORD)
    timeout = aiohttp.ClientTimeout(total=2.5, connect=0.8, sock_connect=0.8, sock_read=1.5)
    payload = {k: str(v) for k, v in ac_status.items()}

    last_error: Exception | None = None
    preferred_ip_failed = False
    for host in device_hosts:
        url = local_http_url(host, "/local-ir/ac-command")
        try:
            async with session.post(url, data=payload, auth=auth, timeout=timeout) as response:
                if response.status >= 400:
                    body = await response.text()
                    last_error = HomeAssistantError(
                        f"Local AC request failed ({response.status}) via {host}: {body[:120]}"
                    )
                    continue
                schedule_ip_recovery_after_mdns_success(
                    hass,
                    device_unique_name=device_unique_name,
                    successful_host=host,
                    preferred_ip=preferred_ip,
                    preferred_ip_failed=preferred_ip_failed,
                )
                return
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            if preferred_ip and host == preferred_ip:
                preferred_ip_failed = True
            last_error = err
            continue

    if isinstance(last_error, HomeAssistantError):
        raise HomeAssistantError(f"{last_error} | hosts={', '.join(device_hosts)}")
    if last_error is not None:
        detail = str(last_error).strip() or type(last_error).__name__
        raise HomeAssistantError(
            f"Cannot reach IR device locally (hosts: {', '.join(device_hosts)}): {detail}"
        ) from last_error
    raise HomeAssistantError(f"Cannot reach IR device locally (hosts: {', '.join(device_hosts)})")
