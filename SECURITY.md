# Security Policy

## Supported versions

Only the **latest release** receives security fixes. Update via HACS before
reporting an issue you can no longer reproduce on the current version.

| Version | Supported |
| --- | --- |
| latest release | ✅ |
| older releases | ❌ |

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Use GitHub's private vulnerability reporting instead:
**[Report a vulnerability](https://github.com/sNNNeaker/HA-Copper/security/advisories/new)**
(Repository → Security → "Report a vulnerability").

You can expect an initial response within a week. This is a hobby project
maintained in spare time — critical token-handling issues will be prioritised.

### What counts as a vulnerability here

This integration handles an Auth0 refresh token for your Copper account, so
reports are especially welcome for:

- Token or sign-in-code leakage (logs, diagnostics, error messages, redirects)
- Flaws in the OAuth/PKCE login flow (e.g. redirect handling, state validation)
- Anything that lets another integration/user read the stored credentials
  beyond what Home Assistant's own storage model already implies

### Out of scope

- **Copper Labs' servers, apps, or API** — this is an *unofficial* integration
  with no affiliation to Copper Labs. Vulnerabilities in their infrastructure
  must be reported to Copper Labs directly, not here, and this project does
  not authorise or encourage testing their systems.
- Home Assistant core or HACS themselves (report to those projects).
- The inherent design of Home Assistant's credential storage
  (`.storage/` is plaintext by HA's design; host security is the boundary).
- Reports requiring an already-compromised Home Assistant host.

## Handling of your report

Confirmed vulnerabilities are fixed in a new release as quickly as possible;
the advisory is published after the fix is available. Please leave reasonable
time for a fix before any public disclosure.