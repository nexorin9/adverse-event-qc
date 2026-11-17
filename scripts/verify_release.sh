#!/usr/bin/env bash
# 发布前可重复验证：安装检查 → 断网降级 → 生成脱敏样例 → 跑最窄端到端切片
# → 校验产物存在且非空 → 发布树与 .gitignore 检查 → 退出码 0。
#
# 全程不联网：凭据是本机回环占位值，公开口径抓取走显式 offline 分支。
# 所有跑批产物写在 mktemp 临时目录，脚本结束即清理；发布树里被本脚本或
# 其它命令留下的运行时产物会在最后一步按白名单清掉，可重复执行。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="$ROOT/src:$ROOT${PYTHONPATH:+:$PYTHONPATH}"

# 测试层凭据：一律本机回环占位，绝不连真实院内端点、绝不依赖外网。
export AE_BASE_URL="http://127.0.0.1:1"
export AE_API_TOKEN="release-local"
export QC_BASE_URL="http://127.0.0.1:1"
export QC_TOKEN="release-local"
export MODEL_BASE_URL="http://127.0.0.1:1"
export MODEL_API_KEY="release-local"
export MODEL_NAME="release-local-model"
export MODEL_TIMEOUT="1"
export AE_RULE_SET=""

WORK="$(mktemp -d "${TMPDIR:-/tmp}/ae_qc_release_XXXXXX")"
cleanup() {
  if [[ -n "${WORK:-}" && "$WORK" == *"ae_qc_release_"* ]]; then
    rm -rf -- "$WORK"
  fi
}
trap cleanup EXIT

step() { printf '\n[%s] %s\n' "$1" "$2"; }

step 1/8 "安装检查：依赖可导入 + 包可导入"
python3 - <<'PY'
import importlib
import pydantic, openpyxl, httpx, yaml, dotenv, pytest, ae_qc
for name in ("pydantic", "openpyxl", "httpx", "yaml", "dotenv", "pytest"):
    importlib.import_module(name)
print("依赖 OK：pydantic", pydantic.__version__, "/ openpyxl", openpyxl.__version__,
      "/ httpx", httpx.__version__, "/ pytest", pytest.__version__)
print("项目包 OK：ae_qc", getattr(ae_qc, "__version__", "0.1.0"))
PY

step 2/8 "断网数据获取降级路径可用（显式 offline，不发请求）"
WORK="$WORK" python3 - <<'PY'
import os
from pathlib import Path
from scripts.fetch_reference_docs import BUILTIN_LEVELS, MANIFEST, fetch_all
assert BUILTIN_LEVELS.exists(), "缺 assets/reference 内置分级口径副本"
had_manifest = MANIFEST.exists()
result = fetch_all(offline=True, dest=Path(os.environ["WORK"]) / "reference")
assert result["degraded"], "离线模式未走降级分支"
assert result["builtin_match"]["ok"], result["builtin_match"]
if not had_manifest and MANIFEST.exists():
    MANIFEST.unlink()  # 验证过程产生的清单不留在发布树
print("降级 OK：", len(result["reasons"]), "个来源使用内置副本；A–I 逐条一致",
      result["builtin_match"])
PY

step 3/8 "生成脱敏样例批次"
WORK="$WORK" python3 - <<'PY'
import os
from pathlib import Path
from scripts.gen_sample_batches import generate_batches
inbox = Path(os.environ["WORK"]) / "inbox"
generate_batches(inbox_dir=inbox, depts=["D01", "D02"],
                 periods=["2026-01"], rows_per_sheet=12)
files = sorted(inbox.glob("*/*.xlsx"))
assert files, "样例批次未生成"
print("样例批次", len(files), "份：", ", ".join(f.name for f in files))
PY

step 4/8 "跑最窄端到端切片（demo_minimal + CLI 跑批 / 导出）"
bash scripts/demo_minimal.sh
python3 -m ae_qc.cli batch run --inbox "$WORK/inbox" --out "$WORK/out" \
  --db "$WORK/ledger.sqlite" --dept D01
python3 -m ae_qc.cli findings export --db "$WORK/ledger.sqlite" --out "$WORK/out"

step 5/8 "校验产物存在且非空"
WORK="$WORK" python3 - <<'PY'
import os
from pathlib import Path
from openpyxl import load_workbook
work = Path(os.environ["WORK"])
out = work / "out"
csvs = sorted(out.glob("越界行清单*.csv"))
xlsxs = sorted(out.glob("整改清单*.xlsx"))
assert csvs, "缺越界行清单 CSV"
assert xlsxs, "缺整改清单 xlsx"
for p in csvs + xlsxs:
    assert p.stat().st_size > 0, f"空产物：{p}"
    print("非空 OK：", p.name, p.stat().st_size, "bytes")
lines = csvs[0].read_text(encoding="utf-8-sig").splitlines()
assert len(lines) >= 2 and lines[0].startswith("批次号"), "CSV 表头异常"
print("越界清单数据行：", len(lines) - 1)
wb = load_workbook(xlsxs[0], read_only=True)
assert wb.sheetnames, "xlsx 无工作表"
print("整改清单工作表：", wb.sheetnames)
wb.close()
assert (work / "ledger.sqlite").stat().st_size > 0, "台账文件为空"
print("台账 OK：ledger.sqlite")
PY

step 6/8 "发布树检查：保留项在位 + .gitignore 覆盖 + 运行时产物已清理"
python3 - <<'PY'
from pathlib import Path
import shutil

root = Path.cwd()
keep = [
    "README.md", "LICENSE", "qrcode.jpg", "requirements.txt", ".env.example",
    "config.toml", "pyproject.toml", ".gitignore",
    "docs/concepts/visual-target.md",
    "docs/screenshots/01-cli-batch-run.png",
    "docs/screenshots/02-main-window.png",
    "docs/screenshots/03-rectify-list.png",
    "docs/examples.md", "docs/语义归并对照表.md", "docs/规模验收结论.md",
    "scripts/verify_release.sh", "scripts/capture_screenshots.py",
    "scripts/eval_merge.py", "scripts/scale_check.py",
    "assets/eval/labeled_event_names.json",
    "assets/reference/severity_levels_builtin.json",
    "catalogs/rulesets/v2026-01.yaml",
]
for rel in keep:
    p = root / rel
    assert p.is_file() and p.stat().st_size > 0, f"发布保留项缺失或为空：{rel}"
for rel in ("scripts", "catalogs", "assets", "tests", "src/ae_qc"):
    assert (root / rel).is_dir(), f"发布保留目录缺失：{rel}"
print("保留项 OK：", len(keep), "个文件 + 5 个目录")

gitignore = (root / ".gitignore").read_text(encoding="utf-8")
required = [".env", "venv/", ".venv/", "node_modules/", "dist/", "build/",
            "coverage/", "data/", "logs/", "out/", "tmp/", "__pycache__/",
            "*.py[cod]", ".pytest_cache/", "*.log", "*.sqlite", "score.json"]
missing = [pat for pat in required if pat not in gitignore]
assert not missing, f".gitignore 缺覆盖：{missing}"
assert "qrcode.jpg" not in gitignore, ".gitignore 不得排除 qrcode.jpg"
print(".gitignore 覆盖 OK：", len(required), "项；未排除 qrcode.jpg")

# 按白名单清理发布树内由跑批 / 测试 / 本脚本留下的运行时产物
removed = []
for rel in ("logs", "data", "tmp", "temp", ".pytest_cache", ".mypy_cache",
            ".ruff_cache", ".cache", "out"):
    p = root / rel
    if p.exists():
        shutil.rmtree(p) if p.is_dir() else p.unlink()
        removed.append(rel)
for p in root.rglob("__pycache__"):
    if p.is_dir():
        shutil.rmtree(p)
        removed.append(str(p.relative_to(root)))
for pattern in ("*.pyc", "*.pyo", "*.log", "*.tmp", "*.sqlite", "*.sqlite3", "*.db"):
    for p in root.rglob(pattern):
        if p.is_file():
            p.unlink()
            removed.append(str(p.relative_to(root)))
leftovers = [r for r in ("logs", "data", "tmp", "out", ".pytest_cache") if (root / r).exists()]
assert not leftovers, f"发布树仍有运行时目录：{leftovers}"
print("运行时产物清理 OK：", len(removed), "项（含本脚本刚生成的缓存）")
PY

step 7/8 "git 工作树检查"
if git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  DIRTY="$(git -C "$ROOT" status --porcelain)"
  if [[ -n "$DIRTY" ]]; then
    printf '%s\n' "$DIRTY" >&2
    echo "发布树存在未提交/未跟踪内容" >&2
    exit 1
  fi
  echo "git status 干净"
else
  echo "本目录尚未初始化 git 工作树（无 .git）；已用等价的发布树文件系统断言替代："
  echo "  运行时产物已清理、保留项齐备、.gitignore 覆盖完整。推送到 GitHub 前执行 git init 即可。"
fi

step 8/8 "结论"
echo "PASS：安装 / 断网降级 / 样例生成 / 端到端切片 / 产物非空 / 发布树 全部通过，退出码 0"
