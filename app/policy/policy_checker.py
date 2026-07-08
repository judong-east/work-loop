from __future__ import annotations

from fnmatch import fnmatch

from app.core.contracts import ModelRoutingConfig, PolicyBoundary, PolicyCheck


def _segments(value: str) -> list[str]:
    return [part for part in value.replace("\\", "/").split("/") if part not in ("", ".")]


def _glob_match(parts: list[str], patterns: list[str]) -> bool:
    # fnmatch 不支持 **，这里按路径段实现 glob 语义：** 匹配零个或多个段，
    # 其余通配符只在单段内生效，保证 **/.env 也能命中根级 .env。
    if not patterns:
        return not parts
    head, rest = patterns[0], patterns[1:]
    if head == "**":
        return any(_glob_match(parts[i:], rest) for i in range(len(parts) + 1))
    return bool(parts) and fnmatch(parts[0], head) and _glob_match(parts[1:], rest)


class PolicyChecker:
    def check_context(self, policy: PolicyBoundary, confidence: float, conflicts: list[str]) -> PolicyCheck:
        issues: list[str] = []
        warnings: list[str] = []
        requires_human = False

        if confidence < policy.min_context_confidence:
            issues.append(f"上下文置信度 {confidence:.2f} 低于阈值 {policy.min_context_confidence:.2f}。")

        if conflicts and policy.require_human_for_conflicts:
            warnings.append("上下文存在冲突，必须人工确认。")
            requires_human = True

        return PolicyCheck(passed=not issues, issues=issues, warnings=warnings, requires_human=requires_human)

    def check_tool(self, policy: PolicyBoundary, tool_name: str) -> PolicyCheck:
        if tool_name in policy.forbidden_tools:
            return PolicyCheck(False, [f"工具 {tool_name} 被禁止调用。"])
        if policy.allowed_tools and tool_name not in policy.allowed_tools:
            return PolicyCheck(False, [f"工具 {tool_name} 不在允许列表中。"])
        if tool_name in policy.restricted_tools:
            return PolicyCheck(True, warnings=[f"工具 {tool_name} 需要人工确认。"], requires_human=True)
        return PolicyCheck(True)

    def check_path(self, policy: PolicyBoundary, path: str) -> PolicyCheck:
        parts = _segments(path)

        for pattern in policy.deny_paths:
            if _glob_match(parts, _segments(pattern)):
                return PolicyCheck(False, [f"路径 {path} 命中禁止规则 {pattern}。"])

        if policy.allow_paths:
            allowed = any(_glob_match(parts, _segments(pattern)) for pattern in policy.allow_paths)
            if not allowed:
                return PolicyCheck(False, [f"路径 {path} 不在允许修改范围内。"])

        return PolicyCheck(True)

    def check_model_assignment(self, policy: PolicyBoundary, routing: ModelRoutingConfig) -> PolicyCheck:
        issues: list[str] = []
        for group in policy.distinct_model_roles:
            seen: dict[str, str] = {}
            for role in group:
                profile_name = routing.roles.get(role, routing.roles.get("default", ""))
                profile = routing.profiles.get(profile_name)
                if profile is None:
                    issues.append(f"角色 {role} 无法解析到模型配置。")
                    continue
                if profile.model in seen:
                    issues.append(
                        f"角色 {seen[profile.model]} 与 {role} 解析到同一模型 {profile.model}，违反异构审核约束。"
                    )
                else:
                    seen[profile.model] = role
        return PolicyCheck(passed=not issues, issues=issues)


