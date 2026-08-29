from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def _default_root() -> str:
    if _is_frozen():
        data_dir = Path.home() / ".workloop"
        data_dir.mkdir(parents=True, exist_ok=True)
        return str(data_dir)
    return "."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Workloop V2 multi-model workbench")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="Start the local V2 workbench")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--root", default=_default_root(), help="Workloop data root")
    serve.add_argument("--no-browser", action="store_true", help="Do not open a browser")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    from app.web.server import make_server

    server = make_server(
        Path(args.root).resolve(),
        args.port,
        open_browser=_is_frozen() and not args.no_browser,
    )
    print(f"Workloop V2 已启动：http://127.0.0.1:{server.server_address[1]}（Ctrl+C 停止）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("已停止。")


if __name__ == "__main__":
    main()
