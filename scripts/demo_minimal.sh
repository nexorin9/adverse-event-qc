#!/usr/bin/env bash
# 最窄端到端切片：生成样例 → 读表 → 切批 → 一条 set_membership → 越界行清单。
# 退出码 0 且 out/越界行清单_*.csv 非空即视为主干打通。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"

# pytest 用 pyproject.toml 的 pythonpath=src 免安装导入；脚本显式补上以适配直接运行
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

python3 -m scripts.demo_minimal
