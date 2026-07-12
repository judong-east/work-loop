from __future__ import annotations

import re
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
from pathlib import Path
from typing import Any

from app.projects.contracts import ProjectPolicy, ValidationCommand


class ProjectPolicyLoader:
    def load(self, repository: Path, config_path: str) -> ProjectPolicy:
        path = self._config_location(repository, config_path)
        if not path.is_file():
            raise ValueError(f"项目策略文件不存在：{path}")
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ValueError(f"无法读取项目策略 {path}：{error}") from error
        return self._parse(data)

    def _config_location(self, repository: Path, config_path: str) -> Path:
        if not config_path.strip():
            raise ValueError("项目策略路径不能为空。")
        root = Path(repository).resolve()
        candidate = (root / config_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise ValueError(f"项目策略路径越出仓库：{config_path}") from error
        return candidate

    def _parse(self, data: dict[str, Any]) -> ProjectPolicy:
        version = data.get("schema_version", 1)
        if isinstance(version, bool) or version != 1:
            raise ValueError(f"不支持的项目策略 schema_version：{version!r}")

        permissions = self._table(data, "permissions")
        protected_paths = self._string_list(permissions, "protected_paths")
        network = permissions.get("network", "deny")
        if network != "deny":
            raise ValueError("第一版 permissions.network 必须是 deny；网络访问需要单独人工授权。")

        validation = self._table(data, "validation")
        timeout = validation.get("timeout_seconds")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            raise ValueError("validation.timeout_seconds 必须是正整数。")
        raw_commands = validation.get("commands")
        if not isinstance(raw_commands, list):
            raise ValueError("validation.commands 必须是命令数组。")

        commands: list[ValidationCommand] = []
        seen: set[str] = set()
        for item in raw_commands:
            if not isinstance(item, dict):
                raise ValueError("每个验证命令必须是对象。")
            name = item.get("name")
            argv = item.get("argv")
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
                raise ValueError(f"验证命令名称不合法：{name!r}")
            if name in seen:
                raise ValueError(f"验证命令名称重复：{name}")
            if (
                not isinstance(argv, list)
                or not argv
                or not all(isinstance(value, str) and value for value in argv)
            ):
                raise ValueError(f"验证命令 {name} 的 argv 必须是非空字符串数组。")
            commands.append(ValidationCommand(name=name, argv=list(argv)))
            seen.add(name)

        evidence = data.get("evidence", {})
        if not isinstance(evidence, dict):
            raise ValueError("evidence 必须是配置表。")
        redact_patterns = self._string_list(evidence, "redact_patterns") if evidence else []
        for pattern in redact_patterns:
            if len(pattern) > 256 or "\n" in pattern or "\r" in pattern:
                raise ValueError("脱敏规则不能超过 256 字符或包含换行。")
            if "*" in pattern[:-1]:
                raise ValueError("脱敏规则只允许一个位于末尾的 * 通配符。")

        return ProjectPolicy(
            validation_commands=commands,
            protected_paths=protected_paths,
            timeout_seconds=timeout,
            network=network,
            redact_patterns=redact_patterns,
        )

    def _table(self, data: dict[str, Any], key: str) -> dict[str, Any]:
        value = data.get(key)
        if not isinstance(value, dict):
            raise ValueError(f"{key} 必须是配置表。")
        return value

    def _string_list(self, data: dict[str, Any], key: str) -> list[str]:
        value = data.get(key)
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item for item in value
        ):
            raise ValueError(f"{key} 必须是非空字符串数组。")
        return list(value)
