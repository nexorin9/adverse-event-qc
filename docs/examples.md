# 实测回显（真实运行，逐条可复制）

本文件由 `python3 scripts/capture_screenshots.py` 采集，内容全部是真实命令的
完整 stdout；`docs/screenshots/01-cli-batch-run.png` 只按窗口高度展示前若干行。
命令都在临时工作目录里执行，端点使用本机不可达占位地址，未实连院内系统。

```bash
$ python3 -m ae_qc.cli rule-set validate --version v2026-01 --against v2026-02
```

```text
{"command": "rule-set validate", "version": "v2026-01", "entries": 9, "meta": {"effective_from": "2026-01", "note": "院内首版期望套件；等级允许集合取自已核官方附件，事件类别目录为院内自维护（经验推断·待核实）"}, "against": "v2026-02", "diff": {"added": [], "removed": ["AE-SEV-CLASS-SET"], "modified": ["AE-SEV-LEVEL"]}}
```

```bash
$ python3 -m ae_qc.cli batch run --inbox inbox/ --out out/ --db ledger.sqlite --dept D01
```

```text
{"command": "batch run", "batch_ref": "D01-2026-01", "rule_set_version": "v2026-01", "checked_rows": 20, "executed_rules": 11, "skipped_rules": 0, "unexpected_rows": 21, "list_path": "out/越界行清单_D01-2026-01.csv", "db": "ledger.sqlite"}
```

```bash
$ python3 -m ae_qc.cli batch run --inbox inbox/ --out out/ --db ledger.sqlite --dept D02
```

```text
{"command": "batch run", "batch_ref": "D02-2026-01", "rule_set_version": "v2026-01", "checked_rows": 20, "executed_rules": 11, "skipped_rules": 0, "unexpected_rows": 20, "list_path": "out/越界行清单_D02-2026-01.csv", "db": "ledger.sqlite"}
```

```bash
$ python3 -m ae_qc.cli batch run --inbox inbox/ --out out/ --db ledger.sqlite --dept D03
```

```text
{"command": "batch run", "batch_ref": "D03-2026-01", "rule_set_version": "v2026-01", "checked_rows": 20, "executed_rules": 11, "skipped_rules": 0, "unexpected_rows": 21, "list_path": "out/越界行清单_D03-2026-01.csv", "db": "ledger.sqlite"}
```

```bash
$ python3 -m ae_qc.cli findings export --db ledger.sqlite --out out/
```

```text
{"command": "findings export", "path": "out/整改清单_all.xlsx", "row_count": 62, "sheets": ["整改清单"]}
```

```bash
$ python3 -m scripts.scale_check --output scale.md
```

```text
规模：10 科室 × 3 期 = 30 批 / 600 行
正确性：注入 90 行越界，检出 90 行，差异 0
性能：单批最大 0.0007s / 阈值 15.0s → 通过
结论：scale.md
```

```bash
$ 归因编排（模型端点不可达，真实降级路径）
```

```text
输入越界命中 62 条 · 成功 0 条 · 降级 62 条 · 台账待人工确认 34 个唯一行键（同一行命中多条规则按 row_key 去重）
```
