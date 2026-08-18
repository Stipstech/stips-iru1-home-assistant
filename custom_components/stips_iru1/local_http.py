"""Local HTTP helpers for low-latency IRU1 control and reactive IP recovery."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_AUTO_UPDATE_IP,
    CONF_DEVICE_IPS,
    DOMAIN,
    LOCAL_HTTP_PASSWORD,
    LOCAL_HTTP_USERNAME,
)

_LOGGER = logging.getLogger(__name__)


def _dedupe_hosts(hosts: list[str]) -> list[str]:
    out: list[str] = []
    for host in hosts:
        h = str(host or "").strip()
        if h and h not in out:
            out.append(h)
    return out


def _validated_ip(value: Any) -> str:
    """Return a normalized IP string or an empty string."""
    s = str(value or "").strip()
    if not s:
        return ""
    try:
        return str(ipaddress.ip_address(s))
    except ValueError:
        return ""


def _normalized_device_name(value: Any) -> str:
    """Normalize a device hostname/name for safe identity comparison."""
    name = str(value or "").strip().lower().rstrip(".")
    if name.endswith(".local"):
        name = name[:-6]
    return name


def iter_dns_host_candidates(device_unique_name: str, preferred_ip: str = "") -> list[str]:
    """Build fast local-control candidates with the saved IP first, then mDNS names."""
    hosts: list[str] = []

    ip_s = _validated_ip(preferred_ip)
    if ip_s:
        hosts.append(ip_s)

    unique_name = str(device_unique_name or "").strip()
    if unique_name:
        if "." not in unique_name:
            hosts.append(f"{unique_name}.local")
        hosts.append(unique_name)
    return _dedupe_hosts(hosts)


def is_device_mdns_host(device_unique_name: str, host: str) -> bool:
    """Return True when host is the device hostname/mDNS fallback rather than an IP."""
    if _validated_ip(host):
        return False
    return _normalized_device_name(host) == _normalized_device_name(device_unique_name)


def _entry_device_ip_map(entry: ConfigEntry) -> dict[str, str]:
    """Return saved device IPs, with options overriding initial config data."""
    merged: dict[str, str] = {}
    data_map = entry.data.get(CONF_DEVICE_IPS, {})
    if isinstance(data_map, dict):
        merged.update({str(k): str(v or "").strip() for k, v in data_map.items()})
    options_map = entry.options.get(CONF_DEVICE_IPS, {})
    if isinstance(options_map, dict):
        merged.update({str(k): str(v or "").strip() for k, v in options_map.items()})
    return merged


def entry_auto_update_ip_enabled(entry: ConfigEntry) -> bool:
    """Return whether reactive mDNS /device_info IP recovery is enabled."""
    if CONF_AUTO_UPDATE_IP in entry.options:
        return bool(entry.options[CONF_AUTO_UPDATE_IP])
    if CONF_AUTO_UPDATE_IP in entry.data:
        return bool(entry.data[CONF_AUTO_UPDATE_IP])
    # Existing entries created before this option existed get automatic recovery by default.
    return True


def _entry_has_device(entry: ConfigEntry, device_unique_name: str) -> bool:
    uid = str(device_unique_name or "").strip()
    return any(str(device.get("uniqueName") or "").strip() == uid for device in entry.data.get("devices", []))


def get_configured_device_ip(
    hass: HomeAssistant,
    *,
    device_unique_name: str,
    backend_ip: str = "",
) -> str:
    """Return the user/auto-saved IP for a device, falling back to the cloud catalog IP.

    An explicitly saved blank value means "use hostname/mDNS only" and intentionally does
    not fall back to the backend IP.
    """
    uid = str(device_unique_name or "").strip()
    if uid:
        for entry in hass.config_entries.async_entries(DOMAIN):
            options_map = entry.options.get(CONF_DEVICE_IPS, {})
            if isinstance(options_map, dict) and uid in options_map:
                return _validated_ip(options_map.get(uid))
            data_map = entry.data.get(CONF_DEVICE_IPS, {})
            if isinstance(data_map, dict) and uid in data_map:
                return _validated_ip(data_map.get(uid))
    return _validated_ip(backend_ip)


def _extract_live_ip(payload: dict[str, Any]) -> str:
    for key in ("ip_address", "ipAddress", "ip", "Ip"):
        val = payload.get(key)
        if val is None:
            continue
        ip_s = _validated_ip(val)
        if ip_s:
            return ip_s
    return ""


def _url_host(host: str) -> str:
    """Bracket IPv6 literals when they are used in an HTTP URL."""
    h = str(host or "").strip()
    try:
        if ipaddress.ip_address(h).version == 6:
            return f"[{h}]"
    except ValueError:
        pass
    return h


def local_http_url(host: str, path: str) -> str:
    """Build a local HTTP URL that also supports IPv6 literal addresses."""
    clean_path = "/" + str(path or "").lstrip("/")
    return f"http://{_url_host(host)}{clean_path}"


async def async_fetch_device_info_live_ip(
    hass: HomeAssistant,
    *,
    host: str,
    timeout: aiohttp.ClientTimeout,
    expected_device_unique_name: str = "",
) -> str:
    """Try GET /device_info on one host and return its validated live IP.

    If the device reports a name, reject a response from a different IRU. This helps avoid
    saving an address from the wrong LAN host after DHCP changes.
    """
    session = async_get_clientsession(hass)
    auth = aiohttp.BasicAuth(LOCAL_HTTP_USERNAME, LOCAL_HTTP_PASSWORD)
    url = local_http_url(host, "/device_info")
    try:
        async with session.get(url, auth=auth, timeout=timeout) as response:
            if response.status >= 400:
                return ""
            try:
                payload = await response.json(content_type=None)
            except Exception:
                body = await response.text()
                try:
                    payload = json.loads(body)
                except Exception:
                    return ""
            if not isinstance(payload, dict):
                return ""

            expected = _normalized_device_name(expected_device_unique_name)
            reported = _normalized_device_name(payload.get("name"))
            if expected and reported and expected != reported:
                _LOGGER.warning(
                    "Ignoring /device_info from %s: reported device %s, expected %s",
                    host,
                    reported,
                    expected,
                )
                return ""
            return _extract_live_ip(payload)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return ""


async def async_discover_device_live_ip(
    hass: HomeAssistant,
    *,
    device_unique_name: str,
) -> str:
    """Resolve one IRU through mDNS/name and read its current IP from /device_info."""
    unique_name = str(device_unique_name or "").strip()
    if not unique_name:
        return ""

    hosts = iter_dns_host_candidates(unique_name)
    timeout = aiohttp.ClientTimeout(total=1.5, connect=0.7, sock_connect=0.7, sock_read=1.0)
    for host in hosts:
        live_ip = await async_fetch_device_info_live_ip(
            hass,
            host=host,
            timeout=timeout,
            expected_device_unique_name=unique_name,
        )
        if live_ip:
            return live_ip
    return ""


def _save_entry_device_ip(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    device_unique_name: str,
    live_ip: str,
) -> bool:
    """Persist one discovered IP to config-entry options if it changed."""
    uid = str(device_unique_name or "").strip()
    normalized = _validated_ip(live_ip)
    if not uid or not normalized:
        return False

    current = _entry_device_ip_map(entry)
    old_ip = _validated_ip(current.get(uid))
    if old_ip == normalized:
        return False

    current[uid] = normalized
    options = dict(entry.options)
    options[CONF_DEVICE_IPS] = current
    hass.config_entries.async_update_entry(entry, options=options)
    _LOGGER.info("Updated STIPS IRU1 %s local IP: %s -> %s", uid, old_ip or "unset", normalized)
    return True


async def async_recover_device_ip_from_mdns_host(
    hass: HomeAssistant,
    *,
    device_unique_name: str,
    mdns_host: str,
) -> str:
    """Read /device_info through a working mDNS host and save the new IP.

    This is the automatic recovery path. It is intentionally called only after a command
    failed via the saved IP (or no IP was saved) and then succeeded via mDNS/hostname.
    """
    uid = str(device_unique_name or "").strip()
    if not uid or not is_device_mdns_host(uid, mdns_host):
        return ""

    matching_entries = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if _entry_has_device(entry, uid) and entry_auto_update_ip_enabled(entry)
    ]
    if not matching_entries:
        return ""

    timeout = aiohttp.ClientTimeout(total=1.5, connect=0.7, sock_connect=0.7, sock_read=1.0)
    live_ip = await async_fetch_device_info_live_ip(
        hass,
        host=mdns_host,
        timeout=timeout,
        expected_device_unique_name=uid,
    )
    if not live_ip:
        return ""

    for entry in matching_entries:
        _save_entry_device_ip(
            hass,
            entry,
            device_unique_name=uid,
            live_ip=live_ip,
        )
    return live_ip


def schedule_ip_recovery_after_mdns_success(
    hass: HomeAssistant,
    *,
    device_unique_name: str,
    successful_host: str,
    preferred_ip: str,
    preferred_ip_failed: bool,
) -> None:
    """Schedule non-blocking IP recovery after a successful hostname/mDNS fallback.

    No periodic timer is used. Recovery runs only when the configured IP was unavailable
    (or absent) but the same device was reachable through its hostname/mDNS address.
    """
    if not is_device_mdns_host(device_unique_name, successful_host):
        return
    if preferred_ip and not preferred_ip_failed:
        return

    hass.async_create_task(
        async_recover_device_ip_from_mdns_host(
            hass,
            device_unique_name=device_unique_name,
            mdns_host=successful_host,
        )
    )


async def async_refresh_entry_device_ips(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    force: bool = False,
) -> dict[str, str]:
    """Manually discover current device IPs and persist changed values.

    Automatic recovery does not call this on a timer. The function remains for the explicit
    ``stips_iru1.refresh_device_ips`` action and troubleshooting.
    """
    current = _entry_device_ip_map(entry)
    if not force and not entry_auto_update_ip_enabled(entry):
        return current

    uids = [
        str(device.get("uniqueName"))
        for device in entry.data.get("devices", [])
        if device.get("uniqueName")
    ]
    if not uids:
        return current

    async def _discover(uid: str) -> tuple[str, str]:
        return uid, await async_discover_device_live_ip(hass, device_unique_name=uid)

    discovered = await asyncio.gather(*(_discover(uid) for uid in uids))
    updated = dict(current)
    changed = False
    for uid, live_ip in discovered:
        if not live_ip:
            continue
        old_ip = _validated_ip(updated.get(uid))
        if old_ip == live_ip:
            continue
        updated[uid] = live_ip
        changed = True
        _LOGGER.info("Updated STIPS IRU1 %s local IP: %s -> %s", uid, old_ip or "unset", live_ip)

    if changed:
        options = dict(entry.options)
        options[CONF_DEVICE_IPS] = updated
        hass.config_entries.async_update_entry(entry, options=options)
    return updated


async def async_build_control_hosts(
    hass: HomeAssistant,
    *,
    device_unique_name: str,
    backend_ip: str,
) -> tuple[list[str], str]:
    """Return command hosts without doing any blocking mDNS/device-info probe.

    Priority is saved/manual IP -> ``.local`` mDNS hostname -> bare device hostname. The
    second return value is the currently saved preferred IP. If that IP fails but a hostname
    succeeds, the caller can schedule reactive /device_info recovery in the background.
    """
    preferred_ip = get_configured_device_ip(
        hass,
        device_unique_name=device_unique_name,
        backend_ip=backend_ip,
    )
    hosts = iter_dns_host_candidates(device_unique_name, preferred_ip)
    return hosts, preferred_ip
