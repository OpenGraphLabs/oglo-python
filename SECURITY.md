# Security policy

## Supported versions

| Version | Security updates |
| --- | --- |
| 0.1.x with firmware 0.9.10/schema 6 | Yes |
| development snapshots and older versions | No |

Historical 0.9.9 wire captures are retained for decoder regression tests; they do
not make a live 0.9.9 glove a supported deployment.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for the sole canonical repository:

<https://github.com/OpenGraphLabs/oglo-python/security/advisories/new>

Do not include exploit details, credentials, customer data, device recordings, or
unredacted serial numbers in a public issue. Include the affected SDK version,
firmware version, transport, host OS, reproduction steps, and expected impact in the
private report.

Maintainers will acknowledge a report as soon as practical, investigate it privately,
and coordinate disclosure after a fix or mitigation is available.
