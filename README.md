# Workloop V2

Workloop is a local multi-model development workbench. It has one runtime, one
resource center, one project/session model, and one HTTP API (`/api/v2`).

## What it does

- groups multiple model aliases under each provider;
- supports OpenAI Chat Completions and Claude Messages;
- supports Bearer, `x-api-key`, custom-header, and no-auth providers;
- stores credentials outside provider/model catalog JSON;
- defines reusable node contracts and editable DAG workflows;
- binds every workflow node to an explicit model alias or capability default;
- persists project, session, context, node run, strategy, and Gate state;
- reads a configured local workspace into a bounded context snapshot;
- applies model-proposed file writes atomically inside that workspace;
- rejects absolute paths, traversal, symlink escape, protected paths, deletion,
  duplicate writes, and oversized change sets;
- runs explicit project validation commands without a command shell;
- blocks failed validation or review through a quality Gate;
- calls the same V2 model gateway for ordinary chat and task nodes.

## Start

```powershell
py -3.10 -m app.cli serve --root . --port 8765
```

Open `http://127.0.0.1:8765/`.

## First setup

1. Open **管理中心 → 模型**.
2. Add a provider, choose its protocol and authentication mode, then add one or
   more model aliases.
3. Create a project with an absolute workspace path, a default model, and
   optional validation commands. Each validation command is one JSON argv array:

```json
["py", "-3.10", "-m", "unittest", "discover", "-s", "tests", "-q"]
```

4. Use normal chat for direct model responses, or task mode for the selected
   DAG workflow.

## Default workflow

```text
requirement -> planning -> implementation -> testing -> review
```

The implementation model returns complete, structured file writes:

```json
{
  "changes": "Implement the requested behavior",
  "file_changes": [
    {
      "operation": "write",
      "path": "src/example.py",
      "content": "complete UTF-8 file content"
    }
  ],
  "artifacts": {},
  "decisions": []
}
```

Workloop validates and publishes these writes atomically. The testing node then
runs only the commands configured on the project. A failed command or non-pass
review blocks the session with a `quality_review` Gate.

## Architecture

```text
Browser
  -> /api/v2
     -> WorkbenchService
        -> ResourceCenter          provider / model / credential metadata
        -> JsonCollection          projects / sessions
        -> DagOrchestrator         order / retry / resume / Gate / context
           -> ModelGateway         OpenAI or Claude protocol
           -> WorkspaceRuntime     snapshot / atomic write / validation
```

The implementation lives in:

- `app/domain`: durable contracts, node catalog, workflow catalog, orchestration;
- `app/application`: use-case facade;
- `app/infrastructure`: JSON persistence, provider gateway, workspace runtime;
- `app/web`: the V2-only local API and workbench UI.

There is no compatibility `/api/agent` surface or second task/runtime model.

## API

Core read endpoints:

- `GET /api/v2/catalog`
- `GET /api/v2/strategies`
- `GET /api/v2/resources`
- `GET /api/v2/projects`
- `GET /api/v2/projects/{project_id}/workspace`
- `GET /api/v2/projects/{project_id}/sessions`
- `GET /api/v2/sessions/{session_id}`

Core write endpoints:

- `POST /api/v2/projects`
- `POST /api/v2/projects/{project_id}`
- `POST /api/v2/projects/{project_id}/sessions`
- `POST /api/v2/sessions/{session_id}/messages`
- `POST /api/v2/sessions/{session_id}/run`
- `POST /api/v2/sessions/{session_id}/policy`
- `POST /api/v2/sessions/{session_id}/policy/approve`
- `POST /api/v2/sessions/{session_id}/policy/replan`
- `POST /api/v2/resources/providers`
- `POST /api/v2/resources/models`
- `POST /api/v2/nodes`
- `POST /api/v2/workflows`

The server binds to `127.0.0.1` and rejects cross-origin writes. Provider keys
remain in local secret files and are never returned through the API.

## Tests

```powershell
py -3.10 -m compileall -q app tests
node --check app/web/static/workbench.js
py -3.10 -m unittest discover -s tests -v
git diff --check
```
