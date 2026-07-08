# 多模型分工能力实施计划（Multi-Model Role Routing）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Workloop 内核实现按角色（planner/executor/reviewer）路由到不同模型的调用层，并以边界策略强制审核模型 ≠ 执行模型。

**Architecture:** 新增 `app/models/` 子系统（契约集中于 `contracts.py`、配置加载、路由、CLI/fake 双后端），扩展 PolicyChecker 做 distinct 校验，`workflow.py` 新增 `run_model_loop` 编排 plan→execute→review 并全量落盘。后端按 `profile.provider` 从 `backends: dict[str, ModelBackend]` 分派。

**Tech Stack:** Python 3.11，仅标准库（dataclasses、json、subprocess、unittest、unittest.mock）。测试框架为 unittest（非 pytest）。

**约定：**
- 规格文档：`docs/superpowers/specs/2026-07-03-multi-model-design.md`
- 本计划不含任何 git 操作（用户全局指令：未经主动要求不执行 git 操作；且项目目录尚未初始化 git 仓库）。每个任务以运行测试收尾。
- 全量测试命令：`cd D:/judong/code/workloop && python -m unittest discover -s tests -v`
- 单文件测试命令：`cd D:/judong/code/workloop && python -m unittest discover -s tests -p "test_models.py" -v`
- 所有新代码的用户可见消息用中文（与现有 policy_checker/evaluators 一致）。

---

### Task 1: DecisionEngine 阈值传导修复

> **实施期修订（代码质量审查结论）**：本任务初版方案（传入 boundary 参数）已被推翻并按下述方案重做——阈值判定唯一归属 `PolicyChecker.check_context`，**删除** DecisionEngine 的置信度分支（该分支被 workflow 中的 policy 门禁前置遮蔽，在 kernel 路径不可达为真，属死代码 + 逻辑重复）。`decide_after_context` 签名保持原状。回归测试改为：(1) kernel 级——自定义严格阈值 0.9 经 `create_task` 走到 `POLICY_BLOCKED`，证明配置在生产路径生效；(2) 单元级——score 0.55 且 policy 通过时决策放行 `READY_FOR_PLAN`，防止置信度二次门禁回归（旧硬编码实现下该测试失败）。规格文档 §12 已同步修订。以下原始步骤保留作历史记录，不再照此执行。

**Files:**
- Modify: `app/decision/decision_engine.py`
- Modify: `app/core/workflow.py:61`
- Test: `tests/test_kernel.py`

- [ ] **Step 1: 在 tests/test_kernel.py 追加失败测试**

注意用例构造：score 必须落在自定义阈值与旧硬编码值 0.65 之间（此处 0.55，阈值 0.5），否则两种实现表现相同、测试无鉴别力。

```python
    def test_decision_threshold_comes_from_boundary(self) -> None:
        from app.core.contracts import ContextPack, ContextSection, TaskState
        from app.decision.decision_engine import DecisionEngine
        from app.evaluation.evaluators import ContextEvaluator

        pack = ContextPack(task_id="TASK-x", purpose="t")
        pack.sections.append(ContextSection(name="s", content="c", confidence=0.55))
        evaluation = ContextEvaluator().evaluate(pack)  # score 0.55，无任何 issue
        lenient = PolicyBoundary(min_context_confidence=0.5)
        check = PolicyChecker().check_context(lenient, evaluation.score, [])
        self.assertTrue(check.passed)

        decision = DecisionEngine().decide_after_context(
            TaskState(title="t", goal="g"), evaluation, check, lenient
        )
        # 0.55 ≥ 自定义阈值 0.5：应放行进入 READY_FOR_PLAN；
        # 若阈值仍硬编码 0.65，这里会错误地转入 CLARIFICATION_REQUIRED
        self.assertEqual(decision.next_state, TaskStatus.READY_FOR_PLAN)
```

- [ ] **Step 2: 运行验证失败**

Run: `cd D:/judong/code/workloop && python -m unittest discover -s tests -p "test_kernel.py" -v`
Expected: FAIL — `TypeError: decide_after_context() takes 4 positional arguments but 5 were given`

- [ ] **Step 3: 修改 decision_engine.py**

签名与阈值行（其余分支不动）：

```python
from app.core.contracts import (
    DecisionAction,
    DecisionResult,
    EvaluationResult,
    PolicyBoundary,
    PolicyCheck,
    TaskState,
    TaskStatus,
)


class DecisionEngine:
    def decide_after_context(
        self,
        task: TaskState,
        evaluation: EvaluationResult,
        policy_check: PolicyCheck,
        boundary: PolicyBoundary,
    ) -> DecisionResult:
```

原 48 行 `if evaluation.score < 0.65:` 改为：

```python
        if evaluation.score < boundary.min_context_confidence:
```

同分支内 reason 改为：

```python
                reason=f"上下文评分 {evaluation.score:.2f} 低于阈值 {boundary.min_context_confidence:.2f}，不足以进入方案设计。",
```

- [ ] **Step 4: 修改 workflow.py 调用点（第 61 行）**

```python
        decision = self.decision_engine.decide_after_context(task, evaluation, policy_check, boundary)
```

- [ ] **Step 5: 运行全量测试**

Run: `cd D:/judong/code/workloop && python -m unittest discover -s tests -v`
Expected: 全部 PASS（原 4 个 + 新 1 个）

---

### Task 2: 模型契约与任务加载

**Files:**
- Modify: `app/core/contracts.py`（文件末尾追加）
- Modify: `app/core/artifact_store.py`
- Test: `tests/test_model_loop.py`（新建）

- [ ] **Step 1: 新建 tests/test_model_loop.py，写失败测试**

```python
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.core.artifact_store import ArtifactStore
from app.core.contracts import TaskStatus, task_state_from_dict
from app.core.workflow import WorkloopKernel


class TaskLoadTest(unittest.TestCase):
    def test_task_state_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            created = kernel.create_task(
                title="往返",
                goal="验证加载",
                raw_input="目标：验证任务加载。验收标准：通过测试。",
            )
            store = ArtifactStore(Path(tmp) / "tasks")
            loaded = store.load_task(created.task_id)
            self.assertEqual(loaded.task_id, created.task_id)
            self.assertEqual(loaded.status, TaskStatus.READY_FOR_PLAN)
            self.assertEqual(loaded.title, "往返")
            self.assertEqual(loaded.context_refs, created.context_refs)

    def test_task_state_from_dict_defaults(self) -> None:
        task = task_state_from_dict({"title": "t", "goal": "g", "task_id": "TASK-1"})
        self.assertEqual(task.status, TaskStatus.CREATED)
        self.assertEqual(task.iteration, 0)
        self.assertEqual(task.artifacts, {})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行验证失败**

Run: `cd D:/judong/code/workloop && python -m unittest discover -s tests -p "test_model_loop.py" -v`
Expected: FAIL — `ImportError: cannot import name 'task_state_from_dict'`

- [ ] **Step 3: contracts.py 末尾追加模型契约与反序列化**

```python
@dataclass
class ModelProfile:
    name: str
    provider: str
    model: str
    command: list[str] = field(default_factory=list)
    timeout_seconds: int = 300


@dataclass
class ModelRequest:
    task_id: str
    role: str
    prompt: str


@dataclass
class ModelResponse:
    text: str
    profile_name: str
    model: str
    duration_seconds: float
    succeeded: bool
    error: str = ""


@dataclass
class ModelRoutingConfig:
    profiles: dict[str, ModelProfile] = field(default_factory=dict)
    roles: dict[str, str] = field(default_factory=dict)


def task_state_from_dict(data: dict[str, Any]) -> TaskState:
    return TaskState(
        title=data["title"],
        goal=data["goal"],
        task_id=data["task_id"],
        status=TaskStatus(data.get("status", TaskStatus.CREATED.value)),
        inputs=list(data.get("inputs", [])),
        artifacts=dict(data.get("artifacts", {})),
        context_refs=list(data.get("context_refs", [])),
        decisions=list(data.get("decisions", [])),
        evaluations=list(data.get("evaluations", [])),
        events=list(data.get("events", [])),
        risk_level=data.get("risk_level", "medium"),
        iteration=int(data.get("iteration", 0)),
        created_at=data.get("created_at", utc_now()),
        updated_at=data.get("updated_at", utc_now()),
    )
```

- [ ] **Step 4: artifact_store.py 追加 load_task（save_task 之后）**

```python
    def load_task(self, task_id: str) -> TaskState:
        path = self.root / task_id / "state.json"
        if not path.exists():
            raise FileNotFoundError(f"任务 {task_id} 不存在：{path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return task_state_from_dict(data)
```

同文件 import 行改为：

```python
from app.core.contracts import TaskState, task_state_from_dict, to_plain, utc_now
```

- [ ] **Step 5: 运行全量测试**

Run: `cd D:/judong/code/workloop && python -m unittest discover -s tests -v`
Expected: 全部 PASS

---

### Task 3: 配置加载与校验

**Files:**
- Create: `app/models/__init__.py`（空文件）
- Create: `app/models/config.py`
- Test: `tests/test_models.py`（新建）

- [ ] **Step 1: 新建 tests/test_models.py，写失败测试**

```python
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.models.config import load_routing_config


def write_config(tmp: str, data: dict) -> Path:
    path = Path(tmp) / "models.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


VALID = {
    "profiles": [
        {"name": "plan-a", "provider": "cli", "model": "model-a", "command": ["a-cli", "{prompt}"]},
        {"name": "exec-b", "provider": "cli", "model": "model-b", "command": ["b-cli", "{prompt}", "--model", "{model}"]},
        {"name": "review-c", "provider": "fake", "model": "model-c"},
    ],
    "roles": {"planner": "plan-a", "executor": "exec-b", "reviewer": "review-c", "default": "exec-b"},
}


class ConfigTest(unittest.TestCase):
    def test_valid_config_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_routing_config(write_config(tmp, VALID))
            self.assertEqual(config.profiles["plan-a"].model, "model-a")
            self.assertEqual(config.roles["default"], "exec-b")

    def test_missing_default_role_raises(self) -> None:
        data = json.loads(json.dumps(VALID))
        del data["roles"]["default"]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "default"):
                load_routing_config(write_config(tmp, data))

    def test_unknown_profile_reference_raises(self) -> None:
        data = json.loads(json.dumps(VALID))
        data["roles"]["reviewer"] = "no-such-profile"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "no-such-profile"):
                load_routing_config(write_config(tmp, data))

    def test_duplicate_profile_name_raises(self) -> None:
        data = json.loads(json.dumps(VALID))
        data["profiles"].append(dict(data["profiles"][0]))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "重复"):
                load_routing_config(write_config(tmp, data))

    def test_cli_profile_without_prompt_placeholder_raises(self) -> None:
        data = json.loads(json.dumps(VALID))
        data["profiles"][0]["command"] = ["a-cli", "run"]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "prompt"):
                load_routing_config(write_config(tmp, data))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行验证失败**

Run: `cd D:/judong/code/workloop && python -m unittest discover -s tests -p "test_models.py" -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.models'`

- [ ] **Step 3: 创建 app/models/__init__.py（空）与 app/models/config.py**

```python
from __future__ import annotations

import json
from pathlib import Path

from app.core.contracts import ModelProfile, ModelRoutingConfig


def load_routing_config(path: Path) -> ModelRoutingConfig:
    data = json.loads(Path(path).read_text(encoding="utf-8"))

    profiles: dict[str, ModelProfile] = {}
    for item in data.get("profiles", []):
        profile = ModelProfile(
            name=item["name"],
            provider=item["provider"],
            model=item["model"],
            command=list(item.get("command", [])),
            timeout_seconds=int(item.get("timeout_seconds", 300)),
        )
        if profile.name in profiles:
            raise ValueError(f"模型配置名重复：{profile.name}。")
        if profile.timeout_seconds <= 0:
            raise ValueError(f"模型配置 {profile.name} 的 timeout_seconds 必须大于 0。")
        if profile.provider == "cli":
            if not profile.command:
                raise ValueError(f"CLI 模型配置 {profile.name} 缺少 command。")
            if not any("{prompt}" in part for part in profile.command):
                raise ValueError(f"CLI 模型配置 {profile.name} 的 command 缺少 {{prompt}} 占位符。")
        profiles[profile.name] = profile

    roles = {str(key): str(value) for key, value in data.get("roles", {}).items()}
    if "default" not in roles:
        raise ValueError("roles 必须包含 default 兜底角色。")
    for role, profile_name in roles.items():
        if profile_name not in profiles:
            raise ValueError(f"角色 {role} 引用了不存在的模型配置 {profile_name}。")

    return ModelRoutingConfig(profiles=profiles, roles=roles)
```

- [ ] **Step 4: 运行测试**

Run: `cd D:/judong/code/workloop && python -m unittest discover -s tests -p "test_models.py" -v`
Expected: 5 个测试 PASS

---

### Task 4: 角色路由器

**Files:**
- Create: `app/models/router.py`
- Test: `tests/test_models.py`（追加）

- [ ] **Step 1: tests/test_models.py 追加失败测试**

文件顶部追加导入：

```python
from app.models.router import ModelRouter
```

追加测试类：

```python
class RouterTest(unittest.TestCase):
    def _config(self):
        with tempfile.TemporaryDirectory() as tmp:
            return load_routing_config(write_config(tmp, VALID))

    def test_resolve_configured_role(self) -> None:
        profile, fallback = ModelRouter(self._config()).resolve("planner")
        self.assertEqual(profile.name, "plan-a")
        self.assertFalse(fallback)

    def test_resolve_unknown_role_falls_back_to_default(self) -> None:
        profile, fallback = ModelRouter(self._config()).resolve("no-such-role")
        self.assertEqual(profile.name, "exec-b")
        self.assertTrue(fallback)
```

- [ ] **Step 2: 运行验证失败**

Run: `cd D:/judong/code/workloop && python -m unittest discover -s tests -p "test_models.py" -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.models.router'`

- [ ] **Step 3: 创建 app/models/router.py**

```python
from __future__ import annotations

from app.core.contracts import ModelProfile, ModelRoutingConfig


class ModelRouter:
    """按角色解析模型配置；未配置的角色回落 default。"""

    def __init__(self, config: ModelRoutingConfig):
        self.config = config

    def resolve(self, role: str) -> tuple[ModelProfile, bool]:
        profile_name = self.config.roles.get(role)
        fallback = profile_name is None
        if fallback:
            profile_name = self.config.roles["default"]
        return self.config.profiles[profile_name], fallback
```

- [ ] **Step 4: 运行测试**

Run: `cd D:/judong/code/workloop && python -m unittest discover -s tests -p "test_models.py" -v`
Expected: 全部 PASS

---

### Task 5: 后端抽象与 FakeBackend

**Files:**
- Create: `app/models/backends/__init__.py`（空文件）
- Create: `app/models/backends/base.py`
- Create: `app/models/backends/fake_backend.py`
- Test: `tests/test_models.py`（追加）

- [ ] **Step 1: tests/test_models.py 追加失败测试**

文件顶部追加导入：

```python
from app.core.contracts import ModelProfile, ModelRequest
from app.models.backends.fake_backend import FakeBackend
```

追加测试类：

```python
FAKE_PROFILE = ModelProfile(name="f", provider="fake", model="fake-model")


class FakeBackendTest(unittest.TestCase):
    def test_default_reviewer_response_is_pass_json(self) -> None:
        response = FakeBackend().invoke(FAKE_PROFILE, ModelRequest(task_id="T", role="reviewer", prompt="p"))
        self.assertTrue(response.succeeded)
        self.assertEqual(json.loads(response.text)["verdict"], "pass")

    def test_configured_response_and_failure(self) -> None:
        backend = FakeBackend(responses={"planner": "计划内容"}, failures={"executor"})
        ok = backend.invoke(FAKE_PROFILE, ModelRequest(task_id="T", role="planner", prompt="p"))
        bad = backend.invoke(FAKE_PROFILE, ModelRequest(task_id="T", role="executor", prompt="p"))
        self.assertEqual(ok.text, "计划内容")
        self.assertFalse(bad.succeeded)
        self.assertEqual(len(backend.requests), 2)
```

- [ ] **Step 2: 运行验证失败**

Run: `cd D:/judong/code/workloop && python -m unittest discover -s tests -p "test_models.py" -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.models.backends'`

- [ ] **Step 3: 创建 base.py 与 fake_backend.py**

`app/models/backends/base.py`：

```python
from __future__ import annotations

from abc import ABC, abstractmethod

from app.core.contracts import ModelProfile, ModelRequest, ModelResponse


class ModelBackend(ABC):
    @abstractmethod
    def invoke(self, profile: ModelProfile, request: ModelRequest) -> ModelResponse:
        """调用模型并返回响应；实现不得抛出调用失败异常，失败以 succeeded=False 表达。"""
```

`app/models/backends/fake_backend.py`：

```python
from __future__ import annotations

from app.core.contracts import ModelProfile, ModelRequest, ModelResponse
from app.models.backends.base import ModelBackend

DEFAULT_REVIEW = '{"verdict": "pass", "issues": []}'


class FakeBackend(ModelBackend):
    """离线测试后端：按角色返回预置应答或预置失败。"""

    def __init__(self, responses: dict[str, str] | None = None, failures: set[str] | None = None):
        self.responses = responses or {}
        self.failures = failures or set()
        self.requests: list[ModelRequest] = []

    def invoke(self, profile: ModelProfile, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if request.role in self.failures:
            return ModelResponse(
                text="", profile_name=profile.name, model=profile.model,
                duration_seconds=0.0, succeeded=False, error="预置失败",
            )
        if request.role in self.responses:
            text = self.responses[request.role]
        elif request.role == "reviewer":
            text = DEFAULT_REVIEW
        else:
            text = f"fake response for {request.role}"
        return ModelResponse(
            text=text, profile_name=profile.name, model=profile.model,
            duration_seconds=0.0, succeeded=True,
        )
```

- [ ] **Step 4: 运行测试**

Run: `cd D:/judong/code/workloop && python -m unittest discover -s tests -p "test_models.py" -v`
Expected: 全部 PASS

---

### Task 6: CLI 子进程后端

**Files:**
- Create: `app/models/backends/cli_backend.py`
- Test: `tests/test_models.py`（追加）

- [ ] **Step 1: tests/test_models.py 追加失败测试**

文件顶部追加导入：

```python
import subprocess
from unittest import mock

from app.models.backends.cli_backend import CliBackend
```

追加测试类：

```python
CLI_PROFILE = ModelProfile(
    name="c", provider="cli", model="model-x",
    command=["some-cli", "-p", "{prompt}", "--model", "{model}"], timeout_seconds=7,
)


class CliBackendTest(unittest.TestCase):
    def test_command_rendering_and_success(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="答案", stderr="")
        with mock.patch("app.models.backends.cli_backend.subprocess.run", return_value=completed) as run:
            response = CliBackend().invoke(CLI_PROFILE, ModelRequest(task_id="T", role="planner", prompt="你好"))
        run.assert_called_once()
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["some-cli", "-p", "你好", "--model", "model-x"])
        self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(kwargs["timeout"], 7)
        self.assertTrue(response.succeeded)
        self.assertEqual(response.text, "答案")

    def test_nonzero_exit_is_failure(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="炸了")
        with mock.patch("app.models.backends.cli_backend.subprocess.run", return_value=completed):
            response = CliBackend().invoke(CLI_PROFILE, ModelRequest(task_id="T", role="planner", prompt="p"))
        self.assertFalse(response.succeeded)
        self.assertIn("炸了", response.error)

    def test_timeout_is_failure(self) -> None:
        with mock.patch(
            "app.models.backends.cli_backend.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="x", timeout=7),
        ):
            response = CliBackend().invoke(CLI_PROFILE, ModelRequest(task_id="T", role="planner", prompt="p"))
        self.assertFalse(response.succeeded)
        self.assertIn("超时", response.error)

    def test_empty_output_is_failure(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="  \n", stderr="")
        with mock.patch("app.models.backends.cli_backend.subprocess.run", return_value=completed):
            response = CliBackend().invoke(CLI_PROFILE, ModelRequest(task_id="T", role="planner", prompt="p"))
        self.assertFalse(response.succeeded)
        self.assertIn("空输出", response.error)
```

- [ ] **Step 2: 运行验证失败**

Run: `cd D:/judong/code/workloop && python -m unittest discover -s tests -p "test_models.py" -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.models.backends.cli_backend'`

- [ ] **Step 3: 创建 app/models/backends/cli_backend.py**

```python
from __future__ import annotations

import subprocess
import time

from app.core.contracts import ModelProfile, ModelRequest, ModelResponse
from app.models.backends.base import ModelBackend


class CliBackend(ModelBackend):
    """通过本机 CLI 子进程调用模型；shell=False，模板仅替换 {prompt} 与 {model}。"""

    def invoke(self, profile: ModelProfile, request: ModelRequest) -> ModelResponse:
        command = [
            part.replace("{prompt}", request.prompt).replace("{model}", profile.model)
            for part in profile.command
        ]
        start = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=profile.timeout_seconds,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            return self._failure(profile, start, f"调用超时（{profile.timeout_seconds}s）。")
        except OSError as error:
            return self._failure(profile, start, f"无法启动命令：{error}")

        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            return self._failure(profile, start, f"退出码 {completed.returncode}：{stderr}")

        text = (completed.stdout or "").strip()
        if not text:
            return self._failure(profile, start, "模型返回空输出。")

        return ModelResponse(
            text=text, profile_name=profile.name, model=profile.model,
            duration_seconds=round(time.monotonic() - start, 3), succeeded=True,
        )

    def _failure(self, profile: ModelProfile, start: float, error: str) -> ModelResponse:
        return ModelResponse(
            text="", profile_name=profile.name, model=profile.model,
            duration_seconds=round(time.monotonic() - start, 3), succeeded=False, error=error,
        )
```

- [ ] **Step 4: 运行测试**

Run: `cd D:/judong/code/workloop && python -m unittest discover -s tests -p "test_models.py" -v`
Expected: 全部 PASS

---

### Task 7: distinct 策略校验

**Files:**
- Modify: `app/core/contracts.py`（PolicyBoundary 追加字段）
- Modify: `app/policy/policy_checker.py`
- Test: `tests/test_models.py`（追加）

- [ ] **Step 1: tests/test_models.py 追加失败测试**

文件顶部追加导入：

```python
from app.core.contracts import ModelRoutingConfig, PolicyBoundary
from app.policy.policy_checker import PolicyChecker
```

追加测试类：

```python
def routing_with_models(executor_model: str, reviewer_model: str) -> ModelRoutingConfig:
    profiles = {
        "e": ModelProfile(name="e", provider="fake", model=executor_model),
        "r": ModelProfile(name="r", provider="fake", model=reviewer_model),
    }
    roles = {"executor": "e", "reviewer": "r", "default": "e"}
    return ModelRoutingConfig(profiles=profiles, roles=roles)


class ModelAssignmentPolicyTest(unittest.TestCase):
    def test_same_model_for_executor_and_reviewer_is_blocked(self) -> None:
        check = PolicyChecker().check_model_assignment(
            PolicyBoundary(), routing_with_models("m-1", "m-1")
        )
        self.assertFalse(check.passed)
        self.assertIn("m-1", check.issues[0])

    def test_distinct_models_pass(self) -> None:
        check = PolicyChecker().check_model_assignment(
            PolicyBoundary(), routing_with_models("m-1", "m-2")
        )
        self.assertTrue(check.passed)

    def test_custom_distinct_groups(self) -> None:
        policy = PolicyBoundary(distinct_model_roles=[["planner", "reviewer"]])
        routing = routing_with_models("m-1", "m-1")  # planner 未配置，回落 default=e/m-1
        check = PolicyChecker().check_model_assignment(policy, routing)
        self.assertFalse(check.passed)
```

- [ ] **Step 2: 运行验证失败**

Run: `cd D:/judong/code/workloop && python -m unittest discover -s tests -p "test_models.py" -v`
Expected: FAIL — `AttributeError: 'PolicyChecker' object has no attribute 'check_model_assignment'`

- [ ] **Step 3: contracts.py 的 PolicyBoundary 追加字段（`require_human_for_conflicts` 之后）**

```python
    distinct_model_roles: list[list[str]] = field(default_factory=lambda: [["executor", "reviewer"]])
```

- [ ] **Step 4: policy_checker.py 追加方法与导入**

导入行改为：

```python
from app.core.contracts import ModelRoutingConfig, PolicyBoundary, PolicyCheck
```

类内追加（check_path 之后）：

```python
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
```

- [ ] **Step 5: 运行全量测试**

Run: `cd D:/judong/code/workloop && python -m unittest discover -s tests -v`
Expected: 全部 PASS

---

### Task 8: run_model_loop 编排主路径

**Files:**
- Modify: `app/core/workflow.py`
- Test: `tests/test_model_loop.py`（追加）

- [ ] **Step 1: tests/test_model_loop.py 追加失败测试**

文件顶部追加导入：

```python
import json

from app.core.contracts import ModelProfile, ModelRoutingConfig, PolicyBoundary
from app.models.backends.fake_backend import FakeBackend


def make_routing(planner="m-plan", executor="m-exec", reviewer="m-review") -> ModelRoutingConfig:
    profiles = {
        "p": ModelProfile(name="p", provider="fake", model=planner),
        "e": ModelProfile(name="e", provider="fake", model=executor),
        "r": ModelProfile(name="r", provider="fake", model=reviewer),
    }
    roles = {"planner": "p", "executor": "e", "reviewer": "r", "default": "e"}
    return ModelRoutingConfig(profiles=profiles, roles=roles)


def make_ready_task(kernel: WorkloopKernel):
    return kernel.create_task(
        title="循环",
        goal="验证多模型循环",
        raw_input="目标：验证多模型循环。验收标准：通过测试。",
    )
```

追加测试类：

```python
class RunModelLoopTest(unittest.TestCase):
    def test_happy_path_reaches_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = make_ready_task(kernel)
            backend = FakeBackend()
            result = kernel.run_model_loop(task.task_id, make_routing(), {"fake": backend})

            self.assertEqual(result.status, TaskStatus.DONE)
            task_dir = Path(tmp) / "tasks" / task.task_id
            self.assertTrue((task_dir / "artifacts" / "plan.md").exists())
            self.assertTrue((task_dir / "artifacts" / "execution.md").exists())
            self.assertTrue((task_dir / "artifacts" / "review.json").exists())
            self.assertTrue((task_dir / "artifacts" / "model_calls" / "1-planner" / "prompt.txt").exists())
            self.assertTrue((task_dir / "artifacts" / "model_calls" / "3-reviewer" / "meta.json").exists())
            # 三个角色各被调用一次，顺序 planner -> executor -> reviewer
            self.assertEqual([r.role for r in backend.requests], ["planner", "executor", "reviewer"])
            # 新增工件引用是相对路径
            self.assertEqual(result.artifacts["plan"], "artifacts/plan.md")

    def test_same_executor_reviewer_model_is_policy_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = make_ready_task(kernel)
            backend = FakeBackend()
            routing = make_routing(executor="m-same", reviewer="m-same")
            result = kernel.run_model_loop(task.task_id, routing, {"fake": backend})

            self.assertEqual(result.status, TaskStatus.POLICY_BLOCKED)
            self.assertEqual(backend.requests, [])  # 任何模型都不得被调用

    def test_wrong_initial_status_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = kernel.create_task(title="空", goal="g", raw_input="")  # -> POLICY_BLOCKED
            with self.assertRaisesRegex(ValueError, "ready_for_plan"):
                kernel.run_model_loop(task.task_id, make_routing(), {"fake": FakeBackend()})
```

- [ ] **Step 2: 运行验证失败**

Run: `cd D:/judong/code/workloop && python -m unittest discover -s tests -p "test_model_loop.py" -v`
Expected: FAIL — `AttributeError: 'WorkloopKernel' object has no attribute 'run_model_loop'`

- [ ] **Step 3: workflow.py 实现**

文件头导入区改为：

```python
from __future__ import annotations

import json
from pathlib import Path

from app.callbacks.event_bus import EventBus
from app.context.context_pack import ContextPackBuilder
from app.core.artifact_store import ArtifactStore
from app.core.contracts import (
    ModelRequest,
    ModelResponse,
    ModelRoutingConfig,
    PolicyBoundary,
    TaskState,
    TaskStatus,
)
from app.decision.decision_engine import DecisionEngine
from app.evaluation.evaluators import ContextEvaluator
from app.models.backends.base import ModelBackend
from app.models.router import ModelRouter
from app.policy.policy_checker import PolicyChecker

REVIEW_VERDICTS = {"pass", "revise", "block"}

PLANNER_PROMPT = "你是计划制定者。\n任务：{title}\n目标：{goal}\n上下文：\n{context}\n请输出实现该目标的分步计划。"
EXECUTOR_PROMPT = "你是执行者。\n目标：{goal}\n计划：\n{plan}\n请严格按计划产出执行结果。"
REVIEWER_PROMPT = (
    "你是独立审核者，与执行者不是同一模型。\n目标：{goal}\n计划：\n{plan}\n执行结果：\n{execution}\n"
    '请严格审核执行结果是否达成目标。只输出 JSON：{{"verdict": "pass|revise|block", "issues": ["问题列表"]}}'
)


def default_policy_boundary() -> PolicyBoundary:
    return PolicyBoundary(
        allowed_tools=["read_file", "search_code", "run_tests"],
        restricted_tools=["write_file", "db_migration"],
        forbidden_tools=["deploy_prod", "delete_data"],
        deny_paths=["**/.env", "**/secrets/**", "**/prod/**"],
    )
```

`create_task` 中原内联默认 boundary（第 24-29 行）改为：

```python
        boundary = policy or default_policy_boundary()
```

类内追加方法：

```python
    def run_model_loop(
        self,
        task_id: str,
        routing: ModelRoutingConfig,
        backends: dict[str, ModelBackend],
        policy: PolicyBoundary | None = None,
    ) -> TaskState:
        task = self.store.load_task(task_id)
        if task.status is not TaskStatus.READY_FOR_PLAN:
            raise ValueError(f"任务 {task_id} 状态为 {task.status.value}，run-loop 要求 ready_for_plan。")
        boundary = policy or default_policy_boundary()
        task_dir = self.store.task_dir(task_id)

        assignment = self.policy_checker.check_model_assignment(boundary, routing)
        self.store.write_json(task_dir / "artifacts" / "model_assignment_check.json", assignment)
        task.artifacts["model_assignment_check"] = "artifacts/model_assignment_check.json"
        if not assignment.passed:
            task.transition(TaskStatus.POLICY_BLOCKED)
            self.events.publish(task, "policy.blocked", {"issues": assignment.issues})
            self.store.save_task(task)
            self.store.append_audit(task_id, "task.updated", {"status": task.status.value})
            return task

        router = ModelRouter(routing)
        context_text = self._load_context_text(task_dir)

        plan = self._invoke_role(
            task, router, backends, "planner", 1,
            PLANNER_PROMPT.format(title=task.title, goal=task.goal, context=context_text),
        )
        if not plan.succeeded:
            return self._fail(task, "planner", plan)
        self.store.write_text(task_dir / "artifacts" / "plan.md", plan.text)
        task.artifacts["plan"] = "artifacts/plan.md"
        task.transition(TaskStatus.READY_FOR_IMPLEMENTATION)
        self.store.save_task(task)

        execution = self._invoke_role(
            task, router, backends, "executor", 2,
            EXECUTOR_PROMPT.format(goal=task.goal, plan=plan.text),
        )
        if not execution.succeeded:
            return self._fail(task, "executor", execution)
        self.store.write_text(task_dir / "artifacts" / "execution.md", execution.text)
        task.artifacts["execution"] = "artifacts/execution.md"
        task.transition(TaskStatus.VALIDATION)
        self.store.save_task(task)

        review = self._invoke_role(
            task, router, backends, "reviewer", 3,
            REVIEWER_PROMPT.format(goal=task.goal, plan=plan.text, execution=execution.text),
        )
        if not review.succeeded:
            return self._fail(task, "reviewer", review)
        self.store.write_text(task_dir / "artifacts" / "review.json", review.text)
        task.artifacts["review"] = "artifacts/review.json"

        verdict = self._parse_review(review.text)
        if verdict is None:
            task.transition(TaskStatus.CLARIFICATION_REQUIRED)
            self.events.publish(task, "review.unparseable", {"raw_prefix": review.text[:200]})
        elif verdict["verdict"] == "pass":
            task.transition(TaskStatus.DONE)
            self.events.publish(task, "review.passed", {"issues": verdict["issues"]})
        else:
            task.transition(TaskStatus.CLARIFICATION_REQUIRED)
            self.events.publish(
                task, "review.rejected",
                {"verdict": verdict["verdict"], "issues": verdict["issues"]},
            )

        self.store.save_task(task)
        self.store.append_audit(task_id, "task.updated", {"status": task.status.value})
        return task

    def _load_context_text(self, task_dir: Path) -> str:
        parts: list[str] = []
        contexts_dir = task_dir / "contexts"
        for path in sorted(contexts_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            for section in data.get("sections", []):
                parts.append(f"[{section.get('name', '')}] {section.get('content', '')}")
        return "\n".join(parts) if parts else "（无上下文）"

    def _invoke_role(
        self,
        task: TaskState,
        router: ModelRouter,
        backends: dict[str, ModelBackend],
        role: str,
        call_index: int,
        prompt: str,
    ) -> ModelResponse:
        profile, fallback = router.resolve(role)
        self.store.append_audit(
            task.task_id, "model.routed",
            {"role": role, "profile": profile.name, "model": profile.model, "fallback": fallback},
        )
        backend = backends.get(profile.provider)
        if backend is None:
            response = ModelResponse(
                text="", profile_name=profile.name, model=profile.model,
                duration_seconds=0.0, succeeded=False,
                error=f"没有可用的 {profile.provider} 后端。",
            )
        else:
            response = backend.invoke(profile, ModelRequest(task_id=task.task_id, role=role, prompt=prompt))

        call_dir = self.store.task_dir(task.task_id) / "artifacts" / "model_calls" / f"{call_index}-{role}"
        self.store.write_text(call_dir / "prompt.txt", prompt)
        self.store.write_text(call_dir / "response.txt", response.text if response.succeeded else response.error)
        self.store.write_json(call_dir / "meta.json", response)

        event_type = "model.invoked" if response.succeeded else "model.failed"
        self.events.publish(
            task, event_type,
            {"role": role, "profile": profile.name, "model": profile.model, "error": response.error},
        )
        return response

    def _fail(self, task: TaskState, role: str, response: ModelResponse) -> TaskState:
        task.transition(TaskStatus.FAILED)
        self.store.save_task(task)
        self.store.append_audit(
            task.task_id, "task.updated",
            {"status": task.status.value, "failed_role": role, "error": response.error},
        )
        return task

    def _parse_review(self, text: str) -> dict | None:
        stripped = text.strip()
        if stripped.startswith("```"):
            lines = [line for line in stripped.splitlines() if not line.strip().startswith("```")]
            stripped = "\n".join(lines).strip()
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or data.get("verdict") not in REVIEW_VERDICTS:
            return None
        return {"verdict": data["verdict"], "issues": [str(item) for item in data.get("issues", [])]}
```

- [ ] **Step 4: 运行全量测试**

Run: `cd D:/judong/code/workloop && python -m unittest discover -s tests -v`
Expected: 全部 PASS（含 Task 1-7 全部既有测试）

---

### Task 9: run_model_loop 失败路径

**Files:**
- Test: `tests/test_model_loop.py`（追加；实现已在 Task 8 就绪，本任务以测试确认各失败分支）

- [ ] **Step 1: 追加失败路径测试**

```python
class RunModelLoopFailureTest(unittest.TestCase):
    def test_executor_failure_marks_task_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = make_ready_task(kernel)
            backend = FakeBackend(failures={"executor"})
            result = kernel.run_model_loop(task.task_id, make_routing(), {"fake": backend})

            self.assertEqual(result.status, TaskStatus.FAILED)
            call_dir = Path(tmp) / "tasks" / task.task_id / "artifacts" / "model_calls" / "2-executor"
            self.assertTrue((call_dir / "meta.json").exists())  # 失败调用同样落盘
            meta = json.loads((call_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertFalse(meta["succeeded"])

    def test_reviewer_revise_requires_clarification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = make_ready_task(kernel)
            backend = FakeBackend(responses={"reviewer": '{"verdict": "revise", "issues": ["不达标"]}'})
            result = kernel.run_model_loop(task.task_id, make_routing(), {"fake": backend})
            self.assertEqual(result.status, TaskStatus.CLARIFICATION_REQUIRED)

    def test_reviewer_invalid_json_requires_clarification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = make_ready_task(kernel)
            backend = FakeBackend(responses={"reviewer": "看起来不错！"})
            result = kernel.run_model_loop(task.task_id, make_routing(), {"fake": backend})
            self.assertEqual(result.status, TaskStatus.CLARIFICATION_REQUIRED)

    def test_reviewer_json_in_code_fence_is_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = make_ready_task(kernel)
            fenced = '```json\n{"verdict": "pass", "issues": []}\n```'
            backend = FakeBackend(responses={"reviewer": fenced})
            result = kernel.run_model_loop(task.task_id, make_routing(), {"fake": backend})
            self.assertEqual(result.status, TaskStatus.DONE)

    def test_missing_backend_provider_marks_task_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = make_ready_task(kernel)
            result = kernel.run_model_loop(task.task_id, make_routing(), {})  # 无任何后端
            self.assertEqual(result.status, TaskStatus.FAILED)
```

- [ ] **Step 2: 运行测试**

Run: `cd D:/judong/code/workloop && python -m unittest discover -s tests -p "test_model_loop.py" -v`
Expected: 全部 PASS。若有分支未按预期（说明 Task 8 实现有缺口），修正 `workflow.py` 后重跑至绿。

- [ ] **Step 3: 运行全量测试**

Run: `cd D:/judong/code/workloop && python -m unittest discover -s tests -v`
Expected: 全部 PASS

---

### Task 10: CLI run-loop 子命令

**Files:**
- Modify: `app/cli.py`
- Test: `tests/test_model_loop.py`（追加）

- [ ] **Step 1: 追加失败测试**

文件顶部追加导入：

```python
from app.cli import build_backends, build_parser
```

追加测试类：

```python
class CliTest(unittest.TestCase):
    def test_run_loop_args_parse(self) -> None:
        args = build_parser().parse_args(
            ["run-loop", "--task-id", "TASK-1", "--root", "/tmp/x", "--models-config", "m.json"]
        )
        self.assertEqual(args.command, "run-loop")
        self.assertEqual(args.task_id, "TASK-1")
        self.assertEqual(args.models_config, "m.json")

    def test_build_backends_covers_both_providers(self) -> None:
        backends = build_backends()
        self.assertIn("cli", backends)
        self.assertIn("fake", backends)
```

- [ ] **Step 2: 运行验证失败**

Run: `cd D:/judong/code/workloop && python -m unittest discover -s tests -p "test_model_loop.py" -v`
Expected: FAIL — `ImportError: cannot import name 'build_backends'`

- [ ] **Step 3: 改写 app/cli.py**

```python
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
```

- [ ] **Step 4: 运行全量测试**

Run: `cd D:/judong/code/workloop && python -m unittest discover -s tests -v`
Expected: 全部 PASS

---

### Task 11: 全量回归与端到端冒烟

**Files:**
- Create: `models_smoke.json`（项目根，冒烟用，验证后可保留作配置示例）

- [ ] **Step 1: 全量测试**

Run: `cd D:/judong/code/workloop && python -m unittest discover -s tests -v`
Expected: 全部 PASS，无跳过

- [ ] **Step 2: 创建 models_smoke.json**

```json
{
  "profiles": [
    {"name": "plan-fake",   "provider": "fake", "model": "fake-plan"},
    {"name": "exec-fake",   "provider": "fake", "model": "fake-exec"},
    {"name": "review-fake", "provider": "fake", "model": "fake-review"}
  ],
  "roles": {
    "planner": "plan-fake",
    "executor": "exec-fake",
    "reviewer": "review-fake",
    "default": "exec-fake"
  }
}
```

- [ ] **Step 3: 端到端冒烟（fake 后端，离线）**

```bash
cd D:/judong/code/workloop
python -m app.cli create-task --title "冒烟" --goal "验证多模型循环" --input "目标：验证多模型循环。验收标准：通过测试。"
# 从输出 JSON 记下 task_id，替换下行 TASK-xxx
python -m app.cli run-loop --task-id TASK-xxx --models-config models_smoke.json
```

Expected: 第二条命令输出 JSON 中 `"status": "done"`；`tasks/TASK-xxx/artifacts/` 下存在 `plan.md`、`execution.md`、`review.json` 与 `model_calls/1-planner`、`2-executor`、`3-reviewer` 三个目录。

- [ ] **Step 4: distinct 拦截冒烟**

将 `models_smoke.json` 中 `review-fake` 的 `model` 改为 `"fake-exec"`（与 executor 相同），再创建一个新任务并 run-loop。
Expected: 输出 `"status": "policy_blocked"`；改回 `"fake-review"`。

- [ ] **Step 5: 真实 CLI 冒烟（可选，需要本机 claude CLI 可用）**

创建 `models.json`，三个 profile 都用 `"provider": "cli"`、command 形如 `["claude", "-p", "{prompt}", "--model", "{model}"]`，model 分别取本机可用的不同型号（如 claude-opus-4-8 / claude-sonnet-5 / claude-haiku-4-5-20251001），跑一遍 create-task + run-loop。
Expected: `"status": "done"` 或 reviewer 给出 revise 时的 `"clarification_required"`（两者都算冒烟成功——后者恰恰验证了独立审核在工作）。
