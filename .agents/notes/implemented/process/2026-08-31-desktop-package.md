# Agent Note: Package the V2 workbench as a desktop executable

Status: implemented

## Problem

The user needs to inspect the current Workloop V2 workbench as a desktop
application rather than starting Python manually.

## Decision

The existing `desktop.py` entry point is packaged with the repository's
PyInstaller `Workloop.spec` as a single Windows executable. The spec embeds the
static web assets and application icon, starts the local V2 HTTP server on an
ephemeral loopback port, and uses pywebview when available with the existing
browser fallback.

## Alternatives considered

- Package a separate browser-only launcher: this would duplicate the desktop
  entry point and lose the native window path already present in the project.
- Build a directory distribution: this is easier to inspect but less convenient
  for the requested quick visual check.
- Add a new runtime/configuration layer for packaging: this would duplicate
  server assembly and increase the desktop/runtime divergence.

## Consequences

`dist/Workloop.exe` is directly launchable on the current Windows host. Runtime
data remains under the normal per-user `.workloop` directory, while the
executable contains the V2 application and static UI. The executable is built
for the current Python/Windows environment and should be rebuilt when Python or
packaging dependencies change.

## Verification

- PyInstaller 6.20.0 completed successfully with `Workloop.spec`.
- The executable was launched and reported a responsive `Workloop 工作台`
  window.
- Its local server returned HTTP 200 for `/` and `/api/v2/catalog`.
