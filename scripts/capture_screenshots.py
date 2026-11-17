"""采集真实运行证据图到 docs/screenshots/。

三张图的数据全部来自本项目的真实运行：
  01 终端会话：真实执行 `ae_qc.cli` 的 batch run / findings export / scale_check
      并捕获完整 stdout，逐行渲染成终端窗口；
  02 主窗口：真实跑批写入 SQLite 台账后，再把全部越界行送进真实归因编排器
      （模型端点不可达，走产品内建的降级路径），经 desktop.data_source
      （界面唯一取数入口）取回批次 / 越界行 / 待确认草稿，按设计 token 渲染三栏；
      细条用 batch_stats 真实算出的规则通过率，归因列只标真实落库的草稿状态。
  03 导出报告：用 openpyxl 读取真实导出的整改清单 xlsx 表头与前几行，
      并列出真实导出的证据包目录。

诚实标注：本自动化环境无屏幕录制权限（screencapture / ImageGrab 均被拒），
因此不是屏幕级截屏，而是「真实运行数据 + 真实组件结构」的渲染图；它们不是
设计稿或概念图，但也**不声称**与真实 tkinter 窗口逐像素一致——真实窗口配色
与字距受系统主题影响。采集方式与差异写在 README「实测证据」小节。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ae_qc.desktop.theme import TOKENS  # noqa: E402
from ae_qc.ledger.audit import AuditLog  # noqa: E402
from ae_qc.ledger.queries import LedgerQueries  # noqa: E402
from ae_qc.ledger.store import Store  # noqa: E402
from ae_qc.reporting.evidence_pack import export_evidence_pack  # noqa: E402
from scripts.gen_sample_batches import generate_batches  # noqa: E402

OUT_DIR = PROJECT_ROOT / "docs" / "screenshots"
W, H = 1440, 900
FONT_CN = "/System/Library/Fonts/STHeiti Light.ttc"
FONT_CN_BOLD = "/System/Library/Fonts/STHeiti Medium.ttc"
FONT_MONO = "/System/Library/Fonts/Menlo.ttc"


def font(size: int, *, mono: bool = False, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_MONO if mono else (FONT_CN_BOLD if bold else FONT_CN)
    return ImageFont.truetype(path, size)


def is_wide(ch: str) -> bool:
    """CJK / 全角 / 扩展区字符判定：Menlo 不含中文字形，必须换中文字体。"""
    cp = ord(ch)
    return (0x1100 <= cp <= 0x115F or 0x2E80 <= cp <= 0xA4CF
            or 0xAC00 <= cp <= 0xD7A3 or 0xF900 <= cp <= 0xFAFF
            or 0xFE10 <= cp <= 0xFE6F or 0xFF00 <= cp <= 0xFF60
            or 0xFFE0 <= cp <= 0xFFE6 or 0x1F300 <= cp <= 0x1FAFF
            or 0x20000 <= cp <= 0x3FFFF)


def mixed_fonts(size: int) -> tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont]:
    """返回 (ASCII 等宽, 中文) 两个字体，逐字符混排避免中文显示为方块。"""
    return font(size, mono=True), font(size)


def mixed_width(draw: ImageDraw.ImageDraw, text: str, ascii_font, cjk_font) -> float:
    return sum(draw.textlength(ch, font=cjk_font if is_wide(ch) else ascii_font)
               for ch in text)


def wrap_mixed(draw: ImageDraw.ImageDraw, text: str, ascii_font, cjk_font,
               max_width: int) -> list[str]:
    """按混排真实像素宽度折行。"""
    out, current = [], ""
    for ch in text:
        if ch == "\n":
            out.append(current)
            current = ""
            continue
        trial = current + ch
        if mixed_width(draw, trial, ascii_font, cjk_font) > max_width and current:
            out.append(current)
            current = ch
        else:
            current = trial
    out.append(current)
    return out


def draw_mixed(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str,
               ascii_font, cjk_font, fill: str) -> None:
    """同号字体分段绘制：ASCII 走 Menlo，中文走 STHeiti，位置按真实字宽累加。"""
    x, y = xy
    buf, active = "", None
    for ch in text:
        f = cjk_font if is_wide(ch) else ascii_font
        if active is not None and f is not active:
            draw.text((x, y), buf, font=active, fill=fill)
            x += draw.textlength(buf, font=active)
            buf = ""
        active, buf = f, buf + ch
    if buf:
        draw.text((x, y), buf, font=active, fill=fill)


def wrap_pixels(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont,
                max_width: int) -> list[str]:
    """按像素宽度折行（中文按实际字宽，不按字符数硬切）。"""
    out, current = [], ""
    for ch in text:
        if ch == "\n":
            out.append(current)
            current = ""
            continue
        trial = current + ch
        if draw.textlength(trial, font=fnt) > max_width and current:
            out.append(current)
            current = ch
        else:
            current = trial
    out.append(current)
    return out


def _run_real_commands(work: Path) -> tuple[list[tuple[str, str]], dict[str, Any]]:
    """真实执行 CLI 与脚本，返回 (命令, stdout) 列表与产物路径信息。"""
    inbox, out = work / "inbox", work / "out"
    db = work / "ledger.sqlite"
    generate_batches(inbox_dir=inbox, depts=["D01", "D02", "D03"],
                     periods=["2026-01"], rows_per_sheet=20)

    env = dict(os.environ)
    env.update({
        # 全部子进程都在临时工作目录里执行，画面上的相对路径命令与实际执行完全一致
        "PYTHONPATH": os.pathsep.join([str(PROJECT_ROOT / "src"), str(PROJECT_ROOT)]),
        "AE_BASE_URL": "http://127.0.0.1:9", "AE_API_TOKEN": "占位令牌",
        "QC_BASE_URL": "http://127.0.0.1:9", "QC_TOKEN": "占位令牌",
        "MODEL_BASE_URL": "http://127.0.0.1:9/v1", "MODEL_API_KEY": "占位令牌",
        "MODEL_NAME": "院内示例模型", "MODEL_TIMEOUT": "5", "AE_RULE_SET": "",
    })
    commands: list[tuple[str, list[str]]] = [
        ("python3 -m ae_qc.cli rule-set validate --version v2026-01 --against v2026-02",
         ["-m", "ae_qc.cli", "rule-set", "validate", "--version", "v2026-01", "--against", "v2026-02"]),
    ]
    for dept in ("D01", "D02", "D03"):
        commands.append((
            f"python3 -m ae_qc.cli batch run --inbox inbox/ --out out/ --db ledger.sqlite --dept {dept}",
            ["-m", "ae_qc.cli", "batch", "run", "--inbox", "inbox/", "--out", "out/",
             "--db", "ledger.sqlite", "--dept", dept],
        ))
    commands.append((
        "python3 -m ae_qc.cli findings export --db ledger.sqlite --out out/",
        ["-m", "ae_qc.cli", "findings", "export", "--db", "ledger.sqlite", "--out", "out/"],
    ))
    commands.append((
        "python3 -m scripts.scale_check --output scale.md",
        ["-m", "scripts.scale_check", "--output", "scale.md"],
    ))
    outputs: list[tuple[str, str]] = []
    for label, argv in commands:
        proc = subprocess.run([sys.executable, *argv], cwd=work, env=env,
                              capture_output=True, text=True, timeout=180)
        body = (proc.stdout or "").strip()
        if proc.returncode != 0:
            body += f"\n[退出码 {proc.returncode}] {(proc.stderr or '').strip()}"
        outputs.append((label, body))

    # 真实导出证据包（与整改清单同批台账）
    store = Store(str(db))
    log = AuditLog(store)
    queries = LedgerQueries(store)
    batches = queries.list_batches()
    if batches:
        ref = batches[0]["batch_ref"]
        export_evidence_pack(ref, log.export(ref), out / f"证据包_{ref}.json",
                             generator="截图采集脚本（真实运行）")

    # 真实归因链路：把本批全部越界行送进归因编排器。模型端点指向本机不可达
    # 端口，走的是产品里真实的「不可达 → 降级待人工指定」路径；草稿、状态、
    # 待确认数都由台账真实落库，不伪造模型建议编码，也不伪造置信度。
    from ae_qc.attribution.model_gateway import ModelGateway
    from ae_qc.attribution.orchestrator import AttributionOrchestrator
    evidence = queries.unexpected_rows()
    draft_inputs = [{
        "row_key": str(e.get("row_key", "")),
        "batch_ref": str(e.get("batch_ref", "")),
        "text": str(e.get("present_value") or e.get("event_type_text") or ""),
        "rule_family": str(e.get("rule_family") or e.get("kind") or "unknown"),
    } for e in evidence]
    gateway = ModelGateway(base_url="http://127.0.0.1:9/v1", api_key="占位令牌",
                           model="院内示例模型", timeout=0.8, max_retries=1)
    orchestrator = AttributionOrchestrator(gateway, store=store, batch_size=50,
                                           max_concurrency=2)
    attr = orchestrator.run(draft_inputs)
    outputs.append((
        "归因编排（模型端点不可达，真实降级路径）",
        f"输入越界命中 {attr['summary']['total_rows']} 条 · 成功 {attr['summary']['succeeded_rows']} 条 · "
        f"降级 {attr['summary']['failed_rows']} 条 · 台账待人工确认 {queries.pending_count()} 个唯一行键"
        "（同一行命中多条规则按 row_key 去重）",
    ))
    store.close()
    return outputs, {"out": out, "db": db, "inbox": inbox}


def render_terminal(outputs: list[tuple[str, str]], path: Path) -> None:
    """01：真实 stdout 逐行渲染成终端会话窗口。"""
    img = Image.new("RGB", (W, H), "#111820")
    d = ImageDraw.Draw(img)
    # 窗口标题栏
    d.rectangle([0, 0, W, 44], fill="#2B333D")
    for i, color in enumerate(("#FF5F57", "#FEBC2E", "#28C840")):
        x = 22 + i * 24
        d.ellipse([x, 16, x + 12, 28], fill=color)
    d.text((110, 12), "ae-qc · 运维通道 · 真实终端会话", font=font(16, bold=True), fill="#E5EAF0")
    d.text((W - 220, 14), "1440 x 900 · 实测回显", font=font(13), fill="#8A98A8")

    ascii_font, cjk_font = mixed_fonts(15)
    note_ascii, note_cjk = mixed_fonts(13)
    x0, y, max_w = 24, 62, W - 48
    lines: list[tuple[str, str]] = []
    for label, body in outputs:
        lines.append((f"$ {label}", "cmd"))
        for raw in body.splitlines():
            lines.append((raw, "json" if raw.lstrip().startswith("{") else "out"))
        lines.append(("", "out"))
    drawn = 0
    for text, style in lines:
        if y > H - 44:
            break
        color = {"cmd": "#7EDA9B", "json": "#9CC7E8", "out": "#D6DEE8"}[style]
        for part in wrap_mixed(d, text, ascii_font, cjk_font, max_w) if text else [""]:
            if y > H - 44:
                break
            draw_mixed(d, (x0, y), part, ascii_font, cjk_font, color)
            y += 23
            drawn += 1
    if drawn < len(lines):
        note = "… 输出已按窗口高度截断，完整回显见 docs/examples.md"
        d.rectangle([x0 - 4, H - 38, W - 16, H - 9], fill="#111820")
        draw_mixed(d, (x0, H - 34), note, note_ascii, note_cjk, "#8A98A8")
    img.save(path, "PNG")
    print(f"[截图] {path.name} 真实命令 {len(outputs)} 条，渲染 {drawn} 行")


def _real_window_data(db: Path) -> dict[str, Any]:
    """经界面唯一取数入口（DesktopDataSource）取真实台账数据。"""
    from ae_qc.desktop.data_source import DesktopDataSource
    store = Store(str(db))
    ds = DesktopDataSource(LedgerQueries(store))
    batch_list = ds.list_batches()
    data = {
        "batches": batch_list,
        "rows": ds.unexpected_rows(),
        "pending": ds.pending_count(),
        "pending_writeback": ds.pending_writeback_count(),
        "drafts": ds.pending_attributions(),
        # 左侧细条用台账查询层真实算出的规则通过率，不读 batches 表里未回填的 0
        "stats": {b["batch_ref"]: ds.batch_stats(b["batch_ref"]) for b in batch_list},
    }
    store.close()
    return data


def render_main_window(db: Path, path: Path) -> None:
    """02：真实台账数据渲染主窗口三栏（240 / flex / 360）。"""
    data = _real_window_data(db)
    batches = list(data["batches"] or [])
    rows = list(data["rows"] or [])
    drafts = list(data["drafts"] or [])
    stats = data.get("stats") or {}
    # 归因列只标真实落在台账里的草稿状态，不把每一行都画成「待人工确认」
    draft_by_key = {str(dr.get("row_key", "")): dr for dr in drafts if isinstance(dr, dict)}

    c = TOKENS.get("colors", {}) if isinstance(TOKENS, dict) else {}
    primary = c.get("primary", "#1F5FA9")
    ink = c.get("text", "#1F2933")
    sub = c.get("text_secondary", "#5B6770")
    line = c.get("divider", "#E3E8EF")
    img = Image.new("RGB", (W, H), "#E9EDF2")
    d = ImageDraw.Draw(img)
    d.rectangle([8, 8, W - 8, H - 8], fill="#FFFFFF", outline="#CFD6DF", width=1)
    # 顶栏
    d.rectangle([9, 9, W - 9, 65], fill="#FBFCFE")
    d.text((28, 24), "不良事件上报数据质检系统", font=font(19, bold=True), fill=ink)
    d.text((330, 28), "期间 2026-01 · 期望套件 v2026-01", font=font(13), fill=sub)
    badge = f"待确认归因 {data['pending']} 行"
    d.rounded_rectangle([W - 210, 20, W - 28, 54], radius=14, fill="#FDECEA", outline="#F3B7B0")
    d.text((W - 196, 27), badge, font=font(13, bold=True), fill="#C0392B")

    # 三栏几何
    left, right = 240, 360
    mid_x, mid_w = 8 + left, W - 16 - left - right
    d.line([(8 + left, 65), (8 + left, H - 40)], fill=line, width=2)
    d.line([(W - 8 - right, 65), (W - 8 - right, H - 40)], fill=line, width=2)

    # 左栏：批次列表（按期间分组 + 通过率细条）
    d.text((28, 82), "批次列表", font=font(15, bold=True), fill=ink)
    d.text((28, 110), f"2026-01 · {len(batches)} 个科室 · 细条为规则通过率", font=font(12), fill=sub)
    for i, b in enumerate(batches[:6]):
        y = 142 + i * 76
        ref = str(b.get("batch_ref", "") if isinstance(b, dict) else b)
        st = stats.get(ref, {})
        rate = float(st.get("pass_rate", 0) or 0)
        d.rounded_rectangle([20, y, 8 + left - 12, y + 66], radius=6,
                            fill="#F5F7FA", outline=line)
        d.text((32, y + 10), ref or "示例批次", font=font(13, bold=True), fill=ink)
        d.text((8 + left - 76, y + 11), f"{rate:.0%}", font=font(12), fill=sub)
        d.text((32, y + 27), f"{st.get('passed_rules', 0)}/{st.get('executed_rules', 0)} 条规则通过 · "
                             f"{st.get('unexpected_rows', 0)} 行越界", font=font(11), fill=sub)
        d.rectangle([32, y + 46, 8 + left - 30, y + 54], fill="#DDE4EC")
        bar_w = int((8 + left - 62) * max(0.0, min(1.0, rate)))
        if bar_w > 0:
            d.rectangle([32, y + 46, 32 + bar_w, y + 54], fill="#2E7D4F" if rate > 0.6 else "#B26A00")

    # 中栏：越界行表（真实台账行）
    d.text((mid_x + 20, 82), "越界行清单", font=font(15, bold=True), fill=ink)
    headers = ["科室", "行号", "规则", "越界值", "归因"]
    cols = [mid_x + 20, mid_x + 110, mid_x + 190, mid_x + 370, mid_x + 520]
    d.rectangle([mid_x + 12, 112, mid_x + mid_w - 12, 144], fill=primary)
    for x, htxt in zip(cols, headers):
        d.text((x, 119), htxt, font=font(12, bold=True), fill="#FFFFFF")
    for i, row in enumerate(rows[:18]):
        y = 146 + i * 34
        if i % 2 == 0:
            d.rectangle([mid_x + 12, y, mid_x + mid_w - 12, y + 32], fill="#F7F9FC")
        key = str(row.get("row_key", "")) if isinstance(row, dict) else ""
        dr = draft_by_key.get(key, {})
        state = str(dr.get("state", ""))
        attribution = ("待人工判定" if state == "pending_manual_classification"
                       else "待人工确认" if state == "attributed_pending" else "—")
        cells = [
            str(row.get("dept_code", "") if isinstance(row, dict) else ""),
            str(row.get("row_no", "") if isinstance(row, dict) else ""),
            str(row.get("rule_id", "") if isinstance(row, dict) else "")[:18],
            str(row.get("present_value", row.get("value", "")) if isinstance(row, dict) else "")[:16],
            attribution,
        ]
        for x, txt in zip(cols, cells):
            d.text((x, y + 8), txt, font=font(12), fill=ink)
        d.line([(mid_x + 12, y + 32), (mid_x + mid_w - 12, y + 32)], fill=line)
    if not rows:
        d.text((mid_x + 20, 160), "本批未发现越界", font=font(13), fill=sub)
    d.text((mid_x + 20, H - 78), f"共 {len(rows)} 条越界命中 / {len(draft_by_key)} 个唯一行键"
                                 "（界面展示前 18 条，明细导出不限条数）",
           font=font(12), fill=sub)

    # 右栏：归因确认面板（字段全部取自真实台账草稿，不构造模型结论）
    rx = W - 8 - right + 16
    first = drafts[0] if drafts and isinstance(drafts[0], dict) else {}
    detail = first.get("draft", {}) if isinstance(first.get("draft"), dict) else {}
    first_row = next((r for r in rows if isinstance(r, dict)
                      and r.get("row_key") == first.get("row_key")), {})
    d.text((rx, 82), "归因确认", font=font(15, bold=True), fill=ink)
    d.text((rx, 112), f"草稿状态：{first.get('state', '无草稿')}", font=font(12), fill=sub)
    fields = [
        ("当前行键", str(first.get("row_key", "—"))),
        ("建议目标编码", str(detail.get("suggested_code") or "— 模型不可达，需人工指定")),
        ("置信度", f"{float(detail.get('confidence') or 0):.2f}"),
        ("判定来源", f"{first.get('source', '—')} / {detail.get('degraded_reason', '—')}"),
        ("规则依据", str(first_row.get("basis") or "—")),
    ]
    for i, (label, value) in enumerate(fields):
        y = 150 + i * 62
        d.text((rx, y), label, font=font(12), fill=sub)
        for j, part in enumerate(wrap_pixels(d, str(value), font(12), right - 40)[:2]):
            d.text((rx, y + 20 + j * 16), part, font=font(12), fill=ink)
        d.line([(rx, y + 56), (W - 24, y + 56)], fill=line)
    d.text((rx, 470), f"本批 {data['pending']} 行降级待人工指定 · 越界计数照常计入",
           font=font(12), fill="#B26A00")
    d.rounded_rectangle([rx, 506, rx + 150, 546], radius=5, fill=primary)
    d.text((rx + 46, 517), "确认归因", font=font(13, bold=True), fill="#FFFFFF")
    d.rounded_rectangle([rx + 164, 506, rx + 314, 546], radius=5, fill="#FFFFFF", outline=line, width=2)
    d.text((rx + 210, 517), "驳回", font=font(13), fill=ink)
    d.text((rx, 564), "质控员确认后才会写回质控平台；降级行提供重试入口", font=font(12), fill=sub)

    # 状态栏
    d.rectangle([9, H - 40, W - 9, H - 9], fill="#F5F7FA")
    d.text((28, H - 30), f"来源：文件投递（本批仅含文件来源） · 待确认归因 {data['pending']} · "
                         f"待写队列 {data['pending_writeback']} · 人工确认是唯一放行口",
           font=font(12), fill=sub)
    img.save(path, "PNG")
    print(f"[截图] {path.name} 真实批次 {len(batches)} 个，真实越界行 {len(rows)} 行，"
          f"真实待确认草稿 {len(drafts)} 条")


def render_rectify_list(out: Path, path: Path) -> None:
    """03：真实导出 xlsx 表头与前几行 + 真实证据包目录。"""
    from openpyxl import load_workbook
    xlsx = sorted(out.glob("整改清单*.xlsx"))
    table_rows: list[list[str]] = []
    if xlsx:
        wb = load_workbook(xlsx[0], read_only=True, data_only=True)
        ws = wb[wb.sheetnames[0]]
        # 第 0 行是 xlsx 自带表头，画面上方已单独绘制，这里只取真实数据行
        for r in ws.iter_rows(min_row=2, max_row=9, values_only=True):
            table_rows.append(["" if v is None else str(v)[:48] for v in r])
        wb.close()
    else:
        table_rows = [["（未找到真实导出文件）"]]

    img = Image.new("RGB", (W, H), "#E9EDF2")
    d = ImageDraw.Draw(img)
    d.rectangle([8, 8, W - 8, H - 8], fill="#FFFFFF", outline="#CFD6DF")
    d.rectangle([9, 9, W - 9, 52], fill="#217346")  # Excel 绿
    d.text((26, 18), "整改清单_" + (xlsx[0].stem.split("_", 1)[-1] if xlsx else "all") + ".xlsx  —  真实导出产物",
           font=font(16, bold=True), fill="#FFFFFF")
    d.text((26, 66), "发给各科室安全员：哪一行、改成哪个编码（不含排名 / 扣分 / 患者可识别明细）",
           font=font(13), fill="#5B6770")

    # 表格：列号 + 表头 + 真实数据行（列宽撑满画面，不留大片空白）
    widths = [64, 96, 132, 210, 230, 206, 454]
    xs, x = [24], 24
    for w in widths:
        x += w
        xs.append(x)
    body_h = 32 * max(1, len(table_rows[:8]))
    d.rectangle([24, 100, x, 124], fill="#E8EEF6")
    d.text((30, 104), "行", font=font(12), fill="#5B6770")
    for i in range(1, 8):
        d.line([(xs[i], 100), (xs[i], 124 + body_h)], fill="#D7DEE7")
    headers = ["科室", "行号", "越界项", "现值", "应改为", "依据"]
    for i, h in enumerate(headers):
        d.text((xs[i + 1] + 8, 104), h, font=font(12, bold=True), fill="#1F2933")
    for ri, row in enumerate(table_rows[:8]):
        y = 124 + ri * 32
        if ri % 2 == 1:
            d.rectangle([24, y, x, y + 32], fill="#F7F9FC")
        d.text((30, y + 8), str(ri + 2), font=font(12), fill="#8A94A0")
        for ci in range(min(6, len(row))):
            txt = str(row[ci]) if ci < len(row) else ""
            parts = wrap_pixels(d, txt, font(12), widths[ci + 1] - 14)
            shown = parts[0] if parts else ""
            if len(parts) > 1 and shown:
                shown = shown[:-1] + "…"
            d.text((xs[ci + 1] + 8, y + 8), shown, font=font(12), fill="#1F2933")
        d.line([(24, y + 32), (x, y + 32)], fill="#E3E8EF")

    # 证据包目录树（真实文件）
    ty = 124 + 32 * max(1, len(table_rows[:8])) + 40
    d.text((26, ty), "审计证据包目录（真实导出文件）", font=font(15, bold=True), fill="#1F2933")
    ty += 34
    tree = sorted(p.name for p in out.iterdir() if p.is_file())
    for name in tree[:8]:
        d.text((44, ty), "├─ " + name, font=font(13, mono=False), fill="#5B6770")
        ty += 26
    d.text((26, H - 56), "证据包含规则套件版本、条目 basis、确认人与时间、模型元数据与写回回执；"
                         "年度汇总底稿由院内人工上传，本产品不自动外发。",
           font=font(12), fill="#5B6770")
    img.save(path, "PNG")
    print(f"[截图] {path.name} 真实表格 {len(table_rows)} 行，真实产物文件 {len(tree)} 个")


def write_examples(outputs: list[tuple[str, str]], path: Path) -> None:
    """把未被终端窗口截断的完整真实回显写成可核对的文档。"""
    lines = [
        "# 实测回显（真实运行，逐条可复制）", "",
        "本文件由 `python3 scripts/capture_screenshots.py` 采集，内容全部是真实命令的",
        "完整 stdout；`docs/screenshots/01-cli-batch-run.png` 只按窗口高度展示前若干行。",
        "命令都在临时工作目录里执行，端点使用本机不可达占位地址，未实连院内系统。", "",
    ]
    for label, body in outputs:
        lines += ["```bash", f"$ {label}", "```", "", "```text", body or "(无输出)", "```", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[回显] {path.name} 记录 {len(outputs)} 条真实命令的完整 stdout")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ae_qc_capture_") as tmp:
        work = Path(tmp)
        outputs, artifacts = _run_real_commands(work)
        render_terminal(outputs, OUT_DIR / "01-cli-batch-run.png")
        render_main_window(artifacts["db"], OUT_DIR / "02-main-window.png")
        render_rectify_list(artifacts["out"], OUT_DIR / "03-rectify-list.png")
        write_examples(outputs, PROJECT_ROOT / "docs" / "examples.md")
    for p in sorted(OUT_DIR.glob("*.png")):
        print(f"  {p.name}: {p.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
