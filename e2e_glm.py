"""End-to-end: drive the REAL AgentWorkflow with a REAL PiRpcRuntime(glm-5.2)
through a tiny coding task under graph execution.

NOT committed. Sets up a throwaway git repo + project policy, then runs:
  create_task -> analyze (real glm planner)
              -> approve_plan (graph exec: real glm executor w/ tools
                               -> deterministic validation (check_result)
                               -> real glm reviewer)
and prints the honest outcome at every stage.

Security: the glm API key lives only in the Pi config dir's models.json
(never printed, never committed). This script never touches the key.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

# Graph execution must be visible to approve_plan (it reads os.environ).
os.environ.setdefault("WORKLOOP_EXECUTION", "graph")

from app.agents.contracts import AgentTaskStatus, ExecutionPlan, TaskBudget
from app.agents.pi_rpc import PiRpcProfile, PiRpcRuntime
from app.agents.workflow import AgentWorkflow
from app.validation.runner import DeterministicValidator, UnsafeDirectCommandSandbox

# Windows-native path to the Pi config dir holding models.json (glm provider).
PI_CONFIG_DIR = Path(r"C:\Users\23393\AppData\Local\Temp\pi-probe")
PI_BIN = shutil.which("pi")  # resolves the pi.CMD shim on Windows
GLM_MODEL = "glm-5.2"
GLM_PROVIDER = "glm"
THINKING = "medium"


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(f"# {title}")
    print("=" * 72)


def run_git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )


def build_repo(root: Path) -> Path:
    repo = root / "repository"
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    run_git(repo, "config", "user.email", "workloop@example.test")
    run_git(repo, "config", "user.name", "Workloop E2E")
    run_git(repo, "config", "commit.gpgsign", "false")
    (repo / "app.txt").write_text("main\n", encoding="utf-8")

    # Validation command: assert result.txt exists and content == 'done'.
    check_cmd = (
        "import sys; "
        "data=open('result.txt',encoding='utf-8').read().strip(); "
        "sys.exit(0 if data=='done' else 1)"
    )
    policy = repo / ".workloop" / "project.toml"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text(
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[permissions]",
                'protected_paths = [".workloop/project.toml"]',
                'network = "deny"',
                "",
                "[validation]",
                "timeout_seconds = 60",
                "",
                "[[validation.commands]]",
                'name = "check_result"',
                f"argv = [{json.dumps(sys.executable)}, \"-c\", {json.dumps(check_cmd)}]",
                "",
            ]
        ),
        encoding="utf-8",
    )
    run_git(repo, "add", "app.txt", ".workloop/project.toml")
    run_git(repo, "commit", "-m", "initial")
    return repo


def last_run_record(workflow: AgentWorkflow, task_id: str) -> dict | None:
    """Load the most recent artifacts/runs/*.json for diagnosis."""
    runs_dir = workflow.store.task_dir(task_id) / "artifacts" / "runs"
    if not runs_dir.is_dir():
        return None
    files = sorted(runs_dir.glob("*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except Exception:
        return None


def show_plan(task_id: str, workflow: AgentWorkflow) -> None:
    try:
        plan = workflow._load_plan(workflow.store.load(task_id))
        print("PLAN:")
        print("  requirement_understanding:", plan.requirement_understanding)
        print("  steps:", plan.steps)
        print("  acceptance_criteria:", plan.acceptance_criteria)
        print("  required_tests:", plan.required_tests)
    except Exception as exc:  # noqa: BLE001
        print("could not load plan:", exc)


def show_validation(task_id: str, workflow: AgentWorkflow) -> None:
    vpath = workflow._round_dir(workflow.store.load(task_id)) / "validation.json"
    if vpath.is_file():
        print("VALIDATION:", json.dumps(json.loads(vpath.read_text(encoding="utf-8")), ensure_ascii=False))
    else:
        print("VALIDATION: (none)")


def show_review(task_id: str, workflow: AgentWorkflow) -> None:
    rpath = (
        workflow.store.task_dir(task_id) / "artifacts" / "rounds"
        / str(workflow.store.load(task_id).iteration) / "review.json"
    )
    if rpath.is_file():
        print("REVIEW:", json.dumps(json.loads(rpath.read_text(encoding="utf-8")), ensure_ascii=False))
    else:
        print("REVIEW: (none)")


def show_node_runs(task_id: str, workflow: AgentWorkflow) -> None:
    task = workflow.store.load(task_id)
    print("NODE_RUNS:", json.dumps(task.node_runs, ensure_ascii=False, indent=2))


def main() -> int:
    banner(f"env check: WORKLOOP_EXECUTION={os.environ.get('WORKLOOP_EXECUTION')!r}")
    if not PI_BIN:
        print("FATAL: pi not on PATH (shutil.which returned None).")
        return 2
    print("pi bin:", PI_BIN)
    if not PI_CONFIG_DIR.is_dir() or not (PI_CONFIG_DIR / "models.json").is_file():
        print(f"FATAL: Pi config dir missing models.json: {PI_CONFIG_DIR}")
        return 2
    print("pi config dir:", PI_CONFIG_DIR)

    with tempfile.TemporaryDirectory(prefix="workloop-e2e-") as tmp:
        root = Path(tmp)
        repo = build_repo(root)
        print("repo:", repo)

        profile = PiRpcProfile(
            command=[PI_BIN],
            model=GLM_MODEL,
            provider=GLM_PROVIDER,
            thinking=THINKING,
            config_dir=PI_CONFIG_DIR,
        )
        runtime = PiRpcRuntime(profile)
        validator = DeterministicValidator(sandbox=UnsafeDirectCommandSandbox())
        workflow = AgentWorkflow(root, runtime=runtime, validator=validator, max_iterations=2)
        project = workflow.register_project("e2e-proj", repo, "main")
        print("registered project:", project.project_id)

        requirement = (
            "创建 result.txt 文件，文件内容为 done。\n"
            "验收：\n"
            "1. result.txt 存在\n"
            "2. result.txt 内容为 done\n"
            "项目验证命令：check_result"
        )
        task = workflow.create_task(
            "生成结果文件", requirement, project.project_id,
            budget=TaskBudget(max_iterations=2),
        )
        task_id = task.task_id
        print("task:", task_id)

        # ---- STAGE 1: analyze (real glm planner) ----
        banner("STAGE 1: analyze (real glm planner)")
        t0 = time.monotonic()
        try:
            task = workflow.analyze(task_id)
        except Exception:
            print("analyze RAISED:\n", traceback.format_exc())
            rec = last_run_record(workflow, task_id)
            if rec:
                print("last run final_message:\n", rec.get("final_message", ""))
            return 1
        print("analyze elapsed: %.1fs" % (time.monotonic() - t0))
        print("status:", task.status)
        print("error:", task.error)
        show_plan(task_id, workflow)
        rec = last_run_record(workflow, task_id)
        if rec:
            print("planner model:", rec.get("model"))
            print("planner final_message (first 800 chars):")
            print(str(rec.get("final_message", ""))[:800])
        if task.status != AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL:
            print(">> analyze did NOT reach WAITING_FOR_PLAN_APPROVAL. Stopping.")
            return 1

        # ---- STAGE 2: approve_plan (graph exec + validate + review) ----
        banner("STAGE 2: approve_plan (graph exec -> validate -> review)")
        t0 = time.monotonic()
        try:
            task = workflow.approve_plan(task_id)
        except Exception:
            print("approve_plan RAISED:\n", traceback.format_exc())
            rec = last_run_record(workflow, task_id)
            if rec:
                print("last run final_message:\n", rec.get("final_message", ""))
            show_node_runs(task_id, workflow)
            return 1
        print("approve_plan elapsed: %.1fs" % (time.monotonic() - t0))
        print("FINAL status:", task.status)
        print("FINAL error:", task.error)

        show_node_runs(task_id, workflow)
        show_validation(task_id, workflow)
        show_review(task_id, workflow)

        # Did the executor actually write the file?
        ws = Path(task.workspace)
        result_path = ws / "result.txt"
        banner("workspace artifact check")
        print("workspace:", ws)
        print("result.txt exists:", result_path.is_file())
        if result_path.is_file():
            print("result.txt content:", repr(result_path.read_text(encoding="utf-8")))

        rec = last_run_record(workflow, task_id)
        if rec:
            print("last run role:", rec.get("role"), "model:", rec.get("model"))
            print("last run final_message (first 800 chars):")
            print(str(rec.get("final_message", ""))[:800])

        banner("VERDICT")
        if task.status == AgentTaskStatus.READY_TO_DELIVER:
            print(">>> END-TO-END SUCCESS: task reached READY_TO_DELIVER with real glm.")
            return 0
        print(f">>> END-TO-END STOPPED at status={task.status} error={task.error!r}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
