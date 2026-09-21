"""仓库卫生守卫：公开仓库不得跟踪敏感路径 / 密钥串。

为什么需要它（这条注释比代码重要）：
本仓库是 public（GitHub API 实测 private=False）。防泄露目前**完全依赖
.gitignore 兜底**——而"兜底"的含义是：只要有人 `git add -f` 一次，敏感内容就会
即时公开，并且在历史里永久留痕（清理要 force push + filter-repo，代价远大于预防）。
`.gitignore` 只是"声明"，本守卫才是"运行期红灯"，两者合起来才算闭环。

为什么写成 pytest 用例而不是 CI workflow 步骤：
本仓库 PAT 缺 `workflow` scope，改动 `.github/workflows/` 会被服务端拒绝
（`refusing to allow a Personal Access Token ... without workflow scope`），
重试无用。挂在既有的 `pytest tests` 步骤里，既不需要碰 workflow 文件，
又能拦在合并之前。

判定对象为什么是 `git ls-files`（索引）而不是遍历文件系统：
语义必须是"会被提交的东西"。仅存在于磁盘但已被忽略的文件不算泄露；
而已经 `git add -f`、尚未 commit 的文件**在索引里**，所以这条守卫能在提交
之前就报红，而不是等它进了历史才发现——那时已经晚了。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

# tests/ -> backend/ -> 仓库根
REPO_ROOT = Path(__file__).resolve().parents[2]

# 禁止被跟踪的路径形态。与 .gitignore 保持一致，这里再钉一层运行期断言。
FORBIDDEN_PATH_PATTERNS: tuple[str, ...] = (
    r"(^|/)\.workbuddy/",          # 评审记录 / 会话记忆，非项目产物
    r"(^|/)\.env$",                # 真实环境变量
    r"(^|/)\.env\.local$",
    r"(^|/)backend/data/",         # 含模型 Key 与口令哈希的运行时配置
    r"(^|/)data/queries[^/]*\.jsonl$",   # 真实课堂日志抽取的查询池
    r"(^|/)data/eval_golden[^/]*\.json$",
    # 必须锚定仓库根：`backend/app/models` 是 SQLAlchemy 源码包，归到这条下面
    # 会误伤（实测故障注入时就命中了它）。gitignore 的 `models/` 不带前导斜杠
    # 是"任意层级匹配"，这条守卫反而比它更精确。
    r"^models/",                   # 仓库根 ONNX 权重目录，40~100MB 且随模型而异
    r"\.db$",
    r"\.sqlite3?$",
    r"\.pem$",
    r"\.key$",
)

# 示例文件本身就是给人抄的，允许存在
ALLOWED_EXACT: frozenset[str] = frozenset({".env.example"})

# 高置信度密钥形态：宁可漏报，不可误报——误报会让人把守卫关掉，
# 关掉比漏报更糟（"告警常态化后被静音"是同一类失效）。
SECRET_PATTERNS: tuple[str, ...] = (
    r"ghp_[A-Za-z0-9]{20,}",
    r"github_pat_[A-Za-z0-9_]{20,}",
    r"AKIA[0-9A-Z]{16}",
    r"BEGIN (RSA|OPENSSH|DSA|EC) PRIVATE KEY",
)


def _git(*args: str) -> str:
    """在仓库根执行 git；git 不可用时必须失败而不是跳过。

    守卫静默失效比没有守卫更危险——它会给人"有保护"的错觉。
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:  # pragma: no cover - 环境异常才会走到
        pytest.fail("git 不可用：卫生守卫无法判定跟踪清单，应修环境而不是跳过守卫")
    except subprocess.CalledProcessError as exc:  # pragma: no cover
        pytest.fail(f"git {args} 执行失败：{exc.stderr[:200]}")
    return proc.stdout


def _tracked_files() -> list[str]:
    out = _git("ls-files")
    files = [line.strip() for line in out.splitlines() if line.strip()]
    assert files, "git ls-files 返回空清单：守卫无法工作，需检查仓库状态"
    return files


class TestNoSensitivePathsTracked:
    """索引里不得出现敏感路径——含已 add 未 commit 的（提交前就拦住）。"""

    def test_tracked_index_has_no_forbidden_paths(self) -> None:
        offenders: list[tuple[str, str]] = []
        for path in _tracked_files():
            if path in ALLOWED_EXACT:
                continue
            for pattern in FORBIDDEN_PATH_PATTERNS:
                if re.search(pattern, path):
                    offenders.append((path, pattern))
                    break
        assert not offenders, (
            "以下路径不得被跟踪（仓库是 public，一旦提交即公开且历史永久留痕）："
            f"{offenders}"
        )

    def test_workbuddy_never_entered_history(self) -> None:
        """历史维度：进了历史再删文件也救不回来（需 filter-repo 重写）。

        这条无法用故障注入验证（那需要真的写入历史），它的有效性由审计事实
        支撑：全量 `git log --all --name-only` 对 .workbuddy 零命中。
        """
        out = _git("log", "--all", "--pretty=format:", "--name-only")
        leaked = sorted({ln.strip() for ln in out.splitlines() if ".workbuddy" in ln})
        assert not leaked, f"历史提交里出现过 .workbuddy 路径：{leaked}"


class TestNoSecretsInTrackedFiles:
    """跟踪文件内容里不得出现高置信度密钥串。"""

    def test_tracked_files_have_no_secret_strings(self) -> None:
        hits: list[tuple[str, str]] = []
        for rel in _tracked_files():
            path = REPO_ROOT / rel
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:  # pragma: no cover - 权限/竞态等环境异常
                continue
            for pattern in SECRET_PATTERNS:
                if re.search(pattern, text):
                    hits.append((rel, pattern))
                    break
        assert not hits, f"跟踪文件里出现疑似密钥串（公开仓库视为已泄露）：{hits}"
