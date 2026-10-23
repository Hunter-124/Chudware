# Security Policy

## Reporting a vulnerability

Please report security issues privately through GitHub private vulnerability reporting:
open <https://github.com/Hunter-124/Chudware/security> and choose **Report a vulnerability**
(direct link: <https://github.com/Hunter-124/Chudware/security/advisories/new>).

Do not open a public issue, pull request, or discussion for a suspected vulnerability.

A useful report includes:

- the affected component (desktop app, `chudware-hostd`, MCP server, updater, installer,
  browser web app, or the chudware.com website) and the version or commit;
- the platform (Linux, Windows, macOS, browser);
- reproduction steps or a proof of concept, and the impact you observed;
- any suggested fix or mitigation.

You should receive an acknowledgement within a few days. Fixes are developed in a private
security advisory, released, and then disclosed in the advisory with credit to the reporter
unless you ask to remain anonymous.

## Supported versions

Security fixes are made on the default branch and shipped in the next release. Only the
latest published release is supported; the in-app updater installs it.

## Scope

In scope:

- the native desktop application (`host/`), the engine library and daemon (`core/`,
  `chudware-hostd`), and the Python services they run (`services/`), including the MCP
  server and the agent tool surface;
- the auto-updater and release artifacts (signed update manifests, installers, packages);
- the experimental self-hosted browser app and its deployment scripts (`web/`);
- the promotional website source (`website/`).

Out of scope:

- vulnerabilities in third-party dependencies that are already fixed upstream (report them
  upstream; a pull request that bumps the pin is welcome);
- the third-party model providers an agent session talks to;
- findings that require an already-compromised local account or physical access.

## Handling secrets

Never include credentials, API keys, or personal data in a report attachment, an issue, or a
pull request. Configuration templates live in `config.example.json`; real configuration and
`.env` files are ignored by git.
