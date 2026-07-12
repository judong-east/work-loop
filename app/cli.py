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


LEGACY_COMMANDS = {"create-task", "run-loop", "resume", "deliver", "memory"}


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
    serve.add_argument("--root", default=".", help="Project root that contains the tasks directory")

    memory = sub.add_parser("memory", help="List or review the cross-task experience memory")
    memory.add_argument("--approve", default=None, metavar="EXP_ID", help="Approve a pending experience")
    memory.add_argument("--reject", default=None, metavar="EXP_ID", help="Reject a pending experience")
    memory.add_argument("--add", default=None, metavar="TEXT", help="Add a human-authored experience (approved directly)")
    memory.add_argument("--root", default=".", help="Project root that contains the memory directory")

    return parser


def build_backends() -> dict[str, ModelBackend]:
    return {"cli": CliBackend(), "fake": FakeBackend()}


def _print_task(task) -> None:
    print(json.dumps(to_plain(task), ensure_ascii=False, indent=2))


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
    elif args.command == "serve":
        # 局部导入避免 cli <-> web 循环依赖（web.server 复用本模块的 build_backends）
        from app.web.server import make_server

        server = make_server(root, args.port)
        print(f"Workloop 控制台已启动：http://127.0.0.1:{server.server_address[1]}（Ctrl+C 停止）")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("已停止。")
    elif args.command == "memory":
        store = kernel.experience
        try:
            if args.approve:
                record = store.approve(args.approve)
                print(f"已批准 {record.experience_id}：{record.text}")
            elif args.reject:
                record = store.reject(args.reject)
                print(f"已驳回 {record.experience_id}")
            elif args.add:
                record = store.add_manual(args.add)
                print(f"已录入 {record.experience_id}（人工经验，直接批准）")
            else:
                records = store.list_all()
                if not records:
                    print("经验库为空。")
                for status in ("pending", "approved", "rejected"):
                    group = [r for r in records if r.status == status]
                    if not group:
                        continue
                    label = {"pending": "待评审", "approved": "已批准", "rejected": "已驳回"}[status]
                    print(f"{label}（{len(group)}）：")
                    for record in sorted(group, key=lambda r: r.updated_at, reverse=True):
                        source = f" ← {record.source_task}" if record.source_task else ""
                        print(f"- [{record.experience_id}] ({record.kind}) {record.text}{source}")
        except (ValueError, FileNotFoundError) as error:
            print(f"memory 失败：{error}", file=sys.stderr)
            sys.exit(2)


if __name__ == "__main__":
    main()
