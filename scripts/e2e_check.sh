#!/usr/bin/env bash
# 端到端全链路一致性回归：生成样例 → 跑批 → 归因 → 人工确认 → 幂等写回 → 导出。
# 本地协议替身（临时 http.server）由编排脚本 scripts/e2e_run.py 自行启停，
# 绑 127.0.0.1 回环，不连真实生产端点、不依赖外网；脚本结束即关闭，不留后台进程。
# 退出码：0 = 逐段数字全部一致；非 0 = 任一环不一致或运行异常（set -e 透传）。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"

# pytest 用 pyproject.toml 的 pythonpath=src 免安装导入；脚本显式补上，
# 并把项目根加入 PYTHONPATH 以便 import scripts.*
export PYTHONPATH="$ROOT/src:$ROOT${PYTHONPATH:+:$PYTHONPATH}"

# 测试用凭据：一律从环境变量读，全部指向本地占位/回环，绝不连真实端点。
# QC_BASE_URL / QC_TOKEN 会在编排脚本内被临时覆盖为本地 http.server 替身地址。
export AE_BASE_URL="http://127.0.0.1:1"
export AE_API_TOKEN="e2e-local"
export QC_BASE_URL="http://127.0.0.1:1"
export QC_TOKEN="e2e-local"
export MODEL_BASE_URL="http://127.0.0.1:1"
export MODEL_API_KEY="e2e-local"
export MODEL_NAME="e2e-local-model"
export MODEL_TIMEOUT="1"
export AE_RULE_SET=""

python3 -m scripts.e2e_run
