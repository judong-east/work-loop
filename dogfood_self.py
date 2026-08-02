"""Controlled dogfooding: let Workloop (driving real glm-5.2) edit Workloop's
own code, end-to-end, on a throwaway CLONE of the repo (the real repo's
working tree is never touched).

Self-task: append a note to _PLANNER_OUTPUT_INSTRUCTION saying project
validation is automatic and must not be listed as an implementation step.
Gate: the real PromptSchemaHintTest + plan_graph tests must still pass.

Does NOT auto-deliver: stops at READY_TO_DELIVER and prints the worktree diff
for a human to review/merge. Network is required (Pi -> glm).

Security: the glm key lives only in the Pi config dir's models.json; this
script never prints or commits it.
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

os.environ.setdefault("WORKLOOP_EXECUTION", "graph")

from app.agents.contracts import AgentTaskStatus, TaskBudget
from app.agents.pi_rpc import PiRpcProfile, PiRpcRuntime
from app.agents.workflow import AgentWorkflow
from app.validation.runner import DeterministicValidator, UnsafeDirectCommandSandbox

SRC_REPO = Path(r"D:\jd\code\work-loop")
PI_CONFIG_DIR = Path(r"C:\Users\23393\AppData\Local\Temp\pi-probe")
PI_BIN = shutil.which("pi")
UV = r"D:\python\Scripts\uv.exe"
GLM_MODEL, GLM_PROVIDER, THINKING = "glm-5.2", "glm", "medium"
VALIDATION_SUBSET = [
    "tests/test_agent_workflow.py::PromptSchemaHintTest",
    "tests/test_plan_graph_execution.py",
]
MARKER = "验证由系统在执行完成后自动运行"

VALIDATION_ARGV = [
    UV, "run", "--offline", "--with", "pytest", "--python", "3.11",
    "python", "-m", "pytest", *VALIDATION_SUBSET,
]


def banner(t: str) -> None:
    print("\n" + "=" * 72 + f"\n# {t}\n" + "=" * 72)


def run(cmd, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", **kw)


def build_clone(root: Path) -> Path:
    clone = root / "repository"
    banner("cloning work-loop -> throwaway clone (real repo untouched)")
    cp = run(["git", "clone", "--branch", "feature/plan-graph-execution",
              str(SRC_REPO), str(clone)])
    print(cp.stdout.strip()[-300:]); print(cp.stderr.strip()[-300:])
    assert (clone / "app" / "agents" / "workflow.py").is_file(), "clone missing workflow.py"
    # Confirm the schema-hint fix is present in the clone.
    wf = (clone / "app" / "agents" / "workflow.py").read_text(encoding="utf-8")
    assert "_PLANNER_OUTPUT_INSTRUCTION" in wf, "clone predates the fix"
    # Add the project policy (validation = real test subset) and commit it.
    policy = clone / ".workloop" / "project.toml"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text(
        "\n".join([
            "schema_version = 1",
            "",
            "[permissions]",
            'protected_paths = [".workloop/project.toml"]',
            'network = "deny"',
            "",
            "[validation]",
            "timeout_seconds = 120",
            "",
            "[[validation.commands]]",
            'name = "workloop_tests"',
            "argv = " + json.dumps(VALIDATION_ARGV),
            "",
        ]),
        encoding="utf-8",
    )
    run(["git", "-C", str(clone), "add", ".workloop/project.toml"])
    run(["git", "-C", str(clone), "commit", "-m", "chore: add workloop project policy for self-dogfooding"])
    print("clone HEAD:", run(["git", "-C", str(clone), "log", "--oneline", "-1"]).stdout.strip())
    return clone


def warm_and_baseline(clone: Path) -> bool:
    banner("baseline: run validation command in clone (warm uv cache, confirm green)")
    env = {k: os.environ[k] for k in
           ("COMSPEC", "PATH", "PATHEXT", "SYSTEMROOT", "TEMP", "TMP",
            "USERPROFILE", "WINDIR", "LANG") if k in os.environ}
    cp = subprocess.run(VALIDATION_ARGV, cwd=str(clone), env=env,
                        capture_output=True, text=True, encoding="utf-8", timeout=180)
    print((cp.stdout or cp.stderr).strip()[-500:])
    return cp.returncode == 0


def last_run(workflow, task_id) -> dict | None:
    d = workflow.store.task_dir(task_id) / "artifacts" / "runs"
    fs = sorted(d.glob("*.json")) if d.is_dir() else []
    if not fs:
        return None
    try:
        return json.loads(fs[-1].read_text(encoding="utf-8"))
    except Exception:
        return None


def main() -> int:
    banner(f"env: WORKLOOP_EXECUTION={os.environ.get('WORKLOOP_EXECUTION')!r}")
    if not PI_BIN:
        print("FATAL: pi not on PATH"); return 2
    print("pi bin:", PI_BIN, "| config dir:", PI_CONFIG_DIR)

    with tempfile.TemporaryDirectory(prefix="workloop-dogfood-") as tmp:
        root = Path(tmp)
        clone = build_clone(root)
        if not warm_and_baseline(clone):
            print(">> baseline validation FAILED in clone; aborting dogfood."); return 1

        profile = PiRpcProfile(
            command=[PI_BIN], model=GLM_MODEL, provider=GLM_PROVIDER,
            thinking=THINKING, config_dir=PI_CONFIG_DIR,
        )
        runtime = PiRpcRuntime(profile)
        validator = DeterministicValidator(sandbox=UnsafeDirectCommandSandbox())
        workflow = AgentWorkflow(root, runtime=runtime, validator=validator, max_iterations=2)
        project = workflow.register_project("workloop-self", clone, "feature/plan-graph-execution")
        print("registered project:", project.project_id, "on", clone)

        requirement = (
            "在 app/agents/workflow.py 中找到 _PLANNER_OUTPUT_INSTRUCTION 这个字符串常量，"
            f"在该常量文本的末尾追加一句：'{MARKER}，不要把运行验证命令列为 steps 中的实现"
            "步骤；steps 只应包含实际的编码或文件修改动作。' 这是唯一要做的一步编辑；不要改动"
            "其他任何常量、函数或逻辑，也不要运行任何测试或验证命令（项目验证 workloop_tests 由"
            "系统在执行完成后自动运行）。\n"
            "验收：\n"
            f"1. _PLANNER_OUTPUT_INSTRUCTION 的文本包含 {MARKER}\n"
            "2. workloop_tests 验证命令通过\n"
            "项目验证命令：workloop_tests"
        )
        task = workflow.create_task(
            "planner 提示：验证自动运行", requirement, project.project_id,
            budget=TaskBudget(max_iterations=2),
        )
        tid = task.task_id
        print("task:", tid)

        banner("STAGE 1: analyze (real glm planner)")
        t0 = time.monotonic()
        try:
            task = workflow.analyze(tid)
        except Exception:
            print(traceback.format_exc()); rec = last_run(workflow, tid)
            if rec: print("planner final_message:\n", str(rec.get("final_message", ""))[:800])
            return 1
        print(f"analyze elapsed: {time.monotonic()-t0:.1f}s | status: {task.status} | error: {task.error}")
        try:
            plan = workflow._load_plan(workflow.store.load(tid))
            print("PLAN steps:", plan.steps)
            print("PLAN acceptance:", plan.acceptance_criteria)
            print("PLAN required_tests:", plan.required_tests)
        except Exception as e:
            print("plan load failed:", e)
        if task.status != AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL:
            print(">> analyze did not reach WAITING_FOR_PLAN_APPROVAL."); return 1

        banner("STAGE 2: approve_plan (graph exec -> validate -> review)")
        t0 = time.monotonic()
        try:
            task = workflow.approve_plan(tid)
        except Exception:
            print(traceback.format_exc()); show_outcome(workflow, tid, clone); return 1
        print(f"approve_plan elapsed: {time.monotonic()-t0:.1f}s")
        print("FINAL status:", task.status, "| error:", task.error)
        show_outcome(workflow, tid, clone)
        banner("VERDICT")
        if task.status == AgentTaskStatus.READY_TO_DELIVER:
            print(">>> SELF-DOGFOOD reached READY_TO_DELIVER with real glm.")
            print(">>> NOT auto-delivered. Review the diff above; merge only if sane.")
            return 0
        print(f">>> SELF-DOGFOOD stopped at {task.status}: {task.error!r}")
        return 1


def show_outcome(workflow, tid, clone) -> None:
    task = workflow.store.load(tid)
    print("NODE_RUNS:", json.dumps(task.node_runs, ensure_ascii=False, indent=2))
    vpath = workflow._round_dir(task) / "validation.json"
    if vpath.is_file():
        v = json.loads(vpath.read_text(encoding="utf-8"))
        print("VALIDATION passed:", v.get("passed"), "| error:", v.get("error"))
        for c in v.get("checks", []):
            print("  check", c.get("name"), "exit", c.get("exit_code"), "| stderr:", str(c.get("stderr", ""))[:200])
    else:
        print("VALIDATION: (none)")
    rpath = workflow.store.task_dir(tid) / "artifacts" / "rounds" / str(task.iteration) / "review.json"
    if rpath.is_file():
        print("REVIEW:", json.dumps(json.loads(rpath.read_text(encoding="utf-8")), ensure_ascii=False))
    else:
        print("REVIEW: (none)")
    ws = Path(task.workspace)
    banner("worktree git state (staged vs unstaged?)")
    print("worktree:", ws)
    print("== git status --short ==")
    print(run(["git", "-C", str(ws), "status", "--short"]).stdout.strip() or "(clean)")
    print("== git diff --stat (unstaged) ==")
    print(run(["git", "-C", str(ws), "diff", "--stat"]).stdout.strip() or "(none)")
    print("== git diff --cached --stat (staged vs HEAD) ==")
    print(run(["git", "-C", str(ws), "diff", "--cached", "--stat"]).stdout.strip() or "(none)")
    banner("workloop changes.diff artifact (what review/delivery sees)")
    cdiff = workflow.store.task_dir(tid) / "artifacts" / "rounds" / str(task.iteration) / "changes.diff"
    if cdiff.is_file():
        cdtext = cdiff.read_text(encoding="utf-8")
        print("changes.diff length:", len(cdtext))
        print("changes.diff mentions workflow.py:", "workflow.py" in cdtext)
        print("changes.diff mentions marker:", MARKER in cdtext)
        print("--- changes.diff (first 2500 chars) ---")
        print(cdtext[:2500])
    else:
        print("changes.diff: (missing)")
    banner("edited _PLANNER_OUTPUT_INSTRUCTION in worktree")
    wf = ws / "app" / "agents" / "workflow.py"
    if wf.is_file():
        text = wf.read_text(encoding="utf-8")
        i = text.find("_PLANNER_OUTPUT_INSTRUCTION = (")
        if i >= 0:
            seg = text[i:i + 1400]
            print("contains marker:", MARKER in seg)
            print(seg)
        else:
            print("could not locate _PLANNER_OUTPUT_INSTRUCTION")
    rec = last_run(workflow, tid)
    if rec:
        print("\nlast run role:", rec.get("role"), "| final_message (first 600):")
        print(str(rec.get("final_message", ""))[:600])


if __name__ == "__main__":
    sys.exit(main())
