# ADR 0001: Run dashboard locally, not on Railway

**Status:** Accepted  
**Date:** 2026-05-28

## Context

This dashboard depends on AnkiConnect — an add-on that runs inside the Anki desktop app and exposes a local HTTP API on `localhost:8765`. Every Anki-related endpoint (`/api/stats`, `/api/today`, `/api/total-time`, `/sync`, `/api/decks`) calls this service directly.

Railway was evaluated as a hosting option. It runs containerized workloads and does not provide a persistent VM.

## Decision

Run the dashboard locally on the same machine as Anki.

## Reasons

**Simplicity.** A local Flask server started with one command requires no infrastructure to manage, no deployment pipeline, and no ongoing cost.

**AnkiConnect is incompatible with Railway without significant complexity.** Anki is a Qt desktop application. Running it headlessly on Railway would require:
- A custom Dockerfile that installs Anki's full Qt dependency stack and `Xvfb`
- A process supervisor (`supervisord` or equivalent) to run `Xvfb`, `anki`, and `flask` in one container
- A Railway volume to persist Anki's profile across deploys
- Injected AnkiWeb credentials for the server-side Anki instance to authenticate

This complexity isn't justified for a personal-use dashboard that already has Anki running locally.

## Consequences

- The dashboard must be started manually (or via a login item/launchd plist) on the local machine
- Anki must be running with AnkiConnect active for Anki features to work
- No remote access unless accessed over a local network or VPN
