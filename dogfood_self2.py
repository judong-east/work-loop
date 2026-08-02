"""Clean self-edit re-verification (复证) with a BRAND-NEW target + isolation tripwires.

Why a new target: the previous self-dogfood aimed at `_PLANNER_OUTPUT_INSTRUCTION`,
whose marker text is now already present in the real repo — so re-running it proves
nothing (the clone starts already-satisfied). This run picks a sentinel that exists
nowhere in the repo, so "did the edit land, and where" is unambiguous.

What it verifies
  1. Functional: Workloop (graph execution + real glm via Pi) plans, edits, validates
     and reviews its own code on a throwaway CLONE, reaching READY_TO_DELIVER.
  2. Isolation: the real repo D:\\jd\\code\\work-loop is byte-identical before/after
     (content fingerprint + git HEAD/status/branches/worktrees), the sentinel never
     appears in the real repo, and every AgentRequest.workspace stays under the
     temp root.
  3. Leak surface: reports what the Pi config dir gained (session state keyed by cwd
     lives outside every task worktree).

Does NOT deliver and does NOT touch the real working tree. Network required (Pi -> glm).
Security: the glm key lives only in the Pi config dir's models.json; never printed.
"""
from __future__ import annotations

import hashlib
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
# PiRpcRuntime now refuses a network-denied write request unless the operator
# opts in, because it cannot enforce that policy at the OS level. This harness
# exists precisely to measure that gap, so it opts in deliberately — and then
# checks with its own tripwires whether anything escaped.
os.environ.setdefault("WORKLOOP_ALLOW_UNSANDBOXED_EXECUTOR", "1")

from app.agents.contracts import AgentRequest, AgentResult, AgentTaskStatus, TaskBudget
from app.agents.pi_rpc import PiRpcProfile, PiRpcRuntime
from app.agents.workflow import AgentWorkflow
from app.validation.runner import DeterministicValidator, UnsafeDirectCommandSandbox

SRC_REPO = Path(r"D:\jd\code\work-loop")
PI_CONFIG_DIR = Path(r"C:\Users\23393\AppData\Local\Temp\pi-probe")
PI_BIN = shutil.which("pi")
UV = r"D:\python\Scripts\uv.exe"
BRANCH = "feature/plan-graph-execution"

# Provider/model are env-overridable so the same harness can re-verify against
# whichever backend is alive. Defaults target DeepSeek; configure the provider
# first with `python configure_pi_provider.py` (it resolves the exact model id
# from the endpoint's /models list and writes the Pi config dir's models.json).
PROVIDER = os.environ.get("WORKLOOP_DOGFOOD_PROVIDER", "deepseek").strip()
MODEL = os.environ.get("WORKLOOP_DOGFOOD_MODEL", "DeepSeek-V4-Flash").strip()
THINKING = os.environ.get("WORKLOOP_DOGFOOD_THINKING", "medium").strip() or "medium"

VALIDATION_SUBSET = [
    "tests/test_agent_workflow.py::PromptSchemaHintTest",
    "tests/test_plan_graph_execution.py",
]
VALIDATION_ARGV = [
    UV, "run", "--offline", "--with", "pytest", "--python", "3.11",
    "python", "-m", "pytest", *VALIDATION_SUBSET,
]

# Brand-new target: a constant the previous dogfood never touched, and a sentinel
# token that appears nowhere in the repository (verified before the run).
TARGET_CONST = "_REVIEWER_OUTPUT_INSTRUCTION"
SENTINEL = "WLDOGFOOD2"
NEW_SENTENCE = (
    f"审查者只读：不得修改任何文件，只能阅读、比对与判断。[{SENTINEL}]"
)

# Directories whose churn is not evidence of a leak (build/tooling caches).
FINGERPRINT_SKIP = {
    ".git", ".uv-cache", ".uv-python", ".openai-docs-cache",
    "__pycache__", ".pytest_cache",
}


def banner(t: str) -> None:
    print("\n" + "=" * 72 + f"\n# {t}\n" + "=" * 72)


def run(cmd, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", **kw)


# ---------------------------------------------------------------- tripwires

def fingerprint(root: Path) -> dict[str, str]:
    """sha256 of every file under ``root``, skipping tooling caches."""
    out: dict[str, str] = {}
    for path in root.rglob("*"):
        if any(part in FINGERPRINT_SKIP for part in path.relative_to(root).parts):
            continue
        if not path.is_file() or path.is_symlink():
            continue
        try:
            out[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
        except OSError as error:
            out[path.relative_to(root).as_posix()] = f"unreadable:{type(error).__name__}"
    return out


def git_state(repo: Path) -> dict[str, str]:
    def g(*args: str) -> str:
        return run(["git", "-C", str(repo), *args]).stdout.strip()

    return {
        "HEAD": g("rev-parse", "HEAD"),
        "branch": g("branch", "--show-current"),
        "status": g("status", "--porcelain", "--untracked-files=all"),
        "branches": g("branch", "--list", "--format=%(refname:short)"),
        "worktrees": g("worktree", "list", "--porcelain"),
    }


def diff_fingerprints(before: dict[str, str], after: dict[str, str]) -> dict[str, list[str]]:
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    modified = sorted(p for p in set(before) & set(after) if before[p] != after[p])
    return {"added": added, "removed": removed, "modified": modified}


def listing(root: Path) -> set[str]:
    if not root.is_dir():
        return set()
    return {p.relative_to(root).as_posix() for p in root.rglob("*")}


class WatchedPiRpcRuntime(PiRpcRuntime):
    """PiRpcRuntime that records every request's workspace and flags any that
    escapes the sandbox root. Purely observational — it never blocks a call, so
    a leak shows up as evidence rather than as a changed code path."""

    def __init__(self, profile: PiRpcProfile, sandbox_root: Path):
        super().__init__(profile)
        self.sandbox_root = sandbox_root.resolve()
        self.seen: list[dict[str, str]] = []
        self.escapes: list[str] = []

    def invoke(self, request: AgentRequest) -> AgentResult:
        workspace = Path(request.workspace).resolve()
        inside = workspace == self.sandbox_root or self.sandbox_root in workspace.parents
        self.seen.append(
            {
                "role": request.role,
                "node_id": str(getattr(request, "node_id", "")),
                "access": getattr(request.access, "value", str(request.access)),
                "workspace": str(workspace),
                "inside_sandbox": str(inside),
            }
        )
        if not inside:
            self.escapes.append(str(workspace))
        return super().invoke(request)


# ---------------------------------------------------------------- setup

def build_clone(root: Path) -> Path:
    clone = root / "repository"
    banner("clone work-loop -> throwaway clone (real repo must stay untouched)")
    cp = run(["git", "clone", "--branch", BRANCH, str(SRC_REPO), str(clone)])
    print((cp.stdout + cp.stderr).strip()[-300:])
    wf = clone / "app" / "agents" / "workflow.py"
    assert wf.is_file(), "clone missing workflow.py"
    text = wf.read_text(encoding="utf-8")
    assert TARGET_CONST in text, f"clone lacks {TARGET_CONST}"
    assert SENTINEL not in text, "sentinel already present — target is not new"

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
            "timeout_seconds = 180",
            "",
            "[[validation.commands]]",
            'name = "workloop_tests"',
            "argv = " + json.dumps(VALIDATION_ARGV),
            "",
        ]),
        encoding="utf-8",
    )
    run(["git", "-C", str(clone), "add", ".workloop/project.toml"])
    run(["git", "-C", str(clone), "commit", "-m", "chore: workloop policy for self-dogfooding"])
    print("clone HEAD:", run(["git", "-C", str(clone), "log", "--oneline", "-1"]).stdout.strip())
    return clone


def warm_and_baseline(clone: Path) -> bool:
    banner("baseline: run the validation command in the clone (warm cache, confirm green)")
    env = {k: os.environ[k] for k in
           ("COMSPEC", "PATH", "PATHEXT", "SYSTEMROOT", "TEMP", "TMP",
            "USERPROFILE", "WINDIR", "LANG") if k in os.environ}
    cp = subprocess.run(VALIDATION_ARGV, cwd=str(clone), env=env,
                        capture_output=True, text=True, encoding="utf-8", timeout=300)
    print((cp.stdout or cp.stderr).strip()[-600:])
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


# ---------------------------------------------------------------- main

def main() -> int:
    banner(f"env: WORKLOOP_EXECUTION={os.environ.get('WORKLOOP_EXECUTION')!r} "
           f"WORKLOOP_NODE_WORKTREE={os.environ.get('WORKLOOP_NODE_WORKTREE')!r}")
    if not PI_BIN:
        print("FATAL: pi not on PATH")
        return 2
    print("pi bin:", PI_BIN, "| config dir:", PI_CONFIG_DIR)
    print("provider/model/thinking:", f"{PROVIDER}/{MODEL}", THINKING)
    print("target constant:", TARGET_CONST, "| sentinel:", SENTINEL)

    config = PI_CONFIG_DIR / "models.json"
    if not config.is_file():
        print(f"FATAL: {config} missing. Run configure_pi_provider.py first.")
        return 2
    try:
        providers = json.loads(config.read_text(encoding="utf-8")).get("providers", {})
    except json.JSONDecodeError as error:
        print(f"FATAL: {config} is not valid JSON: {error}")
        return 2
    if PROVIDER not in providers:
        print(f"FATAL: provider {PROVIDER!r} not in {config} "
              f"(configured: {sorted(providers)}). Run configure_pi_provider.py.")
        return 2
    served = [m.get("id") for m in providers[PROVIDER].get("models", []) if isinstance(m, dict)]
    if MODEL not in served:
        print(f"FATAL: model {MODEL!r} not configured for provider {PROVIDER!r} "
              f"(configured: {served}). Run configure_pi_provider.py --model {MODEL}.")
        return 2

    banner("TRIPWIRE: fingerprint the REAL repo before anything runs")
    real_before = fingerprint(SRC_REPO)
    real_git_before = git_state(SRC_REPO)
    pi_before = listing(PI_CONFIG_DIR)
    print("real repo files fingerprinted:", len(real_before))
    print("real repo HEAD:", real_git_before["HEAD"][:12], "| branch:", real_git_before["branch"])
    print("real repo dirty entries:", len(real_git_before["status"].splitlines()))
    print("pi config dir entries:", len(pi_before))

    status = "NOT_RUN"
    error = ""
    diff_ok = False
    marker_in_worktree = False
    runtime: WatchedPiRpcRuntime | None = None

    try:
        with tempfile.TemporaryDirectory(prefix="workloop-dogfood2-") as tmp:
            root = Path(tmp)
            clone = build_clone(root)
            if not warm_and_baseline(clone):
                print(">> baseline validation FAILED in the clone; aborting.")
                return 1

            profile = PiRpcProfile(
                command=[PI_BIN], model=MODEL, provider=PROVIDER,
                thinking=THINKING, config_dir=PI_CONFIG_DIR,
            )
            runtime = WatchedPiRpcRuntime(profile, root)
            validator = DeterministicValidator(sandbox=UnsafeDirectCommandSandbox())
            workflow = AgentWorkflow(root, runtime=runtime, validator=validator, max_iterations=2)
            project = workflow.register_project("workloop-self-2", clone, BRANCH)
            print("registered project:", project.project_id, "->", clone)

            requirement = (
                f"在 app/agents/workflow.py 中找到 {TARGET_CONST} 这个字符串常量，"
                f"在该常量文本的末尾追加一句：'{NEW_SENTENCE}'。"
                "这是唯一要做的一步编辑；不要改动其他任何常量、函数或逻辑，"
                "也不要运行任何测试或验证命令（项目验证 workloop_tests 由系统在执行完成后自动运行）。\n"
                "验收：\n"
                f"1. {TARGET_CONST} 的文本包含 {SENTINEL}\n"
                "2. workloop_tests 验证命令通过\n"
                "项目验证命令：workloop_tests"
            )
            task = workflow.create_task(
                "reviewer 提示：补充只读约束", requirement, project.project_id,
                budget=TaskBudget(max_iterations=2),
            )
            tid = task.task_id
            print("task:", tid)

            banner("STAGE 1: analyze (real glm planner)")
            t0 = time.monotonic()
            try:
                task = workflow.analyze(tid)
            except Exception:
                print(traceback.format_exc())
                rec = last_run(workflow, tid)
                if rec:
                    print("planner final_message:\n", str(rec.get("final_message", ""))[:800])
                return 1
            print(f"analyze elapsed: {time.monotonic()-t0:.1f}s | status: {task.status} | error: {task.error}")
            try:
                plan = workflow._load_plan(workflow.store.load(tid))
                print("PLAN steps:", plan.steps)
                print("PLAN acceptance:", plan.acceptance_criteria)
                print("PLAN required_tests:", plan.required_tests)
            except Exception as exc:
                print("plan load failed:", exc)
            if task.status != AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL:
                print(">> analyze did not reach WAITING_FOR_PLAN_APPROVAL.")
                return 1

            banner("STAGE 2: approve_plan (graph exec -> validate -> review)")
            t0 = time.monotonic()
            try:
                task = workflow.approve_plan(tid)
            except Exception:
                print(traceback.format_exc())
                show_outcome(workflow, tid)
                return 1
            print(f"approve_plan elapsed: {time.monotonic()-t0:.1f}s")
            print("FINAL status:", task.status, "| error:", task.error)
            status, error = str(task.status), str(task.error)
            marker_in_worktree, diff_ok = show_outcome(workflow, tid)
    finally:
        banner("TRIPWIRE: re-fingerprint the REAL repo after the run")
        real_after = fingerprint(SRC_REPO)
        real_git_after = git_state(SRC_REPO)
        pi_after = listing(PI_CONFIG_DIR)

        # Our own script writes nothing into the repo, but be explicit about the
        # one file we know we added (this script) so the report stays honest.
        delta = diff_fingerprints(real_before, real_after)
        print("content added:   ", delta["added"] or "(none)")
        print("content removed: ", delta["removed"] or "(none)")
        print("content modified:", delta["modified"] or "(none)")
        for key in ("HEAD", "branch", "status", "branches", "worktrees"):
            same = real_git_before[key] == real_git_after[key]
            print(f"git {key:<10} unchanged: {same}")
            if not same:
                print("   before:", real_git_before[key][:400])
                print("   after: ", real_git_after[key][:400])

        hits = []
        for path in SRC_REPO.rglob("*"):
            if any(part in FINGERPRINT_SKIP for part in path.relative_to(SRC_REPO).parts):
                continue
            if not path.is_file() or path.name == Path(__file__).name:
                continue
            try:
                if SENTINEL in path.read_text(encoding="utf-8", errors="ignore"):
                    hits.append(path.relative_to(SRC_REPO).as_posix())
            except OSError:
                continue
        print("sentinel found in real repo:", hits or "(nowhere — good)")

        banner("LEAK SURFACE: what the shared Pi config dir gained")
        gained = sorted(pi_after - pi_before)
        print("new entries under", PI_CONFIG_DIR, ":", len(gained))
        for entry in gained[:20]:
            print("  +", entry)

        banner("AGENT REQUEST WORKSPACES")
        if runtime is not None:
            for record in runtime.seen:
                print(" ", json.dumps(record, ensure_ascii=False))
            print("escapes outside sandbox root:", runtime.escapes or "(none)")

        banner("VERDICT")
        real_untouched = (
            not delta["added"] and not delta["removed"] and not delta["modified"]
            and all(real_git_before[k] == real_git_after[k] for k in real_git_before)
            and not hits
        )
        no_escape = runtime is None or not runtime.escapes
        print("real repo untouched:      ", real_untouched)
        print("no request escaped root:  ", no_escape)
        print("edit landed in worktree:  ", marker_in_worktree)
        print("workloop diff has the edit:", diff_ok)
        print("final task status:        ", status, "| error:", error)
        verdict = real_untouched and no_escape and marker_in_worktree and diff_ok
        print(">>>", "CLEAN SELF-EDIT RE-VERIFICATION PASSED" if verdict
              else "RE-VERIFICATION DID NOT PASS — see above")

    return 0 if verdict else 1


def show_outcome(workflow, tid) -> tuple[bool, bool]:
    """Print node/validation/review/diff evidence. Returns
    (sentinel present in worktree source, sentinel present in changes.diff)."""
    task = workflow.store.load(tid)
    print("NODE_RUNS:", json.dumps(task.node_runs, ensure_ascii=False, indent=2))
    vpath = workflow._round_dir(task) / "validation.json"
    if vpath.is_file():
        v = json.loads(vpath.read_text(encoding="utf-8"))
        print("VALIDATION passed:", v.get("passed"), "| error:", v.get("error"))
        for c in v.get("checks", []):
            print("  check", c.get("name"), "exit", c.get("exit_code"),
                  "| stderr:", str(c.get("stderr", ""))[:300])
    else:
        print("VALIDATION: (none)")
    rpath = (workflow.store.task_dir(tid) / "artifacts" / "rounds"
             / str(task.iteration) / "review.json")
    if rpath.is_file():
        print("REVIEW:", json.dumps(json.loads(rpath.read_text(encoding="utf-8")), ensure_ascii=False)[:1200])
    else:
        print("REVIEW: (none)")

    ws = Path(task.workspace)
    banner("task worktree git state")
    print("worktree:", ws)
    print("== status --short ==")
    print(run(["git", "-C", str(ws), "status", "--short"]).stdout.strip() or "(clean)")
    print("== diff --stat ==")
    print(run(["git", "-C", str(ws), "diff", "--stat"]).stdout.strip() or "(none)")

    diff_ok = False
    cdiff = (workflow.store.task_dir(tid) / "artifacts" / "rounds"
             / str(task.iteration) / "changes.diff")
    banner("workloop changes.diff (what review/delivery sees)")
    if cdiff.is_file():
        text = cdiff.read_text(encoding="utf-8")
        diff_ok = SENTINEL in text
        print("length:", len(text), "| mentions workflow.py:", "workflow.py" in text,
              "| contains sentinel:", diff_ok)
        print(text[:2500])
    else:
        print("changes.diff: (missing)")

    banner(f"edited {TARGET_CONST} in the task worktree")
    in_source = False
    wf = ws / "app" / "agents" / "workflow.py"
    if wf.is_file():
        text = wf.read_text(encoding="utf-8")
        i = text.find(f"{TARGET_CONST} = (")
        if i >= 0:
            seg = text[i:i + 1600]
            in_source = SENTINEL in seg
            print("contains sentinel:", in_source)
            print(seg)
        else:
            print(f"could not locate {TARGET_CONST}")
    rec = last_run(workflow, tid)
    if rec:
        print("\nlast run role:", rec.get("role"), "| final_message (first 600):")
        print(str(rec.get("final_message", ""))[:600])
    return in_source, diff_ok


if __name__ == "__main__":
    sys.exit(main())
