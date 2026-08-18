"""Remote platform: Broadlink-style `remote.send_command` for cached IR signals."""

from __future__ import annotations

import asyncio
from typing import Any, Iterable
from urllib.parse import quote

import aiohttp
from homeassistant.components.remote import RemoteEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .catalog import (
    model_has_ir_signals,
    normalize_device_ip,
    normalize_device_mac,
    normalize_device_online,
)
from .const import (
    DOMAIN,
    LOCAL_HTTP_PASSWORD,
    LOCAL_HTTP_USERNAME,
    is_learned_ac,
    is_protocol_ac,
    remote_uses_signal_buttons,
)
from .local_http import (
    async_build_control_hosts,
    local_http_url,
    schedule_ip_recovery_after_mdns_success,
)

try:
    from homeassistant.components.remote.const import RemoteEntityFeature
except ImportError:
    RemoteEntityFeature = None  # type: ignore[misc,assignment]


_STIPS_ICON_SVG = """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 256 256'>
<defs>
<linearGradient id='bg' x1='0' y1='0' x2='1' y2='1'>
<stop offset='0%' stop-color='#4267b2'/>
<stop offset='38%' stop-color='#171d2b'/>
<stop offset='100%' stop-color='#0a0d13'/>
</linearGradient>
<linearGradient id='glow' x1='1' y1='0' x2='0' y2='1'>
<stop offset='0%' stop-color='#7a3bff' stop-opacity='0.65'/>
<stop offset='100%' stop-color='#7a3bff' stop-opacity='0'/>
</linearGradient>
<linearGradient id='mark' x1='0' y1='0' x2='1' y2='1'>
<stop offset='0%' stop-color='#5fa8ff'/>
<stop offset='100%' stop-color='#7a3bff'/>
</linearGradient>
</defs>
<rect width='256' height='256' rx='26' fill='url(#bg)'/>
<rect width='256' height='256' rx='26' fill='url(#glow)'/>
<path d='M62 148 L86 134 L86 111 L111 96 L111 106 L95 116 L95 139 L70 154 L70 173 L111 149 L111 160 L62 188 Z' fill='url(#mark)'/>
<text x='122' y='153' font-family='Segoe UI, Arial, sans-serif' font-size='47' letter-spacing='2' fill='url(#mark)'>STIPS</text>
</svg>"""
STIPS_ICON_ENTITY_PICTURE = f"data:image/svg+xml;utf8,{quote(_STIPS_ICON_SVG)}"


def _icon_for_remote_type(remote_type: str | None) -> str:
    """Return a distinct HA icon for the remote type."""
    t = (remote_type or "").strip().lower().replace(" ", "")
    if t in {"ac", "learnedac"}:
        return "mdi:air-conditioner"
    if t in {"tv", "learnedtv"}:
        return "mdi:television-classic"
    if t == "fan":
        return "mdi:fan"
    if t in {"box", "settopbox", "satellite", "cable", "dvd"}:
        return "mdi:set-top-box"
    if t == "projector":
        return "mdi:projector"
    if t in {"avr", "areceiver", "audio", "avreceiver"}:
        return "mdi:speaker"
    if t == "camera":
        return "mdi:camera"
    return "mdi:remote"


def build_protocol_ac_form_fields(
    remote: dict[str, Any], *, power: int | None = None
) -> dict[str, str]:
    """Form body for POST /local-ir/ac-command from cached STIPS protocol AC remote."""
    model = remote.get("model") or {}
    proto = model.get("protocol")
    if proto is None:
        raise HomeAssistantError("This AC remote has no model.protocol in the catalog")
    fields: dict[str, Any] = {"type": int(proto), "model": 0}

    ac = remote.get("acStatus") or {}
    last_key = str(ac.get("lastModeName") or "cool").strip().lower()
    modes = ac.get("modeStates") or {}
    state: dict[str, Any] | None = None
    if isinstance(modes, dict):
        for mk, mv in modes.items():
            if str(mk).strip().lower() == last_key and isinstance(mv, dict):
                state = mv
                break
        if state is None:
            for mv in modes.values():
                if isinstance(mv, dict):
                    state = mv
                    break

    if isinstance(state, dict):
        mapping = (
            ("power", "power"),
            ("mode", "mode"),
            ("fan", "fan"),
            ("temperature", "temp"),
            ("swingV", "swingV"),
            ("swingH", "swingH"),
            ("light", "light"),
            ("beep", "beep"),
            ("econo", "econo"),
            ("filter", "filter"),
            ("turbo", "turbo"),
            ("quiet", "quiet"),
            ("clean", "clean"),
            ("sleep", "sleep"),
        )
        for src, dst in mapping:
            if src in state and state[src] is not None:
                fields[dst] = state[src]

    if power is not None:
        fields["power"] = int(power)

    fields.setdefault("power", 0)
    fields.setdefault("mode", 1)
    fields.setdefault("fan", 3)
    fields.setdefault("temp", 22)

    return {k: str(v) for k, v in fields.items()}


def _normalize_command_key(name: str) -> str:
    return name.strip().lower()


def _register_cmd(
    cmd_map: dict[str, tuple[str, int]],
    display: list[str],
    seen_norm: set[str],
    *,
    signal: str,
    frequency: int,
    label: str,
) -> None:
    """Add one command under normalized key + slug alias."""
    nm = str(label).strip()
    if not nm or not signal or not str(signal).strip():
        return
    norm = _normalize_command_key(nm)
    if norm not in seen_norm:
        seen_norm.add(norm)
        display.append(nm)
    cmd_map[norm] = (str(signal), frequency)
    slug = norm.replace(" ", "_").replace("+", "plus").replace("-", "minus")
    cmd_map[slug] = (str(signal), frequency)


def _learned_signal_label(sig: dict[str, Any], index: int) -> str:
    """Label for Models/read ``signals[]`` rows (name often null on LearnedAc)."""
    name = sig.get("name") or sig.get("Name")
    if name is not None and str(name).strip():
        return str(name).strip()
    mode = str(sig.get("mode") or "mode").strip().lower().replace(" ", "_")
    fan_raw = sig.get("fanSpeed") or sig.get("fan") or "auto"
    fan = str(fan_raw).strip().lower().replace(" ", "_")
    temp = sig.get("temperature")
    if temp is not None:
        base = f"{mode}_{temp}_{fan}"
    else:
        base = f"{mode}_{fan}"
    return f"{base}_{index}" if base else f"signal_{index}"


def _build_command_map(model: dict[str, Any]) -> tuple[dict[str, tuple[str, int]], list[str]]:
    """Map lookup keys -> (signal, frequency). Also return ordered display names."""
    frequency = int(model.get("frequency") or model.get("Frequency") or 38000)
    cmd_map: dict[str, tuple[str, int]] = {}
    display: list[str] = []
    seen_norm: set[str] = set()

    raw = (
        (model.get("buttons") or model.get("Buttons") or [])
        + (model.get("otherButtons") or model.get("OtherButtons") or [])
    )
    for btn in raw:
        if not isinstance(btn, dict):
            continue
        signal = btn.get("signal") or btn.get("Signal")
        name = btn.get("name") or btn.get("Name")
        if not signal or not name:
            continue
        _register_cmd(cmd_map, display, seen_norm, signal=str(signal), frequency=frequency, label=str(name))

    for i, sig in enumerate(model.get("signals") or model.get("Signals") or []):
        if not isinstance(sig, dict):
            continue
        signal = sig.get("signal") or sig.get("Signal")
        if not signal or not str(signal).strip():
            continue
        label = _learned_signal_label(sig, i)
        candidate = label
        dup = 2
        while _normalize_command_key(candidate) in seen_norm:
            candidate = f"{label}_{dup}"
            dup += 1
        _register_cmd(cmd_map, display, seen_norm, signal=str(signal), frequency=frequency, label=candidate)

    pon = model.get("powerOnSignal") or model.get("PowerOnSignal")
    if pon is not None and str(pon).strip():
        _register_cmd(cmd_map, display, seen_norm, signal=str(pon), frequency=frequency, label="power_on")
    poff = model.get("powerOffSignal") or model.get("PowerOffSignal")
    if poff is not None and str(poff).strip():
        _register_cmd(cmd_map, display, seen_norm, signal=str(poff), frequency=frequency, label="power_off")

    return cmd_map, display


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create remote entities for TV/Fan/Learned non-AC remotes only."""
    entities: list[RemoteEntity] = []
    active_unique_ids: set[str] = set()
    devices = entry.data.get("devices", [])

    for device in devices:
        device_unique_name = device.get("uniqueName")
        if not device_unique_name:
            continue
        device_ip = normalize_device_ip(device)
        device_mac = normalize_device_mac(device)
        device_online = normalize_device_online(device)
        device_name = device.get("name") or device_unique_name
        remotes = device.get("remotes") or []

        for idx, remote in enumerate(remotes):
            rtype = str(remote.get("type") or "")
            remote_id = remote.get("id")
            friendly = remote.get("friendlyName") or remote.get("type") or "Remote"
            rid = (
                str(remote_id)
                if remote_id
                else f"{idx}_{_normalize_command_key(friendly).replace(' ', '_')}"
            )

            model = remote.get("model") or {}
            has_signal_buttons = model_has_ir_signals(model)

            # AC and LearnedAc are climate-only to avoid duplicate cards/toggles.
            if is_protocol_ac(rtype) or is_learned_ac(rtype):
                continue

            if not remote_uses_signal_buttons(rtype) and not has_signal_buttons:
                continue
            cmd_map, command_names = _build_command_map(model)
            if not cmd_map:
                continue
            entity = StipsIruRemote(
                hass=hass,
                device_unique_name=str(device_unique_name),
                device_name=device_name,
                device_ip=device_ip,
                device_mac=device_mac,
                device_online=device_online,
                remote_id=rid,
                friendly_name=str(friendly),
                remote_type=rtype,
                cmd_map=cmd_map,
                command_names=command_names,
            )
            entities.append(entity)
            active_unique_ids.add(entity.unique_id)

    # Remove stale AC remote entities left from previous integration behavior.
    registry = er.async_get(hass)
    for ent in er.async_entries_for_config_entry(registry, entry.entry_id):
        if ent.domain != "remote" or not ent.unique_id:
            continue
        if not str(ent.unique_id).startswith(f"{DOMAIN}_"):
            continue
        if ent.unique_id not in active_unique_ids:
            registry.async_remove(ent.entity_id)

    async_add_entities(entities)


class StipsIruProtocolAcRemote(RemoteEntity):
    """Backend protocol AC (IRac): power on/off via ``/local-ir/ac-command`` (not raw timings)."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_icon = "mdi:remote"

    def __init__(
        self,
        *,
        hass: HomeAssistant,
        device_unique_name: str,
        device_name: str,
        device_ip: str,
        device_mac: str,
        device_online: bool,
        remote_id: str,
        friendly_name: str,
        remote_snapshot: dict[str, Any],
    ) -> None:
        super().__init__()
        self.hass = hass
        if RemoteEntityFeature is not None:
            on_f = getattr(RemoteEntityFeature, "TURN_ON", 0)
            off_f = getattr(RemoteEntityFeature, "TURN_OFF", 0)
            sc = getattr(RemoteEntityFeature, "SEND_COMMAND", 0)
            self._attr_supported_features = RemoteEntityFeature(on_f | off_f | sc)
        self._device_unique_name = device_unique_name
        self._device_name = device_name
        self._device_ip = device_ip
        self._device_ip_live = ""
        self._device_mac = device_mac
        self._device_online = device_online
        self._remote_snapshot = remote_snapshot
        safe_rid = "".join(c if c.isalnum() or c in "-_" else "_" for c in remote_id)[:80]
        self._attr_unique_id = f"{DOMAIN}_{device_unique_name}_ac_{safe_rid}"
        self._attr_name = friendly_name
        self._attr_available = True
        self._attr_icon = _icon_for_remote_type(remote_snapshot.get("type"))
        ac = remote_snapshot.get("acStatus") or {}
        self._attr_extra_state_attributes = {
            "remote_type": remote_snapshot.get("type"),
            "device_unique_name": device_unique_name,
            "unique_name": device_unique_name,
            "device_ip": device_ip or None,
            "device_ip_backend": device_ip or None,
            "device_ip_live": None,
            "device_mac": device_mac or None,
            "device_online": device_online,
            "ip_configured": bool(device_ip),
            "stips_control": "ac_command",
            "last_mode_name": ac.get("lastModeName"),
            "commands": ["on", "off"],
        }

    @property
    def available(self) -> bool:
        return True

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._device_unique_name)},
            "name": self._device_name,
            "manufacturer": "STIPS",
            "model": "IRU1",
            "connections": {("mac", self._device_mac)} if self._device_mac else set(),
            "configuration_url": f"http://{self._device_unique_name}/device_info",
        }

    async def _post_ac(self, *, power: int) -> None:
        hosts, live_ip = await async_build_control_hosts(
            self.hass,
            device_unique_name=self._device_unique_name,
            backend_ip=self._device_ip,
        )
        if not hosts:
            raise HomeAssistantError(
                "Device host is missing; run stips_iru1.refresh_catalog while the IRU is online."
            )
        if live_ip:
            self._device_ip_live = live_ip
            self._attr_extra_state_attributes["device_ip_live"] = live_ip
            self._attr_extra_state_attributes["device_ip"] = live_ip
        payload = build_protocol_ac_form_fields(self._remote_snapshot, power=power)
        session = async_get_clientsession(self.hass)
        auth = aiohttp.BasicAuth(LOCAL_HTTP_USERNAME, LOCAL_HTTP_PASSWORD)
        timeout = aiohttp.ClientTimeout(total=2.5, connect=0.8, sock_connect=0.8, sock_read=1.5)
        last_error: Exception | None = None
        preferred_ip_failed = False
        for host in hosts:
            url = local_http_url(host, "/local-ir/ac-command")
            try:
                async with session.post(url, data=payload, auth=auth, timeout=timeout) as response:
                    if response.status >= 400:
                        body = await response.text()
                        last_error = HomeAssistantError(
                            f"Local AC request failed ({response.status}) via {host}: {body[:160]}"
                        )
                        continue
                    schedule_ip_recovery_after_mdns_success(
                        self.hass,
                        device_unique_name=self._device_unique_name,
                        successful_host=host,
                        preferred_ip=live_ip,
                        preferred_ip_failed=preferred_ip_failed,
                    )
                    return
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                if live_ip and host == live_ip:
                    preferred_ip_failed = True
                last_error = err
                continue
        if isinstance(last_error, HomeAssistantError):
            raise HomeAssistantError(f"{last_error} | hosts={', '.join(hosts)}")
        if last_error is not None:
            detail = str(last_error).strip() or type(last_error).__name__
            raise HomeAssistantError(
                f"Cannot reach IR device locally (hosts: {', '.join(hosts)}): {detail}"
            ) from last_error
        raise HomeAssistantError(f"Cannot reach IR device locally (hosts: {', '.join(hosts)})")

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._post_ac(power=1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._post_ac(power=0)

    async def async_send_command(self, command: Iterable[str], **kwargs: Any) -> None:
        flat = [_normalize_command_key(str(c)) for c in command if c is not None and str(c).strip()]
        if flat == ["on"] or flat == ["power_on"]:
            await self.async_turn_on(**kwargs)
            return
        if flat == ["off"] or flat == ["power_off"]:
            await self.async_turn_off(**kwargs)
            return
        raise HomeAssistantError(
            "Protocol AC remote only supports on/off; use stips_iru1.send_ac_status for mode, fan, and temperature."
        )


class StipsIruRemote(RemoteEntity):
    """IR remote backed by STIPS catalog signals; send via local HTTP."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_icon = "mdi:remote"

    def __init__(
        self,
        *,
        hass: HomeAssistant,
        device_unique_name: str,
        device_name: str,
        device_ip: str,
        device_mac: str,
        device_online: bool,
        remote_id: str,
        friendly_name: str,
        remote_type: str,
        cmd_map: dict[str, tuple[str, int]],
        command_names: list[str],
    ) -> None:
        super().__init__()
        self.hass = hass
        if RemoteEntityFeature is not None:
            on_f = getattr(RemoteEntityFeature, "TURN_ON", 0)
            off_f = getattr(RemoteEntityFeature, "TURN_OFF", 0)
            send_f = getattr(RemoteEntityFeature, "SEND_COMMAND", 0)
            self._attr_supported_features = RemoteEntityFeature(on_f | off_f | send_f)
        self._device_unique_name = device_unique_name
        self._device_name = device_name
        self._device_ip = device_ip
        self._device_ip_live = ""
        self._device_mac = device_mac
        self._device_online = device_online
        self._remote_type = remote_type
        self._cmd_map = cmd_map
        self._command_names = command_names
        safe_rid = "".join(c if c.isalnum() or c in "-_" else "_" for c in remote_id)[:80]
        self._attr_unique_id = f"{DOMAIN}_{device_unique_name}_{safe_rid}"
        self._attr_name = friendly_name
        self._attr_icon = _icon_for_remote_type(remote_type)
        # IP comes from the last cloud sync; entity stays "on" so the UI works. Use
        # stips_iru1.refresh_catalog while hardware is online to fill ipAddress, or
        # send_command fails with a clear error until then.
        self._attr_available = True
        self._attr_extra_state_attributes = {
            "remote_type": remote_type,
            "device_unique_name": device_unique_name,
            "unique_name": device_unique_name,
            "device_ip": device_ip or None,
            "device_ip_backend": device_ip or None,
            "device_ip_live": None,
            "device_mac": device_mac or None,
            "device_online": device_online,
            "ip_configured": bool(device_ip),
            "command_count": len(command_names),
            "commands": command_names[:200],
        }

    @property
    def available(self) -> bool:
        return True

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._device_unique_name)},
            "name": self._device_name,
            "manufacturer": "STIPS",
            "model": "IRU1",
            "connections": {("mac", self._device_mac)} if self._device_mac else set(),
            "configuration_url": f"http://{self._device_unique_name}/device_info",
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Map UI 'on' to a likely power-on key (Broadlink-style remotes)."""
        # Prefer explicit learned keys before generic "power" (toggle) to avoid wrong codes.
        for label in ("power on", "power_on", "on", "power"):
            if _normalize_command_key(label) in self._cmd_map:
                await self.async_send_command([label], **kwargs)
                return
        raise HomeAssistantError(
            "No power/on button in this remote; use remote.send_command with a name from "
            "the commands attribute."
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Map UI 'off' to a likely power-off key."""
        # Reuse power_on when the remote only exposes a single power toggle.
        for label in ("power off", "off", "power on", "power_on", "on", "power"):
            if _normalize_command_key(label) in self._cmd_map:
                await self.async_send_command([label], **kwargs)
                return
        raise HomeAssistantError(
            "No power/off button in this remote; use remote.send_command with a name from "
            "the commands attribute."
        )

    async def async_send_command(self, command: Iterable[str], **kwargs: Any) -> None:
        """Send one or more IR codes (same style as Broadlink `remote.send_command`)."""
        hosts, live_ip = await async_build_control_hosts(
            self.hass,
            device_unique_name=self._device_unique_name,
            backend_ip=self._device_ip,
        )
        if not hosts:
            raise HomeAssistantError("Device host is missing; update catalog or network so the IRU reports a host")
        if live_ip:
            self._device_ip_live = live_ip
            self._attr_extra_state_attributes["device_ip_live"] = live_ip
            self._attr_extra_state_attributes["device_ip"] = live_ip

        num_repeats = max(1, int(kwargs.get("num_repeats", 1)))
        delay_secs = float(kwargs.get("delay_secs", 0.0))

        cmds = [c for c in command if c is not None and str(c).strip()]
        if not cmds:
            raise HomeAssistantError("No command provided")

        for i, raw_cmd in enumerate(cmds):
            key = _normalize_command_key(str(raw_cmd))
            if key not in self._cmd_map:
                preview = ", ".join(self._command_names[:12])
                if len(self._command_names) > 12:
                    preview += ", …"
                raise HomeAssistantError(
                    f"Unknown command {raw_cmd!r}. Known examples: {preview or '(none)'}"
                )
            signal, frequency = self._cmd_map[key]
            for _ in range(num_repeats):
                await self._post_signal(signal, frequency)
            if i < len(cmds) - 1 and delay_secs > 0:
                await asyncio.sleep(delay_secs)

    async def _post_signal(self, signal: str, frequency: int) -> None:
        hosts, live_ip = await async_build_control_hosts(
            self.hass,
            device_unique_name=self._device_unique_name,
            backend_ip=self._device_ip,
        )
        if not hosts:
            raise HomeAssistantError("Device host is missing; update catalog or network so the IRU reports a host")
        if live_ip:
            self._device_ip_live = live_ip
            self._attr_extra_state_attributes["device_ip_live"] = live_ip
            self._attr_extra_state_attributes["device_ip"] = live_ip

        session = async_get_clientsession(self.hass)
        params = {
            "signal": signal,
            "frequency": str(frequency),
            "remoteType": self._remote_type,
        }
        auth = aiohttp.BasicAuth(LOCAL_HTTP_USERNAME, LOCAL_HTTP_PASSWORD)
        timeout = aiohttp.ClientTimeout(total=2.5, connect=0.8, sock_connect=0.8, sock_read=1.5)
        last_error: Exception | None = None
        preferred_ip_failed = False
        for host in hosts:
            url = local_http_url(host, "/local-ir/send")
            try:
                async with session.post(url, data=params, auth=auth, timeout=timeout) as response:
                    if response.status >= 400:
                        body = await response.text()
                        last_error = HomeAssistantError(
                            f"Local IR request failed ({response.status}) via {host}: {body[:120]}"
                        )
                        continue
                    schedule_ip_recovery_after_mdns_success(
                        self.hass,
                        device_unique_name=self._device_unique_name,
                        successful_host=host,
                        preferred_ip=live_ip,
                        preferred_ip_failed=preferred_ip_failed,
                    )
                    return
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                if live_ip and host == live_ip:
                    preferred_ip_failed = True
                last_error = err
                continue
        if isinstance(last_error, HomeAssistantError):
            raise HomeAssistantError(f"{last_error} | hosts={', '.join(hosts)}")
        if last_error is not None:
            detail = str(last_error).strip() or type(last_error).__name__
            raise HomeAssistantError(
                f"Cannot reach IR device locally (hosts: {', '.join(hosts)}): {detail}"
            ) from last_error
        raise HomeAssistantError(f"Cannot reach IR device locally (hosts: {', '.join(hosts)})")
