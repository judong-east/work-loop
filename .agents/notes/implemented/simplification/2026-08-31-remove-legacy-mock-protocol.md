# Agent Note: Remove the legacy mock tool-call protocol

Status: implemented

## Problem

The live application is Workloop V2: model calls return one structured JSON
object, and implementation nodes publish complete files through the
`file_changes` contract. The demo mock server still described and emitted the
retired `tool_call` / `write_file` exchange, so it no longer represented the
runtime contract and could mislead local integration checks.

## Decision

The demo mock is aligned with the live V2 gateway contract. It removes the
unused tool-call helpers and returns direct structured JSON for the built-in
node types, including `file_changes` for implementation. The historical
`app/agents` runtime and compatibility API are already absent from the current
tree and are not reintroduced.

## Alternatives considered

- Delete the demo entirely: this would remove a useful dependency-free local
  endpoint for smoke testing.
- Keep the old tool-call behavior: this would preserve a protocol that the V2
  gateway does not consume and leave the repository internally inconsistent.
- Add a second compatibility adapter: this would increase runtime surface and
  violate the single V2 runtime boundary.

## Consequences

The demo can be used to exercise the current structured-output path without a
paid provider. The repository has one documented model-output contract and a
smaller mock implementation. Any external script depending on the retired
tool-call response shape must be updated, which is intentional because that
shape is not part of `/api/v2`.
