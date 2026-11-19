# Contributing

Speakeasy has two parts: the Hermes plugin (`plugin/`, Python) and the Mac app (`mac/`, Swift).
All tests run offline: a fake Hermes API server, a fake `codex app-server` and a fake live voice
session stand in for the real services, so nothing needs network access, an account or credits.

## Plugin

The plugin runs inside Hermes, so its tests use the Python environment that ships with Hermes
Agent (usually `~/.hermes/hermes-agent/venv`).

    cd plugin
    ~/.hermes/hermes-agent/venv/bin/python -m pytest tests -q   # unit and integration tests
    python3 tests/e2e_local.py                                  # end-to-end: real server, fake services

The end-to-end check also writes the JSON contract fixtures the Mac tests read
(`mac/Tests/SpeakeasyCoreTests/Contract/`) when `SPEAKEASY_CONTRACT_DIR` points there.

## Mac app

Needs Xcode 16 or later and macOS 14+.

    cd mac
    swift test                                           # logic and API contract tests; SpeakeasyAppTests links the app and draws its views in windows it never shows
    swift build && .build/debug/Speakeasy --panel-smoke  # renders the call panel, checks layout and clicks
    scripts/package-app.sh                               # builds dist/Speakeasy.app (ad-hoc signed)

## Before pushing

    scripts/scan-secrets.sh

It fails on anything that looks like a key, token or private address. Keep it passing.

## Releasing (maintainers)

    mac/scripts/release.sh 0.2.1 --publish

Builds, signs with a Developer ID certificate, notarizes with Apple, staples, and publishes a
GitHub release with the `.dmg`. The app's Check for Updates reads that release. Needs a
"Developer ID Application" certificate in the keychain and notarization credentials saved with
`xcrun notarytool store-credentials speakeasy`.
