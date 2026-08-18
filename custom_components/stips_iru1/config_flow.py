"""Config and options flows for STIPS IRU1."""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .api import StipsApiAuthError, StipsApiClient, StipsApiError, StipsApiPermissionError
from .catalog import async_fetch_catalog_devices, normalize_device_ip
from .const import (
    API_HOSTS,
    CONF_API_HOST,
    CONF_AUTO_UPDATE_IP,
    CONF_DEVICE_IP,
    CONF_DEVICE_IPS,
    CONF_PASSWORD,
    CONF_SERVER,
    CONF_USERNAME,
    DEFAULT_SERVER,
    DOMAIN,
    SERVER_PRODUCTION,
    SERVER_STAGING,
)

_SERVER_CHOICES = {
    SERVER_PRODUCTION: "Production",
    SERVER_STAGING: "Staging",
}


def _clean_ip(value: Any) -> str | None:
    """Validate a user-entered IP. Blank is allowed for hostname/mDNS-only control."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return None


def _stored_device_ips(entry: config_entries.ConfigEntry) -> dict[str, str]:
    """Build current device IP settings, preferring options over initial data."""
    result: dict[str, str] = {}
    data_map = entry.data.get(CONF_DEVICE_IPS, {})
    if isinstance(data_map, dict):
        result.update({str(k): str(v or "").strip() for k, v in data_map.items()})
    options_map = entry.options.get(CONF_DEVICE_IPS, {})
    if isinstance(options_map, dict):
        result.update({str(k): str(v or "").strip() for k, v in options_map.items()})

    for device in entry.data.get("devices", []):
        uid = device.get("uniqueName")
        if uid and str(uid) not in result:
            result[str(uid)] = normalize_device_ip(device)
    return result


def _stored_auto_update(entry: config_entries.ConfigEntry) -> bool:
    if CONF_AUTO_UPDATE_IP in entry.options:
        return bool(entry.options[CONF_AUTO_UPDATE_IP])
    if CONF_AUTO_UPDATE_IP in entry.data:
        return bool(entry.data[CONF_AUTO_UPDATE_IP])
    return True


class StipsIru1ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle STIPS IRU1 config flow."""

    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._pending_data: dict[str, Any] = {}
        self._pending_devices: list[dict[str, Any]] = []
        self._pending_device_ips: dict[str, str] = {}
        self._device_index = 0

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the IP settings options flow."""
        return StipsIru1OptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Select server, log in, and download the account IR catalog."""
        errors: dict[str, str] = {}
        if user_input is not None:
            server = user_input[CONF_SERVER]
            api_host = API_HOSTS[server]
            username = str(user_input[CONF_USERNAME]).strip()
            password = user_input[CONF_PASSWORD]

            session = async_get_clientsession(self.hass)
            client = StipsApiClient(host=api_host, session=session)
            try:
                await client.login(username, password)
                areas = await client.get_areas()
            except StipsApiAuthError:
                errors["base"] = "invalid_auth"
            except StipsApiPermissionError:
                errors["base"] = "no_catalog_permission"
            except StipsApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"
            else:
                if not areas:
                    errors["base"] = "no_areas"
                else:
                    try:
                        _, catalog_devices = await async_fetch_catalog_devices(client, areas)
                    except StipsApiError:
                        errors["base"] = "cannot_connect"
                    else:
                        valid_devices = [
                            device for device in catalog_devices if device.get("uniqueName")
                        ]
                        if not valid_devices:
                            errors["base"] = "no_devices"
                        else:
                            await self.async_set_unique_id(f"{DOMAIN}_{username.lower()}")
                            self._abort_if_unique_id_configured()

                            self._pending_data = {
                                CONF_SERVER: server,
                                CONF_API_HOST: api_host,
                                CONF_USERNAME: username,
                                CONF_PASSWORD: password,
                                "areas": areas,
                                "devices": catalog_devices,
                            }
                            self._pending_devices = list(valid_devices)
                            self._pending_device_ips = {
                                str(device["uniqueName"]): normalize_device_ip(device)
                                for device in valid_devices
                            }
                            self._device_index = 0
                            return await self.async_step_device_ip()

        schema = vol.Schema(
            {
                vol.Required(CONF_SERVER, default=DEFAULT_SERVER): vol.In(_SERVER_CHOICES),
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): TextSelector(
                    TextSelectorConfig(
                        type=TextSelectorType.PASSWORD,
                        autocomplete="current-password",
                    )
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        """Allow an existing entry to switch Production/Staging and refresh its catalog."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        current_host = str(entry.data.get(CONF_API_HOST) or "")
        current_server = str(entry.data.get(CONF_SERVER) or "")
        if current_server not in API_HOSTS:
            current_server = (
                SERVER_STAGING
                if current_host == API_HOSTS[SERVER_STAGING]
                else SERVER_PRODUCTION
            )

        if user_input is not None:
            server = user_input[CONF_SERVER]
            api_host = API_HOSTS[server]
            username = str(user_input[CONF_USERNAME]).strip()
            password = str(
                user_input.get(CONF_PASSWORD) or entry.data.get(CONF_PASSWORD) or ""
            )

            session = async_get_clientsession(self.hass)
            client = StipsApiClient(host=api_host, session=session)
            try:
                await client.login(username, password)
                areas = await client.get_areas()
                if not areas:
                    errors["base"] = "no_areas"
                else:
                    _, catalog_devices = await async_fetch_catalog_devices(client, areas)
                    if not any(device.get("uniqueName") for device in catalog_devices):
                        errors["base"] = "no_devices"
            except StipsApiAuthError:
                errors["base"] = "invalid_auth"
            except StipsApiPermissionError:
                errors["base"] = "no_catalog_permission"
            except StipsApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"
            else:
                if not errors:
                    await self.async_set_unique_id(f"{DOMAIN}_{username.lower()}")
                    self._abort_if_unique_id_mismatch()
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={
                            CONF_SERVER: server,
                            CONF_API_HOST: api_host,
                            CONF_USERNAME: username,
                            CONF_PASSWORD: password,
                            "areas": areas,
                            "devices": catalog_devices,
                        },
                    )

        schema = vol.Schema(
            {
                vol.Required(CONF_SERVER, default=current_server): vol.In(_SERVER_CHOICES),
                vol.Required(
                    CONF_USERNAME,
                    default=str(entry.data.get(CONF_USERNAME) or ""),
                ): str,
                vol.Optional(CONF_PASSWORD): TextSelector(
                    TextSelectorConfig(
                        type=TextSelectorType.PASSWORD,
                        autocomplete="current-password",
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ):
        """Start reauthentication for expired STIPS credentials."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ):
        """Validate new credentials and refresh the cached catalog."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        api_host = str(entry.data.get(CONF_API_HOST) or API_HOSTS[DEFAULT_SERVER])

        if user_input is not None:
            username = str(user_input[CONF_USERNAME]).strip()
            password = str(user_input[CONF_PASSWORD])
            session = async_get_clientsession(self.hass)
            client = StipsApiClient(host=api_host, session=session)
            try:
                await client.login(username, password)
                areas = await client.get_areas()
                if not areas:
                    errors["base"] = "no_areas"
                else:
                    _, catalog_devices = await async_fetch_catalog_devices(client, areas)
                    if not any(device.get("uniqueName") for device in catalog_devices):
                        errors["base"] = "no_devices"
            except StipsApiAuthError:
                errors["base"] = "invalid_auth"
            except StipsApiPermissionError:
                errors["base"] = "no_catalog_permission"
            except StipsApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"
            else:
                if not errors:
                    await self.async_set_unique_id(f"{DOMAIN}_{username.lower()}")
                    self._abort_if_unique_id_mismatch()
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={
                            CONF_USERNAME: username,
                            CONF_PASSWORD: password,
                            "areas": areas,
                            "devices": catalog_devices,
                        },
                    )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_USERNAME,
                    default=str(entry.data.get(CONF_USERNAME) or ""),
                ): str,
                vol.Required(CONF_PASSWORD): TextSelector(
                    TextSelectorConfig(
                        type=TextSelectorType.PASSWORD,
                        autocomplete="current-password",
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_device_ip(self, user_input: dict[str, Any] | None = None):
        """Review and optionally edit the saved LAN IP for each discovered IRU1."""
        if self._device_index >= len(self._pending_devices):
            return await self.async_step_ip_options()

        device = self._pending_devices[self._device_index]
        uid = str(device.get("uniqueName") or "")
        device_name = str(device.get("name") or uid or "IRU1")
        detected_ip = normalize_device_ip(device)
        current_ip = self._pending_device_ips.get(uid, detected_ip)
        errors: dict[str, str] = {}

        if user_input is not None:
            cleaned = _clean_ip(user_input.get(CONF_DEVICE_IP, ""))
            if cleaned is None:
                errors[CONF_DEVICE_IP] = "invalid_ip"
            else:
                self._pending_device_ips[uid] = cleaned
                self._device_index += 1
                if self._device_index < len(self._pending_devices):
                    return await self.async_step_device_ip()
                return await self.async_step_ip_options()

        schema = vol.Schema({vol.Optional(CONF_DEVICE_IP, default=current_ip): str})
        return self.async_show_form(
            step_id="device_ip",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "device_name": device_name,
                "device_unique_name": uid,
                "detected_ip": detected_ip or "Not reported by API",
                "device_number": str(self._device_index + 1),
                "device_count": str(len(self._pending_devices)),
            },
        )

    async def async_step_ip_options(self, user_input: dict[str, Any] | None = None):
        """Choose whether failed saved-IP control may recover through mDNS."""
        if user_input is not None:
            data = {
                **self._pending_data,
                CONF_DEVICE_IPS: dict(self._pending_device_ips),
                CONF_AUTO_UPDATE_IP: bool(user_input[CONF_AUTO_UPDATE_IP]),
            }
            username = data[CONF_USERNAME]
            return self.async_create_entry(title=f"STIPS ({username})", data=data)

        schema = vol.Schema({vol.Required(CONF_AUTO_UPDATE_IP, default=True): bool})
        return self.async_show_form(step_id="ip_options", data_schema=schema)


class StipsIru1OptionsFlow(config_entries.OptionsFlow):
    """Allow IP addresses and reactive mDNS recovery to be changed later."""

    def __init__(self) -> None:
        super().__init__()
        self._devices: list[dict[str, Any]] = []
        self._device_ips: dict[str, str] = {}
        self._device_index = 0
        self._initialized = False

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Start by reviewing each configured IRU1 IP."""
        if not self._initialized:
            self._devices = [
                device
                for device in self.config_entry.data.get("devices", [])
                if device.get("uniqueName")
            ]
            self._device_ips = _stored_device_ips(self.config_entry)
            self._device_index = 0
            self._initialized = True
        return await self.async_step_device_ip()

    async def async_step_device_ip(self, user_input: dict[str, Any] | None = None):
        """Edit one saved IP at a time."""
        if self._device_index >= len(self._devices):
            return await self.async_step_ip_options()

        device = self._devices[self._device_index]
        uid = str(device.get("uniqueName") or "")
        device_name = str(device.get("name") or uid or "IRU1")
        backend_ip = normalize_device_ip(device)
        current_ip = self._device_ips.get(uid, backend_ip)
        errors: dict[str, str] = {}

        if user_input is not None:
            cleaned = _clean_ip(user_input.get(CONF_DEVICE_IP, ""))
            if cleaned is None:
                errors[CONF_DEVICE_IP] = "invalid_ip"
            else:
                self._device_ips[uid] = cleaned
                self._device_index += 1
                if self._device_index < len(self._devices):
                    return await self.async_step_device_ip()
                return await self.async_step_ip_options()

        schema = vol.Schema({vol.Optional(CONF_DEVICE_IP, default=current_ip): str})
        return self.async_show_form(
            step_id="device_ip",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "device_name": device_name,
                "device_unique_name": uid,
                "detected_ip": backend_ip or "Not reported by API",
                "device_number": str(self._device_index + 1),
                "device_count": str(len(self._devices)),
            },
        )

    async def async_step_ip_options(self, user_input: dict[str, Any] | None = None):
        """Save reactive IP-recovery preference and IP overrides."""
        if user_input is not None:
            options = dict(self.config_entry.options)
            options[CONF_DEVICE_IPS] = dict(self._device_ips)
            options[CONF_AUTO_UPDATE_IP] = bool(user_input[CONF_AUTO_UPDATE_IP])
            return self.async_create_entry(data=options)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_AUTO_UPDATE_IP,
                    default=_stored_auto_update(self.config_entry),
                ): bool
            }
        )
        return self.async_show_form(step_id="ip_options", data_schema=schema)
