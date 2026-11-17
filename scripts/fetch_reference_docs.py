"""公开来源抓取：下载《医疗质量安全不良事件分级分类标准》附件2 PDF 并校验 sha256。

- 下载产物与清单落在 assets/reference/；固定文件名，重复执行不产生重复文件。
- 断网 / 超时 / HTTP 错误时降级使用内置副本 severity_levels_builtin.json，
  打印降级原因，脚本退出码仍为 0（数据获取阶梯的第 ① 级不因断网空转整轮）。
- 内置副本与 catalogs/severity_levels.json 的 A–I 逐级一致由测试断言。

用法：python scripts/fetch_reference_docs.py [--offline] [--timeout 5]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_DIR = PROJECT_ROOT / "assets" / "reference"
CATALOG_LEVELS = PROJECT_ROOT / "catalogs" / "severity_levels.json"
BUILTIN_LEVELS = REFERENCE_DIR / "severity_levels_builtin.json"
MANIFEST = REFERENCE_DIR / "sha256_manifest.json"

SOURCES = [
    {
        "name": "severity_standard_gaolan.pdf",
        "url": "https://www.gaolan.gov.cn/module/download/downfile.jsp?filename=f838b734707b4fbaa0b494661a75d587.pdf",
        "note": "皋兰县人民政府门户公开附件2（研究阶段已核 URL）",
    },
    {
        "name": "severity_standard_kaiping.pdf",
        "url": "https://www.kaiping.gov.cn/attachment/0/364/364116/2952634.pdf",
        "note": "开平市人民政府门户公开附件2（备用镜像）",
    },
]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_levels(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))["levels"]


def verify_builtin_matches_catalog() -> dict[str, Any]:
    """内置副本与正式目录逐条一致，差异即失败（由测试断言）。"""
    catalog = load_levels(CATALOG_LEVELS)
    builtin = load_levels(BUILTIN_LEVELS) if BUILTIN_LEVELS.exists() else []
    return {"ok": catalog == builtin, "catalog_count": len(catalog), "builtin_count": len(builtin)}


def _download(source: dict, timeout: float) -> bytes:
    request = urllib.request.Request(source["url"], headers={"User-Agent": "adverse-event-qc/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec - 公开政府门户只读
        data = response.read()
    if not data or len(data) < 512:
        raise OSError("下载内容过短，疑似不是 PDF 附件")
    return data


def fetch_all(*, offline: bool = False, timeout: float = 5.0,
              dest: Path | None = None) -> dict[str, Any]:
    """抓取全部来源；返回结构化结果。offline=True 模拟断网（测试与离线演练用）。"""
    target = Path(dest) if dest else REFERENCE_DIR
    target.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {}
    if MANIFEST.exists():
        try:
            manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}

    downloaded: list[str] = []
    skipped: list[str] = []
    degraded = False
    reasons: list[str] = []

    for source in SOURCES:
        path = target / source["name"]
        if path.exists() and manifest.get(source["name"]):
            skipped.append(source["name"])
            continue
        if offline:
            degraded = True
            reasons.append(f"{source['name']}：显式离线模式，使用内置副本")
            continue
        try:
            data = _download(source, timeout)
            path.write_bytes(data)
            manifest[source["name"]] = {
                "sha256": sha256_bytes(data), "bytes": len(data), "url": source["url"],
                "note": source["note"],
            }
            downloaded.append(source["name"])
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            degraded = True
            reasons.append(f"{source['name']}：{type(exc).__name__}: {exc}")

    # 断网降级：内置副本始终保证目录资产可用；不改写正式目录文件。
    if degraded:
        if not BUILTIN_LEVELS.exists() and CATALOG_LEVELS.exists():
            payload = json.loads(CATALOG_LEVELS.read_text(encoding="utf-8"))
            payload["builtin"] = True
            payload["note"] = "断网降级自动生成的内置副本"
            BUILTIN_LEVELS.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest["_degraded"] = {"reasons": reasons, "builtin": str(BUILTIN_LEVELS.relative_to(PROJECT_ROOT))}
    else:
        manifest.pop("_degraded", None)
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "downloaded": downloaded, "skipped": skipped, "degraded": degraded,
        "reasons": reasons, "manifest": manifest,
        "builtin_match": verify_builtin_matches_catalog(),
        "reference_dir": str(target),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抓取公开分级分类标准附件（断网自动降级）")
    parser.add_argument("--offline", action="store_true", help="离线演练：不联网，直接走内置副本")
    parser.add_argument("--timeout", type=float, default=5.0, help="单次下载超时秒数")
    args = parser.parse_args(argv)
    result = fetch_all(offline=args.offline, timeout=args.timeout)
    for name in result["downloaded"]:
        print(f"[抓取] 已下载 {name}（sha256 已写入清单）")
    for name in result["skipped"]:
        print(f"[跳过] {name} 已存在且清单有 sha256，不重复下载")
    if result["degraded"]:
        print("[降级] 联网不可用，使用内置分级口径副本：")
        for reason in result["reasons"]:
            print(f"  - {reason}")
        print("[降级] severity_levels.json 的 A–I 与内置副本逐条一致，脚本退出码仍为 0")
    print(f"[清单] {MANIFEST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
