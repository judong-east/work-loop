# Agent Note: Low-chrome visual refinement

Status: implemented

## Problem

The first visual pass improved hierarchy and rounding, but the remaining blue
accent was still too saturated and repeated card/separator borders created
visual noise.

## Decision

Lower the light and dark theme accents to a neutral slate-blue range, soften
success/warning/error colours, and make surface hierarchy depend on background
tones and restrained shadows. Hard borders were removed from navigation,
panels, cards, flow connectors, tabs, and management sections. Inputs retain a
very light inset affordance and a visible focus ring so usability and keyboard
feedback are not lost.

## Alternatives considered

- Remove every boundary including input focus: this would reduce noise but
  would make keyboard focus and data-entry state ambiguous.
- Keep saturated status colours for emphasis: status meaning remains visible
  with muted colours and shape/background differences, while the calmer palette
  better suits a long-running desktop workbench.

## Consequences

The native window now presents a low-contrast, round, ChatGPT-like surface
without changing workflow or API behaviour. The same rules work in dark mode,
and the packaged executable was rebuilt and relaunched after the refinement.
