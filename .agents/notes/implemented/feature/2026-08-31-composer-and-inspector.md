# Agent Note: Composer and orchestration interaction polish

Status: implemented

## Problem

The workbench opened with the orchestration inspector taking space from the
conversation even when no task was running. The composer also placed its
writing surface beside the tool buttons, which made the text area look
narrower than the available workspace. Native selects used inconsistent
widths and did not provide an intentional entry point for task configuration.
Slow model requests additionally kept the draft and disabled send state in
place until the server returned, while the scroll update targeted the outer
workspace instead of the actual message list.

## Decision

The workbench starts with the orchestration inspector collapsed. A labelled
header toggle opens it, and the same toggle becomes a compact right-side
overlay below the desktop two-column breakpoint. The toggle and inspector
expose synchronized `aria-expanded`/`aria-hidden` state.

The composer uses the full available content width and keeps the textarea on
its own row, with attachment, tool, and send actions aligned in a bottom
toolbar. The textarea remains borderless inside the unified shell and keeps a
bounded auto-growing height for long prompts.

Message submission clears and resizes the composer before starting the
network/model request, renders the user's message with an explicit “发送中…”
status, and changes the disabled send control to a quiet static stop-square
state. A failed request restores the draft when the user has not started a
newer one. The message list scrolls its own overflow container on every render,
including optimistic and completed messages, so the latest turn stays visible.

The conversation message stack now expands to a 1240px readability cap (or the
full available content width when narrower), with tighter 18px turn spacing and
smaller conversation insets. This reduces the unused side gutters and vertical
dead space without making the text span an unbounded desktop line.

All single-value selects use a shared full-width treatment with a clear
chevron, keyboard focus ring, and pointer affordance; multi-select controls
retain the native list treatment. Entering task mode opens the workflow picker
through the Chromium native picker API when available and falls back to focus
for older WebView runtimes.

## Alternatives considered

- Keep the inspector open and rely on users to close it: this preserves the
  old first viewport but hides the conversation's primary surface behind an
  unused panel.
- Keep the composer in a horizontal textarea/actions row: this is compact,
  but it permanently reserves the action column and does not match the
  requested chat-client writing experience.
- Replace every native select with a custom popover: this would increase
  keyboard, focus, and option-list maintenance without improving the existing
  data contract; the native picker is retained and styled instead.

## Consequences

The first viewport gives the conversation and composer priority, while task
flow remains one click away and still supports a full inspector on larger
screens. Compact windows gain an overlay rather than losing the flow view.
Task-mode configuration is faster to discover because the workflow list opens
on entry, but users may dismiss that native picker when they only want to
inspect the mode. Slow providers no longer trap the draft in the input or
make the page appear inert; the request still runs through the existing
server contract and reports provider errors with a retryable draft. The pending
control is visually stable, with a stop affordance instead of a distracting
spinner, while remaining disabled to prevent duplicate submissions. No API
payloads or workflow/session contracts change.

Verification covers JavaScript syntax, the full Python test suite, CSS diff
hygiene, and a fresh local browser layout check confirming the composer and
textarea use the available width and `#messageList` is the scroll container.
The PyInstaller desktop bundle is rebuilt and its ephemeral local server serves
the updated `pendingMessage`, scroll helper, static pending stop icon, and
expanded, tighter message-stack rules.
