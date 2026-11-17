# 规则族边界用例索引表（EDGE_INDEX）

本表与 `rule_edge_cases.json` 一一对应：`tests/test_rule_edge_cases.py` 会断言
**索引表条数 == JSON 用例数**、**两侧用例 ID 集合完全相同**、**每个规则族恰好 3 条**。
新增边界用例时，JSON 与本表必须同步补行，避免查漏。

统计：7 种规则族 × 3 个边界用例 = **21 条**（`≥21` 达标）；每条 JSON 用例都含
`kind / case / expectation / why / rows / entry`，`why` 写明「期望行为 + 为什么这样期望」。

| 用例 ID | 规则族 | 边界类型 | 期望行为 | 为什么这样期望 | JSON 用例 |
| --- | --- | --- | --- | --- | --- |
| SM-01 | set_membership | 空 value_set | 加载期拒绝并点名 value_set | 空集合留到运行期会把所有值判越界，等于用坏口径污染整批 | cases[0] |
| SM-02 | set_membership | 全空列 / 混合类型 | 4 行逐行计越界，success=false | 空值三口径与非字符串类型不能静默吞或崩溃 | cases[1] |
| SM-03 | set_membership | 归一 / 超长文本 | 归一后命中 2 行、越界 2 行 | 比对先按参数归一且保留原值；超长文本按内容判越界不丢证据 | cases[2] |
| NN-01 | not_null | 空值三口径 | 3 行越界，null_counts 各 1 | 没填 / 空串 / 空格是三种形态，分开计数才查得出成因 | cases[3] |
| NN-02 | not_null | null_policy=allow | 允许空判通过但观察值仍留痕 | 允许空不代表没看见，口径要可追溯而非静默跳过 | cases[4] |
| NN-03 | not_null | 列根本不存在 | 3 行按 None 口径计越界，不抛 KeyError | 缺列若静默通过会把结构问题伪装成合格 | cases[5] |
| TF-01 | type_format | 混合类型 / 多格式日期 | 三种日期写法归一通过，2 条非法值定位 | 日期写法混杂是常态，非法值要逐行说清期望格式 | cases[6] |
| TF-02 | type_format | 时间字段缺失 | 3 行按 empty 原因计数 | 留空不能整体绕过时间格式口径 | cases[7] |
| TF-03 | type_format | 超长文本 / 枚举 | A 通过，超长文本与空值共 2 条越界 | 超长自由文本按内容判越界，不截断不崩溃 | cases[8] |
| TO-01 | time_order | 时间字段半缺 | 2 行半缺逐行计越界 | 缺一侧无法比较，跳过等于可被留空绕过 | cases[9] |
| TO-02 | time_order | 并列时间 | 通过、越界 0、equal_pairs=1 | 相等合法，但并列数要进观察值以便事后解释 | cases[10] |
| TO-03 | time_order | 倒挂 / 格式不可解析 | 两类失败共 2 条都定位 | 解析不出来不等于没有倒挂，不能静默通过 | cases[11] |
| CCD-01 | cross_column_derived | 派生列半缺 | 2 行半缺逐行计越界 | 半缺若跳过，把类别列留空即可绕过派生规则 | cases[12] |
| CCD-02 | cross_column_derived | 来源值无派生映射 | 目录外值显式标记，合法行照常通过 | 映射表里没有的值不能猜、不能崩，要留痕交人工 | cases[13] |
| CCD-03 | cross_column_derived | 映射不一致 | 1 条越界且两侧实际值都保留 | 质控员要判断改等级还是改类别，只给「不一致」不够 | cases[14] |
| AR-01 | alias_resolution | 别名未命中 | 2 行标未命中且 needs_model_merge=true | 未命中是待归并信号，不是空值也不是合规 | cases[15] |
| AR-02 | alias_resolution | 别名命中（归一后） | 2 行命中、越界 0、alias_hits=2 | 命中即确定性判定不调模型，空格差异归一后仍要命中 | cases[16] |
| AR-03 | alias_resolution | 空别名表 | 全部未命中，alias_table_size=0 | 目录资产缺失不能静默通过，要显式点明 | cases[17] |
| CC-01 | catalog_coverage | 缺失类目 | 1 条提示，unexpected_count=0 且 success=true | 反向覆盖是提示级，不能混进错误越界数压低通过率 | cases[18] |
| CC-02 | catalog_coverage | 全覆盖 | 0 条提示、0 越界 | 不应制造噪音提示，否则真正缺失会被漏看 | cases[19] |
| CC-03 | catalog_coverage | 不适用科室 | 0 条提示，out_of_scope=true | 范围外科室报错属误报，观察值留痕便于回看 | cases[20] |
