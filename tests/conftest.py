"""Small, behavior-neutral fixtures shared across the test suite."""
from __future__ import annotations

import copy
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from functools import partial
from importlib.util import find_spec
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

# ── BL-231：状态根与默认设备目标隔离（模块导入期执行）────────────────────────────
#
# 现象（2026-10-07）：测试经 `autophone.adapters.http.app.dependencies` 提交的 AI job 记录
# 写入 checkout 的 `runs/.ai/jobs/`；未注入设备替身的 RunService 用例经默认 UDS socket
# （`../vphone-cli/vm/vphone.sock`）读取真实设备（shell、app_list、截图等），本机有活
# VM 时实际连上了设备。
#
# 1. 状态根：`autophone.paths.STATE_ROOT` 在模块导入时由 `AUTOPHONE_STATE_DIR` 决定，各模块
#    又在导入期派生常量，所以必须在任何 `autophone` 导入之前设置。每个测试进程（xdist 下即
#    每个 worker）新建一个临时目录，忽略从父进程继承的值；runs/、logs/、.cache/、锁根等
#    全部落在其下，进程结束时删除（`AUTOPHONE_TEST_KEEP_STATE=1` 时保留）。仓库内的资产
#    （apps/、requirements/、docs/ 等）仍按 `CODE_ROOT` 只读解析，不受影响。
# 2. 默认设备目标：`VPHONE_CLI_DIR` 指向状态根下不存在的目录，清掉 `VM_DIR`、`VPHONE_SOCK`、
#    `AP_DEVICE` 与 Frida 端点变量。未注入替身的路径因此连接一个不存在的 socket，结果与没有
#    vphone-cli 的 CI 相同（FileNotFoundError），设备键仍为默认的 vm。子进程继承同一环境。
# 3. 进程内守卫（见下文 `_install_real_device_guard`）：显式指向真实设备 socket、Frida 端口
#    或宿主控制面/看板端口的连接一律拒绝并记账。
#
# 原始值记在 `AUTOPHONE_TEST_ORIGINAL_DEVICE_ENV`（只在首个进程写入），守卫据此识别开发者
# shell 里指向真实设备的值。
_DEVICE_ENV_KEYS = (
    "VPHONE_CLI_DIR", "VM_DIR", "VPHONE_SOCK", "AP_DEVICE",
    "VPHONE_LIBRARY_ROOT", "AUTOPHONE_VPHONE_PROTOCOL", "AUTOPHONE_CORE_BUNDLE",
    "AUTOPHONE_FRIDA_HOST", "AP_FRIDA_HOST", "AUTOPHONE_FRIDA_HOST_PORT", "AUTOPHONE_SSH_PORT",
)
_ORIGINAL_DEVICE_ENV_KEY = "AUTOPHONE_TEST_ORIGINAL_DEVICE_ENV"
if _ORIGINAL_DEVICE_ENV_KEY not in os.environ:
    os.environ[_ORIGINAL_DEVICE_ENV_KEY] = json.dumps(
        {key: os.environ[key] for key in _DEVICE_ENV_KEYS if os.environ.get(key)}
    )
# 放在 /tmp 下：默认设备 socket 路径（`<状态根>/vphone-cli-absent/vm/vphone.sock`）须短于
# AF_UNIX 的约 104 字节上限，连接失败方式才与 CI 相同（FileNotFoundError 而非 path too long）。
TEST_STATE_ROOT = Path(
    tempfile.mkdtemp(prefix="autophone-test-state-", dir="/tmp" if os.path.isdir("/tmp") else None)
).resolve()
os.environ["AUTOPHONE_STATE_DIR"] = str(TEST_STATE_ROOT)
for _key in _DEVICE_ENV_KEYS:
    os.environ.pop(_key, None)
os.environ["VPHONE_CLI_DIR"] = str(TEST_STATE_ROOT / "vphone-cli-absent")

# 已克隆进测试状态根的 checkout 状态目录（相对路径 → 目标），每进程一次。
_CHECKOUT_STATE_CLONES: dict[str, Path] = {}


def clone_checkout_state(relative: str) -> Path:
    """把 checkout 状态根下的一个目录（如 `requirements`、`packages`）克隆进测试状态根。

    BL-231：生产默认 `STATE_ROOT == CODE_ROOT`，需求单、包仓库等既是状态也是入库资产。
    需要断言这些既有数据的用例读克隆，写入只落在测试状态根，checkout 保持只读。
    macOS 用 APFS 克隆（`cp -c`），其他平台用 `cp --reflink=auto`，失败时退回完整复制。
    """
    from autophone.paths import CODE_ROOT

    relative = str(Path(relative))
    if relative in _CHECKOUT_STATE_CLONES:
        return _CHECKOUT_STATE_CLONES[relative]
    source = CODE_ROOT / relative
    target = TEST_STATE_ROOT / relative
    target.mkdir(parents=True, exist_ok=True)
    if not source.is_dir():
        # 公开快照不携带历史需求单等状态目录；用例得到一个空的状态目录。
        _CHECKOUT_STATE_CLONES[relative] = target
        return target
    flags = ["-cR"] if sys.platform == "darwin" else ["-R", "--reflink=auto"]
    completed = subprocess.run(
        ["cp", *flags, f"{source}/.", str(target)], capture_output=True, text=True, check=False,
    )
    if completed.returncode != 0:
        shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True)
    _CHECKOUT_STATE_CLONES[relative] = target
    return target


@pytest.fixture(scope="session")
def checkout_state():
    """返回 `clone_checkout_state`：用例按需把 checkout 状态目录克隆进测试状态根后只读断言。"""
    return clone_checkout_state

def pytest_addoption(parser):
    parser.addoption(
        "--local-third-party-profiles",
        action="store_true",
        help="Run the three App Profile contract cases against the original checkout profiles.",
    )


@pytest.fixture
def third_party_profiles(pytestconfig, monkeypatch):
    from autophone.assets import apps

    tests_dir = Path(__file__).resolve().parent
    if pytestconfig.getoption("--local-third-party-profiles"):
        root = tests_dir.parent
        profiles = {"maps": ("apple-maps", "com.apple.Maps"), "im": ("chat", "com.example.chat")}
    else:
        root = tests_dir / "fixtures" / "public-app-profiles"
        profiles = {"maps": ("sample-maps", "org.example.autophone.maps"), "im": ("sample-im", "org.example.autophone.im")}
    monkeypatch.setattr(apps, "load_app_profile", partial(apps.load_app_profile, root=root))
    return profiles


# ── 测试分层：互斥的主层级 marker（BL-080 / test-suite-inventory.md 清理项 5）──────
#
# 五个主层级在 pyproject.toml 的 `markers` 里登记，**每个 collection item 恰好一个**，
# 由下面的 `pytest_collection_modifyitems` 自动补齐，`tests/test_layer_markers.py` 守卫。
# 手工 `@pytest.mark.<layer>` 优先于自动规则——自动规则只在该 item 一个主层级都没有时
# 才生效，所以给某个文件或某条用例单独定层不需要改这里的表。
#
# 归类依据是 docs/test-suite-inventory.md「当前 pytest 分层审计」的四类边界，加上真实
# 设备一类。**匹配对象是测试模块的文件名（stem）**，不是内容：文件名是这套测试库里唯一
# 稳定且可机械核对的归类信号，内容级判定（是否起线程、是否碰 HTTP）无法在收集期得到。
# 该文档自述分类置信度为中等，所以这里的 token 表刻意保守——**只收高置信度的词根，
# 其余一律落回 `unit`**（该表里单元测试本就占 54%，默认值取多数类），宁可少判一层，
# 不为凑分布硬塞规则。
#
# `device_e2e` 不看文件名：它由 `live_device` / `live_frida` 推出，因此
# `-m "not live_device and not live_frida"` 与 `-m "not device_e2e"` 排除的是同一批
# item，CI 命令不必改写。
_LAYER_MARKERS = ("unit", "contract", "integration", "flow", "device_e2e")
_DEVICE_MARKERS = ("live_device", "live_frida")

_LAYER_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # 契约、Schema、静态资源与注册表完整性：断言的是约定本身，不驱动组件协作。
    (
        "contract",
        (
            "contract",
            "schema",
            "registry_integrity",
            "registry_lint",
            "route_coverage",
            "import_ratchet",
            "entrypoint_boundaries",
            "document_status_index",
            "retention_guard",
            "readonly_assets",
            "reason_catalog",
            "telemetry_dictionary",
            "taxonomy",
            "recipe",
        ),
    ),
    # 跨阶段流程与模拟 E2E：设备与外部服务由 fake / 临时目录 / dry-run 替换，
    # 但断言跨越多个阶段边界。
    (
        "flow",
        (
            "workflow",
            "journey",
            "delivery_loop",
            "repair_loop",
            "complete_flow",
            "smoke_flows",
            "lifecycle",
            "phase_a",
            "phase_b",
            "phase_c",
            "phase_d",
            "phase_e",
            "multicase_schedule",
            "replay",
        ),
    ),
    # 宿主组件集成：HTTP / CLI / 后台线程 / 本机文件系统与子进程。
    (
        "integration",
        (
            "_api",
            "api_",
            "_cli",
            "cli_",
            "http",
            "dashboard",
            "control_plane",
            "scheduler",
            "supervisor",
            "daemon",
            "_script",
            "_scripts",
            "rig_agent",
            "session",
        ),
    ),
)

# 本次 collection 的最终 item 列表（已过 `-m` 去选）。守卫测试读它。
_collected_items: list[pytest.Item] = []


def _declared_layers(item: pytest.Item) -> list[str]:
    return [name for name in _LAYER_MARKERS if item.get_closest_marker(name) is not None]


def _auto_layer(item: pytest.Item) -> str:
    if any(item.get_closest_marker(name) is not None for name in _DEVICE_MARKERS):
        return "device_e2e"
    stem = Path(str(item.reportinfo()[0])).stem.lower()
    for layer, tokens in _LAYER_RULES:
        if any(token in stem for token in tokens):
            return layer
    return "unit"


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """给每个 item 补一个主层级 marker。

    `tryfirst` 是必需的：pytest 内建的 `-m` 去选也在 `pytest_collection_modifyitems`
    里做，本钩子必须跑在它之前，否则 `-m unit` 这类选择在过滤时还看不到 marker。
    多于一个主层级时这里不动手（也不抛错，抛错会让整轮收集失败），交给守卫测试报。
    """
    for item in items:
        if not _declared_layers(item):
            item.add_marker(_auto_layer(item))


def pytest_collection_finish(session: pytest.Session) -> None:
    _collected_items.clear()
    _collected_items.extend(session.items)


@pytest.fixture(scope="session")
def collected_items() -> list[pytest.Item]:
    """本轮收集到的全部 item。仅供分层守卫测试使用。"""
    return _collected_items


@pytest.fixture(scope="session", autouse=True)
def platform_composition_root() -> None:
    """把生产装配推进目标包（M8 切片 5 / BL-008）。

    `autophone/workflow/` 内的模块不 import `autophone.platform`（ratchet R1），
    BuildResolver 与 AI provider 两个端口由平台侧组合根
    `autophone.application.delivery_composition` 在模块体里登记。生产入口（`cli.py`、
    `platform/api.py`、`application/delivery_launch.py`）都会经过它；测试直接 import
    目标包时没有这一步，所以在这里做同一件事，让测试与生产的装配一致。
    未登记时目标包 fail-closed（`build_resolver_not_registered` /
    `AiProviderNotRegistered`），契约测试单独覆盖那条路径。
    """
    if find_spec("autophone.application.delivery_composition") is not None:
        import autophone.application.delivery_composition  # noqa: F401


# ── 夹具仓库与父进程 git 环境的隔离 ─────────────────────────────────────────────
#
# 这些变量一旦从父进程继承进来，`subprocess.run(["git", ...], cwd=tmp)` 里的 `cwd`
# 就不再决定 git 作用于哪个仓库。清理集合取 git 用来定位仓库、索引与对象库的那几个，
# 不含 `GIT_AUTHOR_*` / `GIT_COMMITTER_*`（现有测试自己设它们造确定性提交）。
_GIT_LOCATION_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
    "GIT_REFLOG_ACTION",
    "GIT_CEILING_DIRECTORIES",
)


@pytest.fixture(autouse=True)
def isolate_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清掉父进程的 git 定位类环境变量，让夹具仓库只由 `cwd` 决定。

    根因：`git rebase --exec <cmd>` 给子命令导出 `GIT_DIR=<repo>/.git/worktrees/<wt>`
    且 `GIT_WORK_TREE` 为空。测试套件多处用
    `subprocess.run(["git", "init", ...], cwd=tmp_path, env=None)` 建夹具仓库，继承该
    `GIT_DIR` 后，任何非 bare 的 `git init`（无参、`-b main`、`git init <dir>` 表现一致）
    不初始化目标目录，而是**重初始化 `GIT_DIR` 指向的那个真仓库**，并把 `bare = true`
    写进 linked worktree 共用的主 `.git/config`。主检出随后所有 git 命令报
    `fatal: this operation must be run in a work tree`。

    因此在**每条用例**开始前清掉这些变量：
    - `env=None` 的子进程直接继承已清理的 `os.environ`；
    - 夹具用 `env={**os.environ, ...}` 显式传的 dict 也在本 fixture 之后才拷贝
      （全套测试的 `os.environ` 拷贝都发生在用例调用期，没有 import 期或
      module 级快照），拷到的同样是已清理的值。

    `GIT_CONFIG_GLOBAL` / `GIT_CONFIG_SYSTEM` 不在清理集合内：它们指向配置文件而非
    仓库，删掉会让本机与 CI 的 git 配置来源不同；本 fixture 也不新设它们。
    """
    for name in _GIT_LOCATION_ENV:
        monkeypatch.delenv(name, raising=False)


# ── 共享 Screen Lock 根守卫（7b7dcc16 / 9567c159 的仓库级收口）──────────────────
#
# `ScreenLock(root=None)` 落到 `default_screen_lock_root()`。那是主 checkout 的
# `runs/.locks`，**故意**让 linked worktree 与主 checkout 共用同一组设备锁（见
# `autophone/device/lock.py`）。生产上这是对的：一台设备只能有一个持有者。测试里则
# 相反——`RunService(run_root=tmp_path)` 不传 `lock=` 时就在那个共享目录上读写锁状态，
# 于是别的会话、别的 worktree、以及同一台机器上每个 xdist worker 互相干扰，用例凭空变红，
# 仓库树里也多出 `runs/.locks/*`。
#
# 判据是「访问为 0」。BL-122 之后 `ScreenLock.__init__` 不再建目录，构造与访问就此分开，
# 所以守卫也分两层，各自覆盖不同的窗口：
#
# 1. **构造层**（autouse 夹具 `forbid_shared_screen_lock_root`，只在用例的
#    setup/call/teardown 期间生效）：用例连「在共享锁根上构造 ScreenLock」都不允许。
#    这比「访问为 0」严，理由是构造处能指名是哪条用例，而访问可能发生在后台线程或
#    子进程里；用例侧一律走显式注入或 `isolated_screen_lock_root`，不需要这个自由度。
#    唯一的例外是 **import 期模块体里的构造**（`_screen_lock_constructed_during_import`）：
#    那是每进程一次的模块级单例，归责对象不是「恰好第一个 import 它的用例」。见该函数的
#    docstring。
# 2. **访问层**（`pytest_configure` 里永久装上的 `ScreenLock._ensure_root` 包装，覆盖
#    **整个进程**，含收集期、会话钩子与后台线程）：任何代码都不得在共享锁根上建目录。
#    收集期的 import 副作用（`autophone.adapters.http.app` 模块体的 `app = create_app()`
#    构造 `RunService()` → `ScreenLock()`）不在夹具窗口内，只能靠这一层兜住。
#
# 两层都被 `allow_shared_screen_lock_root` 夹具放行：确实要断言共享锁根本身的用例
# （`tests/test_screen_lock.py` 里那条）取用它退出守卫。
#
# 访问层只拦「建目录」，不拦只读查询：`status()` / `recording_holder()` 在锁根不存在时
# 走 `path.exists()` 的 False 分支返回「无锁」，既不建目录也不在仓库树里留下东西。
_SHARED_SCREEN_LOCK_ROOT: Path | None = None
# 用例窗口之外（收集期、会话钩子、后台线程）的违规记在这个会话级账本里；用例期间由
# autouse 夹具换成该用例自己的账本，好在失败信息里指名用例。
_SCREEN_LOCK_VIOLATIONS: list[str] = []
# 取用 `allow_shared_screen_lock_root` 的用例在其窗口内把两层守卫都放行。
_SCREEN_LOCK_GUARD_ALLOWED = False


def _screen_lock_constructed_during_import() -> bool:
    """当前 `ScreenLock` 构造是否发生在某个模块的 import 期（即模块体里）。

    判断依据是调用栈里有没有 import 机器的帧：`import x` 走
    `importlib._bootstrap._find_and_load` → `_load_unlocked` → `exec_module` →
    `_call_with_frames_removed(exec, code, module.__dict__)` → 模块体。这些帧只是**不进
    traceback**，在运行栈上是真实存在的，所以「栈里有 importlib._bootstrap 帧」等价于
    「此刻正在执行某个模块体」。普通的用例调用链里没有这种帧。

    构造层用它排除模块级单例：`autophone/adapters/http/app.py` 模块体的
    `app = create_app()` 会在默认锁根上构造一次 `ScreenLock`（BL-122 之后不建目录）。
    这次构造每进程只发生一次，落在哪条用例身上取决于本次跑选了哪些文件——先 import 它的
    文件在收集期就执行完模块体（`tests/test_route_coverage.py` 等 20 多个文件是模块级
    import），只有当本次跑的文件全都是在用例体内 import 它时，才轮到某条用例背这笔账。
    所以它不是「用例自己建的锁」，归责给用例会让同一条用例单跑红、全量跑绿。
    判据仍是「访问为 0」：这类构造由访问层（`_ensure_root` 包装）继续覆盖，一旦它真去
    共享锁根上建目录，无论在 import 期还是用例期都照样违规。
    """
    frame: Any = sys._getframe()
    while frame is not None:
        filename = frame.f_code.co_filename
        if filename.startswith("<frozen importlib._bootstrap"):
            return True
        if "/importlib/_bootstrap" in filename.replace(os.sep, "/"):
            # 非 frozen 的 importlib（少数发行版/调试构建）走这一支。
            return True
        frame = frame.f_back
    return False


def _shared_screen_lock_root_violation(resolved: Path, what: str) -> AssertionError:
    where = os.environ.get("PYTEST_CURRENT_TEST") or "<用例窗口之外：收集期 / 会话钩子 / 后台线程>"
    _SCREEN_LOCK_VIOLATIONS.append(str(resolved))
    return AssertionError(
        f"{where} 在主 checkout 的共享 Screen Lock 根上{what}：{resolved}。"
        f"测试必须把锁根隔离到 tmp_path（见 tests/conftest.py 里本守卫的 docstring）；"
        f"确实要断言共享锁根的用例取用 allow_shared_screen_lock_root 夹具。"
    )


def _install_shared_screen_lock_root_access_guard() -> None:
    """在 `ScreenLock._ensure_root` 上装进程级守卫，覆盖收集期在内的整个进程。

    装在 `_ensure_root` 而不是 `__init__`：BL-122 之后构造本身无副作用，而
    `autophone.adapters.http.app` 模块体在收集期仍会构造一个默认锁根的 `ScreenLock`
    （只是不再建目录）。拦构造会把这条合法 import 判成违规，拦建目录才对准判据。
    """
    from autophone.device import lock as lock_module

    original = lock_module.ScreenLock._ensure_root
    if getattr(original, "_shared_root_guarded", False):
        return

    def guarded(self: "object") -> Path:
        root = Path(self.root)  # type: ignore[attr-defined]
        if (
            not _SCREEN_LOCK_GUARD_ALLOWED
            and _SHARED_SCREEN_LOCK_ROOT is not None
            and root == _SHARED_SCREEN_LOCK_ROOT
        ):
            raise _shared_screen_lock_root_violation(root, "建了锁目录")
        return original(self)

    guarded._shared_root_guarded = True  # type: ignore[attr-defined]
    lock_module.ScreenLock._ensure_root = guarded


def pytest_configure(config: pytest.Config) -> None:
    """在任何用例 patch `STATE_ROOT` 之前，把真实共享锁根算出来存下。

    `default_screen_lock_root()` 每次调用都重算，且 `tests/test_screen_lock.py`
    会把 `lock.STATE_ROOT` 指到 tmp 去验证各种 checkout 布局。守卫要比的是**本机
    真实的**那个目录，所以只在这里取一次。

    BL-231 之后测试进程设置了 `AUTOPHONE_STATE_DIR`，默认锁根是本进程测试状态根下的
    `runs/.locks`（不再落到主 checkout）；守卫仍禁止在这个默认锁根上建锁，用例照旧显式注入
    或取用 `isolated_screen_lock_root`。
    """
    global _SHARED_SCREEN_LOCK_ROOT
    from autophone.device.lock import default_screen_lock_root

    from autophone import paths

    if paths.STATE_ROOT != TEST_STATE_ROOT:
        # 有模块在本 conftest 之前导入了 autophone（例如另一个先加载的插件）：状态根已定，
        # 隔离无法生效，不能继续往 checkout 写。
        raise pytest.UsageError(
            f"BL-231：autophone 在 tests/conftest.py 之前已导入，STATE_ROOT={paths.STATE_ROOT}，"
            f"期望测试状态根 {TEST_STATE_ROOT}"
        )
    _SHARED_SCREEN_LOCK_ROOT = default_screen_lock_root()
    _install_shared_screen_lock_root_access_guard()
    _install_real_ai_cli_guard()
    _install_real_device_guard()


def pytest_unconfigure(config: pytest.Config) -> None:
    """删除本进程的测试状态根（BL-231）。"""
    if os.environ.get("AUTOPHONE_TEST_KEEP_STATE", "").strip() not in {"1", "true", "yes"}:
        shutil.rmtree(TEST_STATE_ROOT, ignore_errors=True)


# ── BL-229：测试进程不得拉起真实 AI CLI ─────────────────────────────────────
#
# 现象（2026-10-07）：python 道运行期间进程表出现由测试拉起的真实
# `claude --bare --safe-mode -p …`。来源是 `tests/test_ai_ask_api.py` 中不等待后台 job
# 结束的用例：用例返回后 monkeypatch 撤销，job 线程再调用到真实 `ai_explore.explore`，
# 经 `autophone.application.ai.invoke` 用 `subprocess.Popen` 拉起 PATH 上的 `claude`。
#
# 守卫分两层，都只改测试进程，不改 `autophone/` 运行代码：
#   1. 进程内：包装 `subprocess.Popen.__init__`，可执行文件名属于 AI backend
#      （`ai._BACKENDS` 的 `binary`）或为 ChatGPT.app 内置 codex 路径时，在创建进程前抛
#      AssertionError，并记入账本；用例结束时账本非空即判失败。后台线程里的调用会被
#      调用方的 fail-closed 分支吞掉异常，所以必须靠账本收口。
#   2. 子进程：把 `tests/ai_cli_guard/` 前置到 PATH。测试拉起的其他进程（CLI 子进程等）
#      按名字查找 claude / codex 时解析到占位脚本，占位脚本立即以 126 退出并说明原因。
#      浏览器回归的 dashboard 由 `tests/e2e/server-common.mjs` 做同样的前置。
#
# 不在 `ai.py` 加测试模式开关：那样要改运行代码并重启常驻进程，且守卫依赖运行代码配合；
# 本守卫与上面的共享 Screen Lock 根守卫同一形态（进程级安装 + 用例级账本）。
# 需要覆盖 AI 调用路径的用例一律注入 runner 替身（`ai.invoke(req, runner=fake)`）或
# monkeypatch 上层入口；后台 job 须在 monkeypatch 撤销前跑完。仓库没有真实 AI 调用的
# marker；以后若新增，须同时给出退出本守卫的显式夹具，并在 CI 命令中默认排除。
AI_CLI_GUARD_DIR = Path(__file__).resolve().parent / "ai_cli_guard"
# 用例窗口之外的调用记在会话账本；用例期间由 autouse 夹具换成该用例自己的账本。
_AI_CLI_LAUNCHES: list[str] = []


def _real_ai_cli_target(args: Any, rest: tuple[Any, ...], kwargs: dict[str, Any]) -> str | None:
    """返回本次 Popen 要执行的 AI CLI 路径；不是 AI CLI 时返回 None。"""
    from autophone.application import ai

    names = {backend.binary for backend in ai._BACKENDS.values()}
    candidates: list[str] = []
    executable = kwargs.get("executable", rest[1] if len(rest) > 1 else None)
    if executable:
        candidates.append(os.fsdecode(executable))
    if isinstance(args, (str, bytes, os.PathLike)):
        text = os.fsdecode(args)
        if kwargs.get("shell"):
            try:
                candidates.extend(shlex.split(text)[:1])
            except ValueError:
                candidates.extend(text.split()[:1])
        else:
            candidates.append(text)
    else:
        try:
            candidates.append(os.fsdecode(next(iter(args))))
        except (StopIteration, TypeError):
            pass
    for candidate in candidates:
        if Path(candidate).name in names or candidate == ai.CHATGPT_APP_CODEX_BINARY:
            return candidate
    return None


def _real_ai_cli_violation(target: str, args: Any) -> AssertionError:
    where = os.environ.get("PYTEST_CURRENT_TEST") or "<用例窗口之外：收集期 / 会话钩子 / 后台线程>"
    try:
        argv = shlex.join(os.fsdecode(a) for a in args) if not isinstance(args, (str, bytes)) else os.fsdecode(args)
    except TypeError:
        argv = repr(args)
    record = f"{where} [线程 {threading.current_thread().name}] {argv[:160]}"
    _AI_CLI_LAUNCHES.append(record)
    return AssertionError(
        f"BL-229：测试进程拉起真实 AI CLI 被拒绝：{target}（{record}）。"
        f"测试须注入 runner 替身或 monkeypatch 上层入口；后台 job 须在 monkeypatch 撤销前跑完。"
        f"线程名不是 MainThread 时，调用可能来自更早用例遗留的后台 job。"
    )


def _install_real_ai_cli_guard() -> None:
    """装进程级守卫：Popen 拦截 AI CLI，PATH 前置占位目录供子进程使用。"""
    original = subprocess.Popen.__init__
    if getattr(original, "_real_ai_cli_guarded", False):
        return

    def guarded(self: subprocess.Popen, args: Any, *rest: Any, **kwargs: Any) -> None:
        target = _real_ai_cli_target(args, rest, kwargs)
        if target is not None:
            raise _real_ai_cli_violation(target, args)
        original(self, args, *rest, **kwargs)

    guarded._real_ai_cli_guarded = True  # type: ignore[attr-defined]
    subprocess.Popen.__init__ = guarded  # type: ignore[method-assign]
    path = os.environ.get("PATH", "")
    if not path.startswith(f"{AI_CLI_GUARD_DIR}{os.pathsep}"):
        os.environ["PATH"] = f"{AI_CLI_GUARD_DIR}{os.pathsep}{path}"


@pytest.fixture(autouse=True)
def forbid_real_ai_cli(request: pytest.FixtureRequest):
    """用例期间拉起真实 AI CLI 即判失败（BL-229）；守卫安装见 `_install_real_ai_cli_guard`。"""
    global _AI_CLI_LAUNCHES
    launches: list[str] = []
    # 守卫自检用例（tests/test_real_ai_cli_guard.py）故意触发后清空这个列表。
    request.node.real_ai_cli_launches = launches
    session_ledger = _AI_CLI_LAUNCHES
    _AI_CLI_LAUNCHES = launches
    try:
        yield
    finally:
        _AI_CLI_LAUNCHES = session_ledger
    assert not launches, f"{request.node.nodeid} 期间拉起真实 AI CLI 被拒绝 {len(launches)} 次：{launches}"


# ── BL-231：测试进程不得连接真实设备 ─────────────────────────────────────────
#
# 与 BL-229 的 AI CLI 守卫同一形态（进程级安装 + 用例级账本 + sessionfinish/xdist 汇总）：
#   - `socket.socket.connect` / `connect_ex`：AF_UNIX 地址解析后位于真实 vphone-cli 目录下
#     （`autophone.device.target._default_cli_dir()` 与开发者 shell 原有的 `VPHONE_CLI_DIR`），
#     或等于原有的 `VPHONE_SOCK`；TCP 地址为回环地址且端口属于真实设备与宿主服务端口
#     （SSH 2222/2322、Frida 27042/27142、控制面 8100、看板 8000，以及原有环境里的端口）。
#     `autophone.device.uds.request` 与 `httpx`/`urllib`/asyncio 的连接都经过这里。
#   - `frida.get_device_manager().add_remote_device` / `get_remote_device`：Frida 的连接在
#     原生代码里建立，不经 Python socket，单独包装；端口规则同上（未写端口时按 27042）。
# 拒绝方式是在建立连接前抛 AssertionError 并记账；用例结束时账本非空即判失败（后台线程里
# 的异常可能被调用方吞掉）。测试自建的临时 socket（tmp 路径下）与其他端口不受影响。
# 标记 `live_device` / `live_frida` 的用例豁免（CI 命令默认排除它们）。
# 未覆盖：子进程内的连接（子进程只继承上面重定向后的默认目标），以及 ssh/scp 等外部命令。
_REAL_DEVICE_PORTS = frozenset({2222, 2322, 27042, 27142, 8000, 8100})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0", "::", ""})
_REAL_DEVICE_DIRS: tuple[Path, ...] = ()
_REAL_DEVICE_SOCKETS: frozenset[Path] = frozenset()
_DEVICE_PORTS: frozenset[int] = _REAL_DEVICE_PORTS
# 用例窗口之外的记录记在会话账本；用例期间由 autouse 夹具换成该用例自己的账本。
_DEVICE_CONNECTS: list[str] = []
_WORKER_DEVICE_CONNECTS: list[str] = []
# 标记 live_device / live_frida 的用例在其窗口内放行。
_DEVICE_GUARD_ALLOWED = False


def _resolve_quietly(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except (OSError, RuntimeError):
        return path


def _compute_real_device_endpoints() -> None:
    global _REAL_DEVICE_DIRS, _REAL_DEVICE_SOCKETS, _DEVICE_PORTS
    from autophone.device.target import _default_cli_dir

    original = json.loads(os.environ.get(_ORIGINAL_DEVICE_ENV_KEY) or "{}")
    dirs = {_resolve_quietly(_default_cli_dir())}
    home = Path.home()
    dirs.update(_resolve_quietly(home / name) for name in ("github/vphone-cli", "Documents/github/vphone-cli"))
    if original.get("VPHONE_CLI_DIR"):
        dirs.add(_resolve_quietly(Path(original["VPHONE_CLI_DIR"])))
    sockets = set()
    if original.get("VPHONE_SOCK"):
        sockets.add(_resolve_quietly(Path(original["VPHONE_SOCK"])))
    ports = set(_REAL_DEVICE_PORTS)
    for key in ("AUTOPHONE_FRIDA_HOST_PORT", "AUTOPHONE_SSH_PORT"):
        if str(original.get(key) or "").isdigit():
            ports.add(int(original[key]))
    for key in ("AUTOPHONE_FRIDA_HOST", "AP_FRIDA_HOST"):
        port = str(original.get(key) or "").rpartition(":")[2]
        if port.isdigit():
            ports.add(int(port))
    _REAL_DEVICE_DIRS = tuple(sorted(dirs))
    _REAL_DEVICE_SOCKETS = frozenset(sockets)
    _DEVICE_PORTS = frozenset(ports)


def _real_device_target(family: int, address: Any) -> str | None:
    """返回被拒绝的连接目标描述；不是真实设备端点时返回 None。"""
    if family == getattr(socket, "AF_UNIX", None) and isinstance(address, (str, bytes, os.PathLike)):
        raw = os.fsdecode(address)
        if not raw:
            return None
        resolved = _resolve_quietly(Path(raw))
        if resolved in _REAL_DEVICE_SOCKETS or any(
            resolved == root or root in resolved.parents for root in _REAL_DEVICE_DIRS
        ):
            return raw
        return None
    if family in (socket.AF_INET, socket.AF_INET6) and isinstance(address, tuple) and len(address) >= 2:
        host, port = str(address[0]).lower(), address[1]
        if host in _LOOPBACK_HOSTS and isinstance(port, int) and port in _DEVICE_PORTS:
            return f"{host}:{port}"
    return None


def _frida_host_is_real(host: str) -> bool:
    text = str(host or "").strip()
    if text.startswith("["):
        name, _, rest = text[1:].partition("]")
        port_text = rest.lstrip(":")
    elif text.count(":") == 1:
        name, _, port_text = text.partition(":")
    else:
        name, port_text = text, ""
    port = int(port_text) if port_text.isdigit() else 27042
    return name.lower() in _LOOPBACK_HOSTS and port in _DEVICE_PORTS


def _real_device_violation(kind: str, target: str) -> AssertionError:
    where = os.environ.get("PYTEST_CURRENT_TEST") or "<用例窗口之外：收集期 / 会话钩子 / 后台线程>"
    record = f"{where} [线程 {threading.current_thread().name}] {kind} {target}"
    _DEVICE_CONNECTS.append(record)
    return AssertionError(
        f"BL-231：测试进程连接真实设备被拒绝：{kind} {target}（{record}）。"
        f"测试须注入设备替身（fake request / session / runner）或使用 tmp 路径下的 socket；"
        f"需要真实设备的用例标记 live_device / live_frida。"
    )


class _GuardedFridaDeviceManager:
    """Frida DeviceManager 的代理：远端设备连接按真实端点规则拒绝，其余方法原样转发。"""

    def __init__(self, manager: Any) -> None:
        self._manager = manager

    def __getattr__(self, name: str) -> Any:
        return getattr(self._manager, name)

    def add_remote_device(self, address: str, *args: Any, **kwargs: Any) -> Any:
        if not _DEVICE_GUARD_ALLOWED and _frida_host_is_real(address):
            raise _real_device_violation("frida", str(address))
        return self._manager.add_remote_device(address, *args, **kwargs)

    def get_remote_device(self, *args: Any, **kwargs: Any) -> Any:
        if not _DEVICE_GUARD_ALLOWED:
            raise _real_device_violation("frida", "127.0.0.1:27042")
        return self._manager.get_remote_device(*args, **kwargs)


def _install_real_device_guard() -> None:
    """装进程级守卫：socket 连接与 Frida 远端设备连接。"""
    _compute_real_device_endpoints()
    original_connect = socket.socket.connect
    if not getattr(original_connect, "_real_device_guarded", False):
        original_connect_ex = socket.socket.connect_ex

        def connect(self: socket.socket, address: Any) -> None:
            target = None if _DEVICE_GUARD_ALLOWED else _real_device_target(self.family, address)
            if target is not None:
                raise _real_device_violation("socket", target)
            return original_connect(self, address)

        def connect_ex(self: socket.socket, address: Any) -> int:
            target = None if _DEVICE_GUARD_ALLOWED else _real_device_target(self.family, address)
            if target is not None:
                raise _real_device_violation("socket", target)
            return original_connect_ex(self, address)

        connect._real_device_guarded = True  # type: ignore[attr-defined]
        socket.socket.connect = connect  # type: ignore[method-assign]
        socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    try:
        import frida
    except ImportError:
        return
    original_manager = frida.get_device_manager
    if getattr(original_manager, "_real_device_guarded", False):
        return

    def get_device_manager() -> Any:
        return _GuardedFridaDeviceManager(original_manager())

    get_device_manager._real_device_guarded = True  # type: ignore[attr-defined]
    frida.get_device_manager = get_device_manager


@pytest.fixture(scope="session")
def real_device_endpoints() -> dict[str, Any]:
    """守卫判定用的真实设备端点（守卫自检用）。"""
    return {
        "dirs": _REAL_DEVICE_DIRS,
        "sockets": _REAL_DEVICE_SOCKETS,
        "ports": _DEVICE_PORTS,
        "state_root": TEST_STATE_ROOT,
    }


@pytest.fixture(autouse=True)
def forbid_real_device_connect(request: pytest.FixtureRequest):
    """用例期间连接真实设备即判失败（BL-231）；live_device / live_frida 用例豁免。"""
    global _DEVICE_CONNECTS, _DEVICE_GUARD_ALLOWED
    connects: list[str] = []
    # 守卫自检用例（tests/test_real_device_guard.py）故意触发后清空这个列表。
    request.node.real_device_connects = connects
    allowed = any(request.node.get_closest_marker(name) is not None for name in _DEVICE_MARKERS)
    session_ledger = _DEVICE_CONNECTS
    _DEVICE_CONNECTS = connects
    _DEVICE_GUARD_ALLOWED = allowed
    try:
        yield
    finally:
        _DEVICE_CONNECTS = session_ledger
        _DEVICE_GUARD_ALLOWED = False
    assert not connects, f"{request.node.nodeid} 期间连接真实设备被拒绝 {len(connects)} 次：{connects}"


# xdist 主进程从各 worker 收回的窗口外记录（`pytest_testnodedown`）。
_WORKER_AI_CLI_LAUNCHES: list[str] = []


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any, error: Any) -> None:
    """xdist 主进程：收回 worker 的窗口外 AI CLI 记录，交给本进程的 sessionfinish 判定。"""
    output = getattr(node, "workeroutput", {})
    _WORKER_AI_CLI_LAUNCHES.extend(output.get("real_ai_cli_launches", []))
    _WORKER_DEVICE_CONNECTS.extend(output.get("real_device_connects", []))


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """用例窗口之外（收集期、会话级夹具收尾、用例间隙的后台线程）的 AI CLI 调用使本轮失败。

    xdist worker 的退出码不决定整轮结果，所以 worker 把记录经 `workeroutput` 交给主进程。
    解释器退出阶段（sessionfinish 之后）的调用仍被 Popen 守卫拒绝，但不再计入结果。
    """
    launches = list(_AI_CLI_LAUNCHES)
    connects = list(_DEVICE_CONNECTS)
    workeroutput = getattr(session.config, "workeroutput", None)
    if workeroutput is not None:
        workeroutput["real_ai_cli_launches"] = launches
        workeroutput["real_device_connects"] = connects
        return
    launches += _WORKER_AI_CLI_LAUNCHES
    connects += _WORKER_DEVICE_CONNECTS
    if launches:
        sys.stderr.write(f"\nBL-229：用例窗口之外拉起真实 AI CLI 被拒绝：{launches}\n")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    if connects:
        sys.stderr.write(f"\nBL-231：用例窗口之外连接真实设备被拒绝：{connects}\n")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture
def allow_shared_screen_lock_root() -> Path | None:
    """显式声明本用例断言的就是共享锁根本身，退出下面的守卫。"""
    return _SHARED_SCREEN_LOCK_ROOT


@pytest.fixture
def isolated_screen_lock_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """把默认锁根改到 `tmp_path`，供**拿不到 `lock=` 注入点**的用例使用。

    CLI 入口（`autophone.adapters.cli._common._service`）与 HTTP 组合根
    （`autophone.adapters.http.composition.build_dependencies`）都在生产代码里自己
    构造 `RunService`，测试只能改默认锁根本身。能传 `lock=` 的地方一律传，不要用本夹具
    代替显式注入。
    """
    from autophone.device import lock as lock_module

    root = tmp_path / "locks"
    monkeypatch.setattr(lock_module, "default_screen_lock_root", lambda: root)
    return root


@pytest.fixture(autouse=True)
def forbid_shared_screen_lock_root(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """禁止用例在主 checkout 的共享 Screen Lock 根上建 `ScreenLock`。

    修法是把锁根一起隔离到 `tmp_path`：
    `RunService(run_root=tmp_path, lock=ScreenLock(root=tmp_path / "locks"))`；
    锁根由生产代码内部决定、测试拿不到注入点时（CLI、HTTP 组合根），改用
    `isolated_screen_lock_root` 夹具。
    """
    global _SCREEN_LOCK_VIOLATIONS, _SCREEN_LOCK_GUARD_ALLOWED
    from autophone.device import lock as lock_module

    allowed = "allow_shared_screen_lock_root" in request.fixturenames
    shared = _SHARED_SCREEN_LOCK_ROOT
    original = lock_module.ScreenLock.__init__
    violations: list[str] = []
    # 守卫自检用例（tests/test_screen_lock.py）故意触发一次再清空这个列表，以此证明
    # 本进程（xdist 下就是本 worker）确实装上了守卫。
    request.node.shared_screen_lock_root_violations = violations

    def guarded(self, *, root: Path | None = None) -> None:
        resolved = Path(root) if root is not None else lock_module.default_screen_lock_root()
        if (
            shared is not None
            and resolved == shared
            and not _screen_lock_constructed_during_import()
        ):
            violations.append(str(resolved))
            raise AssertionError(
                f"{request.node.nodeid} 在主 checkout 的共享 Screen Lock 根上建了 ScreenLock："
                f"{resolved}。测试必须把锁根隔离到 tmp_path（见 tests/conftest.py 里本守卫的"
                f" docstring）；确实要断言共享锁根的用例取用 allow_shared_screen_lock_root 夹具。"
            )
        original(self, root=resolved)

    # 用例窗口内：访问层的账本也记到本用例名下，好指名是哪条用例。
    session_ledger = _SCREEN_LOCK_VIOLATIONS
    _SCREEN_LOCK_VIOLATIONS = violations
    _SCREEN_LOCK_GUARD_ALLOWED = allowed
    try:
        if not allowed:
            monkeypatch.setattr(lock_module.ScreenLock, "__init__", guarded)
        yield
    finally:
        _SCREEN_LOCK_VIOLATIONS = session_ledger
        _SCREEN_LOCK_GUARD_ALLOWED = False
    # 后台线程里的构造会被那个线程吞掉异常，这里补一次收口。
    assert not violations, (
        f"{request.node.nodeid} 触到共享 Screen Lock 根：{violations}"
    )


@pytest.fixture(autouse=True)
def disable_crash_trap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the frida crash trap off for the whole suite.

    ``AUTOPHONE_CRASH_TRAP`` defaults to on, so any test that reaches
    ``RunService`` without injecting ``crash_trap_factory`` starts a real
    ``crash-trap`` thread that tries to attach to frida-server.  Off a live VM
    that thread dies with ``frida.ServerNotRunningError`` and pytest reports a
    ``PytestUnhandledThreadExceptionWarning``.  Tests that assert on the switch
    itself opt back in with ``monkeypatch.delenv``/``setenv``.
    """
    monkeypatch.setenv("AUTOPHONE_CRASH_TRAP", "0")


@pytest.fixture(autouse=True)
def disable_app_frida_hygiene(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the Run-boundary app Frida hygiene (BL-083 r8) off for the whole suite.

    ``AUTOPHONE_APP_FRIDA_HYGIENE`` defaults to on, so a test reaching
    ``RunExecution`` without injecting ``app_hygiene_factory`` would read the
    guest process table and terminate the app under test over the real UDS.
    Tests of the hygiene itself inject fake ports or opt back in.
    """
    monkeypatch.setenv("AUTOPHONE_APP_FRIDA_HYGIENE", "0")


@pytest.fixture(autouse=True)
def disable_native_lock_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the native lock-state read off for the whole suite.

    ``RunExecution._ensure_unlocked``, the Rig app-ready gate and the delivery
    unlock read the notify lock state through the optional guest helper:
    the first read pushes the compiled lock_state helper over the target VM socket
    and runs it.  Without this a test that reaches the real path would talk to
    whatever ``vphone.sock`` the developer machine has open.  Tests that cover
    the native judgement opt back in with ``monkeypatch.setenv`` and inject a
    fake probe or a fake UDS.  The quarantine release probe (BL-223) ignores the
    switch; its tests inject the probe.
    """
    monkeypatch.setenv("AUTOPHONE_NATIVE_LOCK_STATE", "0")


@pytest.fixture(autouse=True)
def stub_springboard_residue_check(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the pre-install SpringBoard residue checks (BL-232 I1) to a clean verdict.

    ``packages.store.install_package`` runs ``ps`` and ``procexp`` through the
    injected guest shell before any install.  Existing install tests use stub
    shells that only answer ``mkdir``/``uicache``; without this stub the check
    would find no SpringBoard and refuse every install (fail-closed).  The stub
    returns a schema-valid ``clean`` record and never touches the shell.  Tests
    that cover the real check request the ``real_springboard_residue_check``
    fixture, which leaves the production seam in place.
    """
    if "real_springboard_residue_check" in request.fixturenames:
        return
    from autophone.packages import store

    monkeypatch.setattr(store, "_springboard_residue_check", lambda _shell: stubbed_springboard_residue_record())
    # Target A / 评估 IPA 安装路径（BL-232 3(c)）复用同一检查，默认同样换成通过的桩。
    try:
        target_spec = find_spec("autophone.workflow.ui_verification.ui_verification_eval_target_live")
    except ModuleNotFoundError:
        return
    if target_spec is None or target_spec.origin is None:
        return
    from autophone.workflow.ui_verification import ui_verification_eval_target_live as live

    monkeypatch.setattr(live, "_springboard_residue_check",
                        lambda _runner, **_kw: stubbed_springboard_residue_record())


def stubbed_springboard_residue_record() -> dict:
    """Schema-valid ``clean`` record (``springboard-residue-check.v2.json``) used by the stubs."""
    return {
        "schema": "autophone.springboard-residue-check/v2",
        "verdict": "clean",
        "code": None,
        "detail": "stubbed by tests/conftest.py",
        "criterion": {"tag": "Tag: 255", "match": "any_region"},
        "command": "/iosbinpack64/usr/local/bin/procexp 1 regions",
        "springboard": {"pid": 1, "started": "Thu Jan 1 00:00:00 2026"},
        "regions_parsed": 1,
        "tag255_regions": [],
        "residue_regions": [],
        "detached_agent_regions": 0,
        "elapsed_ms": 0,
    }


@pytest.fixture
def real_springboard_residue_check() -> None:
    """Opt out of ``stub_springboard_residue_check`` for tests of the real check."""


@pytest.fixture(scope="session")
def empty_rig_runtime_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """一个恒空的 Rig 运行目录，全会话共用（里面不会有 control-plane.env）。"""
    return tmp_path_factory.mktemp("rig-runtime-without-control-plane")


@pytest.fixture(autouse=True)
def isolate_control_plane_env(
    monkeypatch: pytest.MonkeyPatch, empty_rig_runtime_dir: Path,
):
    """Pin every test to the compatibility path regardless of the developer shell.

    `configured_control_plane()` 只看 `AUTOPHONE_DATABASE_URL` 是否非空，且结果被
    `lru_cache` 固化。本机开发 shell 一旦 export 该变量（§16.0 兼容路径退出第一步
    后这是默认状态），device console / recording 路由会整片变 410，本机 pytest 与
    CI 结果不一致。这里统一清掉并清缓存；要测 Control Plane 分支的用例自己
    monkeypatch（现有 `test_http_contracts.py` / `test_api_allowlist_contracts.py`
    就是这么做的）。

    `AUTOPHONE_TEST_POSTGRES_DSN` 不在清理范围——那是四个真 PG 测试的开关。

    `AUTOPHONE_CONTROL_WS` 同属控制面白名单（`start_vphone.sh` 与 DSN 同批写入），
    开发 shell 里常有值，一并清掉，否则「没配控制面时不写入任何键」这类判据会被
    环境里的既有值顶掉。

    `AUTOPHONE_CONTROL_HTTP` 指到一个必然连不上的地址：`control_http_base_url()` 在两个
    键都没有时回落到 `http://127.0.0.1:8100`，而本机开发时那个端口上常常真的跑着一个
    控制面。BL-151 的控制面身份闸会去读它的 `/health`，于是测试结论取决于本机那个进程
    跑的是哪一版代码。要测该闸的用例自己 `monkeypatch.setenv` 指向自己的桩。

    `AUTOPHONE_RIG_RUNTIME_DIR` 一并指到一个空的临时目录：CLI 入口现在会读
    `<运行目录>/control-plane.env`（`autophone.adapters.cli.control_plane_env`），
    本机跑着 rig 时那份文件里有真 DSN，不隔离就会让测试进程连上活的控制面。
    要测 env 文件解析的用例自己 `monkeypatch.setenv` 指向自己的临时目录。
    """
    from autophone.application.control_plane import reset_control_plane_cache

    monkeypatch.delenv("AUTOPHONE_DATABASE_URL", raising=False)
    monkeypatch.delenv("AUTOPHONE_DATABASE_SCHEMA", raising=False)
    monkeypatch.delenv("AUTOPHONE_CONTROL_WS", raising=False)
    monkeypatch.setenv("AUTOPHONE_CONTROL_HTTP", "http://127.0.0.1:1")
    monkeypatch.setenv("AUTOPHONE_RIG_RUNTIME_DIR", str(empty_rig_runtime_dir))
    reset_control_plane_cache()
    try:
        yield
    finally:
        reset_control_plane_cache()


@pytest.fixture(autouse=True)
def isolate_recipe_registries():
    """Restore every Recipe/Operation/effect registry after each test.

    App recipe imports rebuild process-global compatibility mirrors in addition
    to the App-scoped caches.  A test that imports another App must not select
    the validation surface seen by a later test.
    """
    from autophone.execution import effects
    from autophone.execution.engine import recipe

    with recipe.recipe_registry_lock():
        snapshot = {
            "operations": copy.deepcopy(recipe.OPERATIONS),
            "operation_requirements": copy.deepcopy(recipe.OPERATION_REQUIREMENTS),
            "app_operations": copy.deepcopy(recipe.APP_OPERATIONS),
            "app_root_operations": copy.deepcopy(recipe.APP_ROOT_OPERATIONS),
            "app_operation_requirements": copy.deepcopy(recipe.APP_OPERATION_REQUIREMENTS),
            "app_root_operation_requirements": copy.deepcopy(
                recipe.APP_ROOT_OPERATION_REQUIREMENTS
            ),
            "recipes": copy.deepcopy(recipe.RECIPES),
            "app_recipes": copy.deepcopy(recipe.APP_RECIPES),
            "app_root_recipes": copy.deepcopy(recipe.APP_ROOT_RECIPES),
            "app_registry_roots": copy.deepcopy(recipe.APP_REGISTRY_ROOTS),
            "active_registry_identity": recipe._ACTIVE_REGISTRY_IDENTITY,
        }
        with effects._EXECUTOR_EFFECTS_LOCK:
            executor_effects = copy.deepcopy(effects._EXECUTOR_EFFECTS)
            app_executor_effects = copy.deepcopy(effects._APP_EXECUTOR_EFFECTS)
            app_root_executor_effects = copy.deepcopy(
                effects._APP_ROOT_EXECUTOR_EFFECTS
            )
            app_executor_effect_roots = copy.deepcopy(
                effects._APP_EXECUTOR_EFFECT_ROOTS
            )
            active_executor_effect_identity = effects._ACTIVE_EXECUTOR_EFFECT_IDENTITY
    try:
        yield
    finally:
        with recipe.recipe_registry_lock():
            for registry, values in (
                (recipe.OPERATIONS, snapshot["operations"]),
                (recipe.OPERATION_REQUIREMENTS, snapshot["operation_requirements"]),
                (recipe.APP_OPERATIONS, snapshot["app_operations"]),
                (recipe.APP_ROOT_OPERATIONS, snapshot["app_root_operations"]),
                (recipe.APP_OPERATION_REQUIREMENTS, snapshot["app_operation_requirements"]),
                (
                    recipe.APP_ROOT_OPERATION_REQUIREMENTS,
                    snapshot["app_root_operation_requirements"],
                ),
                (recipe.RECIPES, snapshot["recipes"]),
                (recipe.APP_RECIPES, snapshot["app_recipes"]),
                (recipe.APP_ROOT_RECIPES, snapshot["app_root_recipes"]),
                (recipe.APP_REGISTRY_ROOTS, snapshot["app_registry_roots"]),
            ):
                registry.clear()
                registry.update(values)
            recipe._ACTIVE_REGISTRY_IDENTITY = snapshot["active_registry_identity"]
            with effects._EXECUTOR_EFFECTS_LOCK:
                effects._EXECUTOR_EFFECTS.clear()
                effects._EXECUTOR_EFFECTS.update(executor_effects)
                effects._APP_EXECUTOR_EFFECTS.clear()
                effects._APP_EXECUTOR_EFFECTS.update(app_executor_effects)
                effects._APP_ROOT_EXECUTOR_EFFECTS.clear()
                effects._APP_ROOT_EXECUTOR_EFFECTS.update(app_root_executor_effects)
                effects._APP_EXECUTOR_EFFECT_ROOTS.clear()
                effects._APP_EXECUTOR_EFFECT_ROOTS.update(app_executor_effect_roots)
                effects._ACTIVE_EXECUTOR_EFFECT_IDENTITY = active_executor_effect_identity


@dataclass(frozen=True)
class RecordedHttpRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


class JsonHttpServer:
    def __init__(self, response: dict[str, Any], *, status: int = 200) -> None:
        self.requests: list[RecordedHttpRequest] = []
        payload = json.dumps(response).encode("utf-8")
        requests = self.requests

        class Handler(BaseHTTPRequestHandler):
            def _reply(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                requests.append(RecordedHttpRequest(
                    method=self.command,
                    path=self.path,
                    headers={key: value for key, value in self.headers.items()},
                    body=self.rfile.read(length),
                ))
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

            do_GET = _reply  # noqa: N815 - stdlib callback names
            do_POST = _reply  # noqa: N815 - stdlib callback names

            def log_message(self, *_args: object) -> None:
                return

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def url(self, path: str = "") -> str:
        return f"http://127.0.0.1:{self._server.server_port}{path}"

    def close(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=2)
        self._server.server_close()


@pytest.fixture
def json_http_server():
    servers: list[JsonHttpServer] = []

    def start(response: dict[str, Any], *, status: int = 200) -> JsonHttpServer:
        server = JsonHttpServer(response, status=status)
        servers.append(server)
        return server

    yield start

    for server in reversed(servers):
        server.close()


@pytest.fixture
def claude_ai_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin tests whose injected runner returns Claude CLI envelopes."""
    monkeypatch.setenv("AUTOPHONE_AI_PROVIDER", "claude")


@pytest.fixture
def fake_guest_ssh_password(monkeypatch):
    value = "fixture with spaces & 'quotes' $HOME"
    monkeypatch.setenv("AUTOPHONE_SSH_PASSWORD", value)
    return value
