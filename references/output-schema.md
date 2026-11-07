# 可复核清单的数据格式

## 目录
1. 文件与生成命令
2. 报告和来源
3. 检索日志
4. 热点及材料
5. 评分与样品渠道
6. 自动校验的边界

## 1. 文件与生成命令

保存 UTF-8 JSON，`schema_version: 1`。所有未知计数用 `null`，不要填 0。叙述字段用纯文本；渲染器转义 HTML，不接受内嵌 HTML。论文链接必须真实、公开可访问且只使用 http/https，不含 API key、身份凭据或临时签名。

```bash
python <本技能目录>/scripts/render_report.py report.json --validate-only
python <本技能目录>/scripts/render_report.py report.json --output report.html --normalized-json report-normalized.json
```

`--normalized-json` 可选，保留因证据不足而降级后的名单；不覆盖输入。`--fail-on-downgrade` 使主推荐降级时退出 2，便于正式发布前检查。结构错误退出 1；正常生成或仅有非阻断警告退出 0。

HTML 不依赖外部字体、服务或脚本。页首简表列出主题及评分；点击话题后显示轻量材料卡，卡片直接列出入选理由、物理重要性/可做性与生长团队线索。再点击材料才展开历史、单晶、查新和机制证据。具体合成配方默认按需另做，不要求第一轮逐个深挖。每条引文跳转到报告中的出处，出处链接到真实论文。启用 JavaScript 时支持文本筛选、全部展开与打印；禁用时原生展开功能仍可使用。

## 2. 报告和来源

| 字段 | 类型与含义 |
|---|---|
| `title` | 必填，报告名称 |
| `as_of` | 必填，用户时区对应的 ISO 日期，如 `2026-11-07` |
| `mode` | `scout` 探索或 `audit` 语料审计；默认 scout |
| `selection_mode` | `discover` 发现热点或 `topic` 用户指定话题 |
| `scope`, `summary` | 检索范围与本轮主要结论 |
| `limitations` | 局限的字符串数组 |
| `requirements.minimum_material_age_years` | 历史年限阈值，默认 20 |
| `requirements.require_google_scholar` | 默认 true；体现用户要求的 Scholar 补检通道。不能为获得主推荐而静默改为 false |
| `run.id`, `run.previous_run_id` | 本期与上期记录标识，首期 previous_run_id 为 null |
| `run.change_summary` | 相对上期新增、修正、降级、失效和未变化项的简述数组 |
| `sources`, `searches`, `topics` | 必填数组；可以为空，不能杜撰内容填满 |

每个 `sources[]` 保存：

```json
{
  "id": "来源的稳定内部标识",
  "title": "真实论文或资料标题",
  "url": "真实公开来源URL",
  "year": null,
  "kind": "original_paper / preprint / database / institutional_record 等",
  "access_level": "full_text / abstract / metadata / user_provided / not_accessed / unknown",
  "note": "该来源支持什么；已读页码、图表、摘要段或补充材料位置；不支持什么"
}
```

此处为字段说明，不能把占位内容当研究结果。`access_level` 必须准确；只读搜索摘要不能记为 full_text。要满足历史/单晶/机制主推荐门槛，至少一个相应来源须有有效链接、标题及 `full_text`、`abstract` 或 `user_provided` 阅读记录。即使 DOI 存在，也必须人工核对该论文是否支持具体断言。

## 3. 检索日志

每个 `searches[]` 保存以下字段，供材料 `novelty.search_ids` 引用：

| 字段 | 含义 |
|---|---|
| `id`, `source` | 唯一检索标识和真实来源名称；Scholar 写 `Google Scholar (browser)` |
| `query` | 完整原始查询式，不只写“检索过” |
| `searched_at` | ISO 日期或日期时间 |
| `status` | `complete_for_query`、`manual`、`partial`、`failed`、`blocked` 或 `not_attempted` |
| `coverage_notes` | 日期、过滤、别名、页码、截断、全文覆盖、浏览器观察情况等 |
| `result_count` | 当前查询已确认条目数或 null；不使用 Scholar 的“约 n 条”作为精确论文数 |
| `reviewed_count` | 实际审查条目数或 null |
| `result_ids` | 实际记录的 DOI/arXiv/内部文献标识数组；空数组不自动代表零命中 |
| `confirmed_empty` | 仅当成功检索且明确观察到无结果时 true，配合 result_count=0 |
| `truncated` | 是否截断；true 时不能当完整查询 |

`complete` 作为旧数据的 `complete_for_query` 别名兼容。查询完成仅指所列查询范围，不代表全球文献全覆盖。浏览器正常访问 Scholar 的少量页面记为 `manual`；记录正数 `reviewed_count`，或明确空结果的 `confirmed_empty: true`。验证码、访问限制和未尝试分别记为 blocked/failed/not_attempted，不算已完成补检。

正式候选的查新需要两个不同的已审查来源，并至少有一个非 Scholar 学术源完成所列查询；Scholar 浏览器补检可以作为第二来源。两个不同查询拼写不能伪装成两个独立来源。查看 Scholar 不能代替可枚举语料的定量计数。

## 4. 热点及材料

每个 `topics[]` 包含 `id`、`title`、`question`、`heat_status`、`heat_evidence_ids` 和 `materials`。`heat_status` 解释近期关注证据及其边界，`heat_evidence_ids` 指向来源 id。探索模式不得用固定主题或网页样本编造热度名次、增长率；渲染器不会自行计算或补写这些数字。

每个 `materials[]` 使用下列结构；同一材料可出现在不同主题，但同一主题内 id 唯一。不同晶相、掺杂/缺位或相关压力条件应有不同材料身份。

| 字段 | 含义 |
|---|---|
| `id`, `formula`, `aliases` | 稳定材料标识、作者原始化学式、经核实的化学式与旧名别名数组 |
| `phase` | 晶相、结构型/空间群及必要的组分、维度和条件 |
| `status` | `recommended` 主推荐；`watchlist` 待核查；`benchmark` 基准；`excluded` 未入选 |
| `reason` | 在当前话题中入选或不入选的具体原因 |
| `historical.year` | 本次找到并核验的历史实验年；不是自动认定“首次发现年” |
| `historical.evidence_ids`, `historical.note` | 原始证据及具体支持的历史事实 |
| `single_crystal.grade` | C0–C4，按 protocol.md 定义 |
| `single_crystal.evidence_ids`, `method`, `dimensions`, `note` | 单晶出处、原文生长方法、样品尺寸及局限；未知留空或写未知 |
| `novelty.status` | `audited`、`partial` 或 `unknown` |
| `novelty.scope`, `search_ids`, `note` | 材料×问题×条件的查新边界、检索记录及结论 |
| `novelty.citation_chain_checked` | 是否已审查后向/前向引用链，布尔值 |
| `novelty.citation_chain_note` | 查了哪篇、哪个引用方向和哪些边界；不能只填 true |
| `mechanism.facts`, `evidence_ids` | 有证据支持的材料/物理事实 |
| `mechanism.hypothesis` | 与热点的机制桥梁，明确标记待验证 |
| `mechanism.counterevidence` | 已有相反结果、竞争解释、原文局限或待补查事项 |
| `mechanism.next_experiment` | 能区分假说的最小验证实验与结果解释 |
| `superconductivity.level` | intrinsic_bulk / pressure_induced / doping_induced / proximity / theory / family_analogy / probe_only / none / unknown |
| `superconductivity.note`, `evidence_ids` | 超导关联的真实对象、条件及出处 |
| `changes` | 本期该材料的新增发现、修正或资格变化，字符串数组 |

不要把同族超导或超导探针写成候选材料本征超导。不要用计算目录收录作为已生长单晶的证据。每条关键断言的证据位置放入对应来源 `note`，尤其记录补充材料中的表号/页码。

## 5. 评分与样品渠道

`topics[].scores` 与 `materials[].scores` 均可选。分数是启发式判断，统一 `kind: "judgment"`，不能写成独立评测、统计热度排名或发现概率。所有子分取 0–5；未知为 null。每个非空子分必须在 `reasons` 的同名键给出理由，并以 `evidence_ids` 指向至少一个已阅读来源。否则该子分被改为未评分。

| 对象 | 子分与默认权重 | 总分 |
|---|---|---|
| 主题 | importance 40%、momentum 35%、feasibility 15%、breadth 10% | 0–100 |
| 材料 | importance 45%、feasibility 35%、novelty 10%、evidence 10% | 基础0–100 |
| 材料超导加分 | superconductivity_bonus 0–5，单独有理由与证据 | 最高总分105 |

`importance` 说明科学问题重要性、可区分机制或潜在概念价值；`feasibility` 说明单晶已有证据、样品渠道及必要实验条件。不要把它们简化为新闻关注或论文数量。题目的 momentum 可以根据有日期的原始证据作定性判断，但必须直说不是语料计量。

所有基础子分齐全才计算基础总分：`sum(子分 / 5 × 权重)`。任一基础子分未知，综合显示未评分；不把未知当0，不对剩余项重分配权重。材料基础总分齐全但超导加分未知时，显示“基础分 + 加分未评分”，不假装已有完整105分总分。正向的 novelty 稀缺性分数只有限定范围查新、引用链和必须的 Scholar 补检满足要求后才可评分。

一个有限例外：`status` 为 benchmark 或 excluded，`novelty` 明确为0，并且已有**同问题直接研究的已读来源与具体理由**时，可以保留0分，不必等到 Scholar 和其他来源全查完。已找到明确同类工作足以降低新颖性，证明全球稀缺则需要更广覆盖。这个例外不能用于 recommended/watchlist，不适用于未知分数或正向稀缺性分数，也不能仅因为某材料总体热门就机械给所有问题0分。渲染器检查状态、数值、理由和来源是否存在；同问题的事实对应仍由主代理核对。

生长/样品渠道字段：

```json
{
  "sourcing": {
    "growers": [
      {
        "name": "已有公开证据的人或团队",
        "institution": "公开机构名称",
        "role": "实际生长团队、论文作者或通讯作者等；不可混同",
        "evidence_ids": [],
        "url": null
      }
    ],
    "availability_status": "unknown",
    "availability_evidence_ids": [],
    "note": "真实线索及局限；不要因为发过论文就说目前能提供样品",
    "checked_at": null
  },
  "synthesis": {
    "status": "not_requested",
    "note": "详细合成方法在用户选择候选后按需补查"
  }
}
```

`availability_status` 为 confirmed_available / public_offer / author_route / unknown。前两者必须有专门的 `availability_evidence_ids` 与 `checked_at`，仅有作者或生长论文不能证明样品可提供；缺少则降为 author_route 或 unknown。growers 的 role 与机构亦需原文或公开页面支持，不推断作者本人一定负责生长。此模块只记录公开信息，不自动联系作者。

`synthesis.status` 为 not_requested / queued / reviewed。C2的历史单晶测量可支持轻量候选的样品存在判断，不要求所有材料预先达到C3的完整配方。详细生长路线、条件和失败原因另一次按需调用时补入同一材料记录。

## 6. 自动校验的边界

主推荐必须有：满足年限的历史实验年份及已读来源、C2以上单晶来源、明确晶相与理由、已知机制事实及来源、待验证假说、下一步实验、有范围的多源查新、引用链记录，以及默认必需的 Scholar 补检。

缺少任一条件时，渲染器将该条降为 `watchlist`，在卡片显示具体缺口并发出警告。未知来源会显示未核验，不会生成假引文。访问失败却写结果为0时会改为 null。无效、可执行或含凭据的 URL 不会变成可点击外链，也不能满足证据门槛。

结构校验不能验证论文内容是否真的支持断言，也不能自动判定两个名称是否独立数据库。研究者/主代理仍须阅读证据、核实材料身份、日期和查询边界。此格式用于持久数据库的展示快照；真实原始记录、不可变 run 与文献去重请按 `references/database.md` 执行。
