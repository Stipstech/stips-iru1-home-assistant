# STIPS IR Remote for Home Assistant

Custom Home Assistant integration for STIPS IRU1 infrared controllers.

## Features

- Production and staging STIPS API selection during setup.
- Production is selected by default.
- Local LAN control for low-latency IR commands.
- Editable per-device IP addresses during setup and later from integration options.
- Reactive IP recovery: no periodic mDNS polling.
- Saved IP is tried first. If it is unreachable but the IRU1 still works through mDNS/hostname, the integration reads `/device_info` and saves the new IP automatically when automatic IP recovery is enabled.
- Manual `stips_iru1.refresh_device_ips` action for troubleshooting.
- Catalog refresh action.
- Climate and remote entities.
- Reauthentication and reconfiguration flows.
- Downloadable diagnostics with sensitive fields redacted.

## API environments

- Production: `production.api.stips.visionalization.net`
- Staging: `stips.api.stagging.visionalization.net`

## Installation with HACS

### Add as a custom repository

1. Open HACS in Home Assistant.
2. Open the menu in the top-right corner and choose **Custom repositories**.
3. Add this GitHub repository URL.
4. Select **Integration** as the category.
5. Install **STIPS IR Remote**.
6. Restart Home Assistant.
7. Go to **Settings → Devices & services → Add integration**.
8. Search for **STIPS IR Remote** and complete setup.

## Manual installation

Copy:

```text
custom_components/stips_iru1/
```

into:

```text
/config/custom_components/stips_iru1/
```

Restart Home Assistant, then add **STIPS IR Remote** from **Settings → Devices & services**.

## IP handling

The integration stores a configurable IP for each IRU1. Normal commands first use the saved IP. It does not run an IP discovery check every 60 seconds.

When automatic IP recovery is enabled and a command cannot reach the saved IP, the integration falls back to the device hostname/mDNS name. If the command succeeds through that fallback, the integration then reads the device's `/device_info` endpoint and saves a changed `ip_address` for future commands.

This keeps normal control fast while still recovering automatically after DHCP changes.

## Services / actions

### `stips_iru1.refresh_catalog`

Re-download the STIPS account catalog and reload the integration. Manual/saved local IP overrides are preserved.

### `stips_iru1.refresh_device_ips`

Manually query IRU1 devices over mDNS/hostname and update changed IP addresses. This is intended for troubleshooting; automatic recovery is reactive rather than periodic.

### `stips_iru1.send_ac_status`

Send an AC status payload locally to a configured IRU1.

## Diagnostics

Open **Settings → Devices & services → STIPS IR Remote**, open the integration menu and download diagnostics when troubleshooting. Review diagnostics before sharing them publicly.

## Development / validation

This repository includes GitHub Actions for:

- HACS validation
- Home Assistant hassfest validation

## GitHub repository

This package is configured for the GitHub account **Stipstech** and repository **stips-iru1-home-assistant**.

Repository URL: `https://github.com/Stipstech/stips-iru1-home-assistant`

## Security note

The current integration source contains fixed credentials used by the existing STIPS/mobile/local-device implementation. Before making the repository public, confirm with the STIPS backend/device owner that these values are intentionally distributable defaults/public-client credentials. Do not publish private production secrets.
