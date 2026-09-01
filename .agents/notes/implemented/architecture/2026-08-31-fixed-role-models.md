# Agent Note: Fix one model binding per role

Status: implemented

## Problem

The collaboration layer allowed a role to omit `model_alias`, then silently
fall back to a project or capability default. Tasks could also override the
role model with their own alias. That makes a role an unstable routing
identity and does not satisfy the requirement that every role is fixed to one
model.

## Decision

Every persisted role must have one valid, enabled model alias. The
collaboration service creates built-in roles only when a model is available,
assigning the capability-selected alias at creation time. Existing malformed
or unbound role records are ignored by typed loading and can be replaced by a
bound record. Task execution always uses the role's model; task-level model
overrides are removed from the UI and rejected by the service. The model
alias remains readable in task records only as a legacy field so old JSON can
be loaded without a destructive migration.

## Alternatives considered

- Keep project/capability fallback: this preserves availability but violates
  the fixed-role routing contract.
- Require a model for roles but keep task overrides: this still lets one role
  execute on different models and makes routing depend on task payloads.
- Create placeholder model aliases before setup: this would produce invalid
  resources and hide the real configuration prerequisite.
- Delete all old role/task JSON: this risks data loss; typed loading and
  replacement provide a recoverable migration path instead.

## Consequences

Role configuration is deterministic and auditable: a task's effective model is
always visible on its role. A fresh installation with no models temporarily
has no default roles; adding the first enabled model lazily materializes the
built-in role set. Existing clients that send task-level `model_alias` values
must move that binding to the role configuration. Projects and ordinary
workflows may still use their own node/project model selection rules.
