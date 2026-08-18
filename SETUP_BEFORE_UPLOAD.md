# One-time GitHub setup

The repository files are arranged for HACS under `custom_components/stips_iru1/`.

This package is already configured for:

- GitHub owner: `Stipstech`
- Repository: `stips-iru1-home-assistant`
- Repository URL: `https://github.com/Stipstech/stips-iru1-home-assistant`

Before the first public upload:

1. Create a **public** repository named `stips-iru1-home-assistant` under the `Stipstech` account.
2. Upload the **contents** of this folder to the repository root.
3. Confirm that GitHub **Issues** are enabled.
4. Add a short repository description, for example: `Home Assistant integration for STIPS IRU1 infrared controllers with local LAN control and reactive mDNS IP recovery.`
5. Add repository topics such as: `home-assistant`, `hacs`, `stips`, `iru1`, `infrared`.
6. Check the security note in `README.md` before making the repository public, because the current integration source contains fixed credentials from the existing STIPS implementation.

After upload, wait for both GitHub Actions to pass. Then create a GitHub Release such as `v0.4.0` if you want release-based HACS versions.
