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
    create.add_argument("--root", default=".", help="Project root that contains the tasks directory")

    run_loop = sub.add_parser("run-loop", help="Run plan -> execute -> review with role-routed models")
    run_loop.add_argument("--task-id", required=True)
    run_loop.add_argument("--root", default=".", help="Project root that contains the tasks directory")
    run_loop.add_argument("--models-config", default="models.json", help="Path to models.json")

    return parser


def build_backends() -> dict[str, ModelBackend]:
    return {"cli": CliBackend(), "fake": FakeBackend()}


def main() -> None:
    args = build_parser().parse_args()
    root = Path(args.root).resolve()
    kernel = WorkloopKernel(root)

    if args.command == "create-task":
        task = kernel.create_task(args.title, args.goal, args.input)
        print(json.dumps(to_plain(task), ensure_ascii=False, indent=2))
    elif args.command == "run-loop":
        try:
            routing = load_routing_config(Path(args.models_config))
            task = kernel.run_model_loop(args.task_id, routing, build_backends())
        except (ValueError, FileNotFoundError) as error:
            print(f"run-loop 失败：{error}", file=sys.stderr)
            sys.exit(2)
        print(json.dumps(to_plain(task), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
