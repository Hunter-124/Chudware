# Chudware for macOS — release notes

## Platform differences

This macOS build does not include Chudware's embedded CEF browser. Browser-based vendor sign-in opens in the system browser instead; there is no in-app browser tab or in-app vendor sign-in on macOS.

On macOS, the Parts Library fetches vendor pages through Playwright instead of Chudware's CEF daemon. Vendors that fingerprint browser automation can refuse those requests. When that happens, Chudware returns "blocked by the vendor" instead of hanging; the local catalog and vendors that tolerate Playwright continue to work. This is a known platform difference, not a bug. Linux and Windows Parts Library fetching is unaffected.

If a vendor blocks a search on macOS, use the local catalog or run the vendor search from Chudware on a Linux or Windows machine if you have one.

Troubleshooting and current vendor-access guidance: <https://chudware.com/docs/troubleshooting/#macos-parts-library-vendor-access>

## Gatekeeper notice

Current macOS builds are unsigned until Developer ID and notarization credentials are configured. Gatekeeper will warn or block first launch. Download only from the project's GitHub Releases page (<https://github.com/Hunter-124/Chudware/releases>) and verify the published SHA-256 and Ed25519 signature. The packaging pipeline already supports signing and notarization; once credentials are configured this notice will be updated.
