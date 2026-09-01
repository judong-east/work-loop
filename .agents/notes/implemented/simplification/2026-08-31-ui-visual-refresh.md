# Agent Note: Workbench visual refresh

Status: implemented

## Problem

The desktop workbench rendered correctly but felt like a dense engineering
console: the green/teal gradients competed with the content, the Microsoft
YaHei-first stack made Chinese text look heavy and cramped, controls were too
small, and panels relied on
hard borders and uneven spacing. The pywebview/Tauri-like shell was not the
root cause; the page needed a clearer product-level visual system.

## Decision

Keep the existing HTML structure and API contracts, and apply a single visual
system in `workbench.css`: use Segoe UI Variable for Latin text and DengXian
(等线) as the first Simplified Chinese fallback, with Microsoft YaHei retained
as a safe fallback. The refined direction is a fresh, low-saturation blue-gray
workbench: cool white surfaces, a muted steel-blue action colour, restrained
soft shadows, and no decorative hard lines. Spacing follows an 8px rhythm. The
earlier amber, saturated teal, and dark mist-green variants were rejected during
review because their contrast felt too abrupt. The current composition is a
modern studio workbench: a quiet charcoal navigation rail anchors the left
edge, the main work surface is a single neutral canvas, and steel blue is
reserved for actions and state. The earlier nested-card treatment was removed
in favour of one clear navigation/workspace/inspector hierarchy. The welcome
copy and starter actions were simplified to make the empty state feel
purposeful. The composer is one unified rounded editor with context chips, a
large multi-line writing area, and a single primary send action. The
orchestration inspector uses a lightweight status surface and larger node
cards, and can open a modal full-page flow canvas through its expand button or
a double-click. The desktop window background and favicon are aligned with
the same low-saturation blue-gray system.

## Alternatives considered

- Rewrite the UI in a new framework or replace pywebview with Tauri: this
  would increase packaging and regression risk without addressing the visual
  hierarchy problem.
- Keep the earlier low-saturation light-indigo palette as the default: the
  previous pass still looked like a stack of floating cards, so the final pass
  rebalances the page around a single canvas, removes decorative gradients,
  and uses the dark rail only as a navigation anchor.
- Keep the C amber accent: the user explicitly rejected the yellow pairing.
- Use a high-saturation WorkBuddy-like teal: the reference is useful for its
  single-accent hierarchy, generous whitespace, and soft rounded surfaces, but
  the selected production palette is the first blue-and-white direction.
- Change only the colour palette: this would leave the small type, uneven
  spacing, and hard-edged controls that caused the complaint.
- Capture screenshots through the browser-control plugin: the plugin runtime
  was unavailable in this environment, so validation used the launched native
  window and a local screen capture instead.

## Consequences

The existing API, interaction modes, management tabs, and fixed-role model
behaviour are unchanged. A fresh desktop profile opens in the selected
low-saturation light mode; the theme toggle still allows switching to the
coordinated navy dark mode. The theme preference uses a new palette-specific
storage key so an older dark preference cannot hide the selected first version
on its first launch. The enlarged
composer gives more writing room, and the flow dialog provides a dedicated
canvas for node inspection. The stylesheet retains the original rules for
compatibility and layers the final visual tokens/components at the end, making
a future token consolidation a separate, low-risk cleanup. The favicon and
desktop window background use the same neutral blue-gray palette. The font
change is system-only and adds no downloaded asset or runtime dependency; code
and diagnostic values continue to use the existing monospace stack.
