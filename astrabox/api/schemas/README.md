A2A schema provenance
=====================

`a2a.json` is the official, non-normative JSON Schema bundle generated from
A2A v1.0.0 (commit 1736957) protobuf definitions and published at
https://a2a-protocol.org/v1.0.0/spec/a2a.json. Retrieved 2026-10-20.
Its Apache-2.0 license is preserved in LICENSE.a2a.

The runtime uses a2a-sdk 1.2.2 and negotiates A2A 1.0. The native protobuf
contract remains authoritative. `native_documentation.py` only rebases the
bundle's file references into OpenAPI components; it does not translate
protocol fields or parse requests. Re-audit the bundle when the negotiated
protocol changes.
