from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.core.contracts import to_plain
from app.core.workflow import WorkloopKernel
from app.models.backends.base import ModelBackend
from app.models.backends.cli_backend import CliBackend
from app.models.backends.fake_backend import FakeBackend
from app.models.config import load_routing_config


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

    return parser


def build_backends() -> dict[str, ModelBackend]:
    return {"cli": CliBackend(), "fake": FakeBackend()}


def _print_task(task) -> None:
    print(json.dumps(to_plain(task), ensure_ascii=False, indent=2))


def main() -> None:
    args = build_parser().parse_args()
    root = Path(args.root).resolve()
    kernel = WorkloopKernel(root)

    if args.command == "create-task":
        task = kernel.create_task(
            args.title, args.goal, args.input,
            context_files=[Path(item) for item in args.context_file],
        )
        _print_task(task)
    elif args.command == "run-loop":
        try:
            routing = load_routing_config(Path(args.models_config))
            task = kernel.run_model_loop(
                args.task_id, routing, build_backends(),
                workspace_from=Path(args.workspace_from) if args.workspace_from else None,
            )
        except (ValueError, FileNotFoundError) as error:
            print(f"run-loop 失败：{error}", file=sys.stderr)
            sys.exit(2)
        _print_task(task)
    elif args.command == "resume":
        try:
            if args.answer is None:
                questions = kernel.pending_questions(args.task_id)
                if questions:
                    print("待确认问题：")
                    for question in questions:
                        print(f"- {question}")
                else:
                    print("没有待确认问题。")
            else:
                _print_task(kernel.resume_task(args.task_id, args.answer))
        except (ValueError, FileNotFoundError) as error:
            print(f"resume 失败：{error}", file=sys.stderr)
            sys.exit(2)
    elif args.command == "deliver":
        try:
            changes = kernel.pending_delivery(args.task_id)
        except (ValueError, FileNotFoundError) as error:
            print(f"deliver 失败：{error}", file=sys.stderr)
            sys.exit(2)
        if not changes:
            print("没有可交付的变更。")
            return
        print("待交付变更：")
        for change in changes:
            label = "删除" if change.action == "delete" else "写入"
            print(f"- {label} {change.path}")
        if not args.yes:
            # write_file 属 restricted 工具语义：写真实目录必须人工确认
            answer = input(f"确认将以上 {len(changes)} 项变更写入 {args.dest}？[y/N] ")
            if answer.strip().lower() != "y":
                print("已取消交付。", file=sys.stderr)
                sys.exit(1)
        try:
            applied = kernel.deliver(args.task_id, Path(args.dest))
        except (ValueError, FileNotFoundError) as error:
            print(f"deliver 失败：{error}", file=sys.stderr)
            sys.exit(2)
        print(f"已交付 {len(applied)} 项变更到 {args.dest}")


if __name__ == "__main__":
    main()
