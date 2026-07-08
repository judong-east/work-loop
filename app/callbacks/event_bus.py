from __future__ import annotations

from app.core.artifact_store import ArtifactStore
from app.core.contracts import CallbackEvent, TaskState


class EventBus:
    def __init__(self, store: ArtifactStore):
        self.store = store

    def publish(self, task: TaskState, event_type: str, payload: dict) -> CallbackEvent:
        event = CallbackEvent(task_id=task.task_id, event_type=event_type, payload=payload)
        event_ref = f"callbacks/{event.event_id}.json"
        self.store.write_json(self.store.task_dir(task.task_id) / event_ref, event)
        task.events.append(event_ref)
        self.store.append_audit(task.task_id, "event", {"event": event})
        return event

