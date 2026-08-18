# Changelog

## 0.4.0

- Changed automatic IP handling from periodic polling to reactive recovery.
- Saved IP is attempted first for local control.
- When the saved IP fails but mDNS/hostname control succeeds, `/device_info` is queried and a changed IP is saved automatically.
- Added manual device IP refresh action for troubleshooting.
- Added reauthentication support.
- Added diagnostics support.
- Added password masking in configuration flows.
- Improved reconfigure behavior and IPv6 local URL handling.
- Registered integration actions at integration setup level.
