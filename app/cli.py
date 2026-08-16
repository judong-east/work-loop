from __future__ import annotations

import argparse
import sys
from pathlib import Path


LEGACY_COMMANDS = {"create-task", "run-loop", "resume", "deliver", "memory"}


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def _default_root() -> str:
    """Return the default data root: ~/.workloop when frozen, '.' otherwise."""
    if _is_frozen():
        data_dir = Path.home() / ".workloop"
        data_dir.mkdir(parents=True, exist_ok=True)
        return str(data_dir)
    return "."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Workloop reliable loop-engineering kernel")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-task", help="Create a task and run the first reliability loop")
    create.add_argument("--title", required=True)
    create.add_argument("--goal", required=True)
    create.add_argument("--input", required=True, help="Raw requirement or problem description")
    create.add_argument(
        "--context-file", action="append", default=[],
        help="Requirement doc or code file/directory fed to the planner (repeatable)",
    )
    create.add_argument("--root", default=".", help="Project root that contains the tasks directory")

    run_loop = sub.add_parser("run-loop", help="Run plan -> execute -> code review with role-routed models")
    run_loop.add_argument("--task-id", required=True)
    run_loop.add_argument("--root", default=".", help="Project root that contains the tasks directory")
    run_loop.add_argument("--models-config", default="models.json", help="Path to models.json")
    run_loop.add_argument("--workspace-from", default=None, help="Seed the sandbox workspace from this directory")

    resume = sub.add_parser("resume", help="Show pending questions, or answer them to re-run the gate")
    resume.add_argument("--task-id", required=True)
    resume.add_argument("--answer", default=None, help="Human clarification; omit to list pending questions")
    resume.add_argument("--root", default=".", help="Project root that contains the tasks directory")

    deliver = sub.add_parser("deliver", help="Write reviewed workspace changes back to a real directory")
    deliver.add_argument("--task-id", required=True)
    deliver.add_argument("--dest", required=True, help="Destination directory for the reviewed changes")
    deliver.add_argument("--yes", action="store_true", help="Skip the interactive confirmation")
    deliver.add_argument("--root", default=".", help="Project root that contains the tasks directory")

    serve = sub.add_parser("serve", help="Start the local web console (binds 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--root", default=_default_root(), help="Project root that contains the tasks directory")
    serve.add_argument("--no-browser", action="store_true", help="Do not auto-open the browser (frozen mode only)")

    memory = sub.add_parser("memory", help="List or review the cross-task experience memory")
    memory.add_argument("--approve", default=None, metavar="EXP_ID", help="Approve a pending experience")
    memory.add_argument("--reject", default=None, metavar="EXP_ID", help="Reject a pending experience")
    memory.add_argument("--add", default=None, metavar="TEXT", help="Add a human-authored experience (approved directly)")
    memory.add_argument("--root", default=".", help="Project root that contains the memory directory")

    return parser


def main() -> None:
    args = build_parser().parse_args()
    root = Path(args.root).resolve()
    if args.command in LEGACY_COMMANDS:
        print(
            "该命令属于 legacy-v1 写工作流，已停用。请启动 `serve` 并通过 "
            "Agent Runtime 任务接口操作；历史任务仍可只读查看。",
            file=sys.stderr,
        )
        sys.exit(2)

    # `serve` is the only live command; everything else is intercepted above.
    from app.web.server import make_server

    should_open_browser = _is_frozen() and not args.no_browser
    server = make_server(root, args.port, open_browser=should_open_browser)
    print(f"Workloop 控制台已启动：http://127.0.0.1:{server.server_address[1]}（Ctrl+C 停止）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("已停止。")


if __name__ == "__main__":
    main()
