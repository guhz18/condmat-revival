# 研究资料库与增量复用

## 目录

1. 每次调用的持久化流程
2. 数据层级与身份
3. 缓存和增量规则
4. 命令接口
5. 导入格式
6. 供样、合成与结论更新
7. 边界与保护

## 1. 每次调用的持久化流程

`scripts/research_db.py` 使用 Python 标准库和 SQLite，schema 版本为1。它负责记录、事务、查找、缓存判断和一致性快照，不负责联网检索或调用文件保存服务。

每次运行按以下顺序处理：

1. 使用当前环境的 Library 技能按确切文件名查找 `condmat-revival-research.sqlite3`。已有文件必须恢复并保留原 `library_file_id` 和版本；不能为每周/月另建独立空库。若用户明确指定已有资料库，优先使用它。
2. 在当前工作区恢复数据库。首次无库时运行 `init`。先查已有主题、材料、结论、历史查询和最近成功截止日，决定本轮新增工作。
3. 根据请求选择 discovery（发现热点）或 targeted（用户指定话题）；仅对用户实际要求的研究深度展开。生成列表通常为 screening；供样路径为 sourcing；详细生长/合成为 synthesis。
4. 创建唯一 `run_id`，完成一批可复核研究后以 `import-run` 事务导入。允许复用已有文献、事实和判断，不重复撰写相同证据。按需形成周/月报告不等于自动创建定时任务。
5. 用 `snapshot` 生成经完整性验证的单文件备份；使用当前文件保存能力将它更新到**同一个持久化文件身份**。新建时建立一次身份，后续替换其版本。不要直接复制正在使用的 WAL 数据库文件。
6. 记录/展示本次新增、复用、修正、降级和未完成项。保存失败时保留本地快照并说明没有成功持久化，不能声称下次一定能读到。

并行研究者输出独立 JSON 片段，由一个写入者合并和导入；不要让多个研究者各自恢复并覆盖同一持久库。遇到远端版本冲突，重新恢复最新版，把当前未入库 run 再导入并保存；相同 run 是幂等的，不删除版本检查。冲突的同名 run 必须人工核对或用新的 run_id。

## 2. 数据层级与身份

| 表/对象 | 用途 |
|---|---|
| `runs` | 不可变的每次运行输入快照、截止日、模式和范围 |
| `documents` / `document_aliases` | 文献稳定身份和 DOI、arXiv、来源 ID；最新元数据视图 |
| `materials` | 显式材料 ID、化学式、晶相、样品形态；不按约分式自动合并 |
| `topics` | 主题定义及最新标签 |
| `entity_versions` | 文献、材料、主题的每次导入版本 |
| `searches` | 原查询、时间范围、研究深度、成功/失败、结果 IDs及覆盖边界 |
| `claims` / `claim_evidence` | 事实、历史解释、假说、局限与原始出处/页码/章节 |
| `assessments` / `assessment_claims` | 材料×主题的判断版本、入选理由、评分及来源事实 |
| `run_items` | 每次报告使用的新旧对象，支持复用而不重做 |

DOI去前缀并统一大小写；arXiv去版本后缀、保留原始编号。不会仅按标题相近、同作者或相同化学式合并。多个不同既有文献身份被一个新别名桥接时，导入会拒绝并要求明确核对；不要自动认定它们是同一次研究。为同一工作添加 DOI/arXiv 版本关联前，先读原记录核实。

材料必须给出 `identity.formula`、`identity.phase`、`identity.sample_form`。相不明可显式写 `unresolved`，但不能作为明确晶相的主推荐。相变、薄膜/体块、掺杂或成分差异需要不同材料 ID。已记录的 identity 不可静默改写；新发现身份应创建准确的新 ID，并在 metadata/claims 中写出与旧记录的关系。

## 3. 缓存和增量规则

查询缓存键严格包含：

`source + query + filters + query_version + research_depth + phase + problem + material_id`

- `research_depth` 为 screening / sourcing / synthesis。也接受同值的 `level` 字段，但建议统一用前者。
- `phase`、`problem` 必须显式记录。全领域发现可写 `not_material_specific` 与当前研究问题；不确定晶相写 `unresolved`，不能缺省后偷偷复用。
- `material_id` 可为null，表示主题级检索。日期、语言、文献类型、类别和其他筛选条件放入 `filters`。
- 保留查询内部字符与大小写；不要把 Co 与 CO、不同引号或搜索语法归为相同查询。键只做 JSON 字段顺序规范化和外层空白清理。
- 仅 `complete_for_query`、未过TTL、非未来时间戳的结果可视为该查询已完成。partial / failed / manual 不会被当作完成；失败结果计数必须为null，不能写0。
- 同范围最新一次失败但此前完整结果仍在TTL内，可以复用此前快照；输出会同时披露最新失败尝试。缓存有效不意味着当前全世界文献无变化。
- TTL由任务指定：热点扫描通常7天；旧论文原文证据可长得多，例如365天；供样信息通常需7–30天内重核。用户要求“重新查新”或出现反证时主动失效，别用TTL拒绝用户要求。

**按月更新不重做全历史：**先读取该主题/材料最后一个完整检索的结束日，查询新增日期段，并可向前重叠7–14天处理延迟索引；重叠记录按稳定标识去重。精确缓存不会自动推理范围包含关系，规划者须明确记录哪些历史区间复用、哪些新增区间已完成。带缺口或失败的区间须补查。旧生长事实可以跨报告复用，但一个 screening 查询绝不能证明 synthesis 工作完成。

引用记录和事实可复用；“目前少人研究”“现有样品可得”“这家实验室可供样”等时效结论必须按当前范围重新评估。数据库不会自动生成新颖性或供样事实。

## 4. 命令接口

把 `<技能目录>` 替换成当前技能目录，`<工作区>` 替换成实际路径：

```bash
python <技能目录>/scripts/research_db.py --db <工作区>/working.sqlite3 init
python <技能目录>/scripts/research_db.py --db <工作区>/working.sqlite3 search --kind materials --text Rb
python <技能目录>/scripts/research_db.py --db <工作区>/working.sqlite3 search --kind runs --id <run_id>
python <技能目录>/scripts/research_db.py --db <工作区>/working.sqlite3 cache-status --scope query-scope.json --ttl-days 7
python <技能目录>/scripts/research_db.py --db <工作区>/working.sqlite3 cache-status --needs-refresh --ttl-days 7
python <技能目录>/scripts/research_db.py --db <工作区>/working.sqlite3 import-run --file run.json
python <技能目录>/scripts/research_db.py --db <工作区>/working.sqlite3 snapshot --output <工作区>/condmat-revival-research.sqlite3
```

命令结果为JSON；错误返回非零退出码和明确原因。`import-run --file -`可从stdin读JSON。`search`支持 `--kind`、`--text`、`--id`、`--run-id`、`--limit`（每类最多200条）。`cache-status --as-of`可以指定带时区的时间戳作可复核判断。快照目标已存在时，先核对用途再显式使用`--replace`，避免覆盖无关文件。

## 5. 导入格式

顶层是对象，包含 `schema_version:1`、`run`，以及按需出现的 documents/materials/topics/searches/claims/assessments/reuse 数组。不需要重复导入数据库中已经存在的实体；用 `reuse` 把其加入本次报告。

以下仅是字段说明，**不是实际研究结果**：

```json
{
  "schema_version": 1,
  "run": {
    "run_id": "2026-11-07-topic-example-01",
    "as_of": "2026-11-07T15:00:00+08:00",
    "mode": "scout",
    "topic_mode": "targeted",
    "scope": {"topic_id": "topic:example", "research_depth": "screening"}
  },
  "documents": [{
    "document_id": "doc:example-source",
    "title": "原文准确题名（示例占位）",
    "url": "https://example.org/paper",
    "metadata": {"full_text_access": "abstract_only"}
  }],
  "materials": [{
    "material_id": "material:example-phase",
    "name": "示例材料（占位）",
    "identity": {"formula": "Example", "phase": "unresolved", "sample_form": "unknown"},
    "aliases": ["人工核实的别名"]
  }],
  "topics": [{"topic_id": "topic:example", "label": "示例主题", "definition": "明确的物理问题"}],
  "searches": [{
    "search_id": "search:example-01",
    "source": "google_scholar_browser",
    "query": "精确保存的检索式",
    "filters": {"from": "2021-11-07", "to": "2026-11-07"},
    "research_depth": "screening",
    "phase": "unresolved",
    "problem": "明确的目标物理问题",
    "material_id": "material:example-phase",
    "retrieved_at": "2026-11-07T15:00:00+08:00",
    "status": "partial",
    "result_ids": ["doc:example-source"],
    "result_count": 1,
    "reported_total": null,
    "coverage": "只读了结果第一页；不能据此判断全部文献",
    "method": "browser",
    "pages_reviewed": 1,
    "truncated": true
  }],
  "claims": [{
    "claim_id": "claim:example-01",
    "subject_type": "material",
    "subject_id": "material:example-phase",
    "claim_type": "limitation",
    "text": "未核验单晶生长，不能进入正式实验推荐",
    "evidence": []
  }],
  "assessments": [{
    "assessment_id": "assessment:example-01",
    "topic_id": "topic:example",
    "material_id": "material:example-phase",
    "tier": "watchlist",
    "research_depth": "screening",
    "reasons": ["证据尚不完整，保留待核查"],
    "claim_ids": ["claim:example-01"],
    "scores": {"physics_importance": null, "experimental_feasibility": null, "overall": null},
    "sourcing": {"status": "unknown", "public_people": [], "institutions": [], "checked_at": null},
    "synthesis": {"status": "not_started", "detail": "用户选择后再展开"}
  }],
  "reuse": []
}
```

`query-scope.json`只需包含与上例 search 一致的 source/query/filters/research_depth/phase/problem/material_id，以及可选query_version。运行模式 `scout` 不能因为数据库已建成就改成 `audit`。

文献可加 `doi`、`arxiv_id`、`aliases:[{"type":"openalex","value":"W…"}]`。如果同一文献在先前以arXiv ID记录，后来经核实新增DOI，稳定身份仍沿用既有记录，返回 `document_id_map` 告知归并结果。同一run内重复文献身份会拒绝导入；先合并其元数据和来源。

事实/解释的 `claim_type` 分别为fact/historical_interpretation，必须附至少一个evidence：

```json
{"document_id":"doc:example-source","url":"https://example.org/paper","locator":"摘要，或页码/章节/图表编号","relation":"supports"}
```

`relation`可为supports/contradicts/context/basis_for_hypothesis。hypothesis和limitation可以没有直接证据，但必须明确其性质。`subject_type`为material/topic/document/general。

## 6. 供样、合成与结论更新

把供样可能性与已长过单晶分开：

| `sourcing.status` | 含义 |
|---|---|
| confirmed | 有日期明确、针对当前样品的可得确认，并附证据；不要从作者名单推断 |
| public_offer | 存在公开供样/销售/共享说明，具体规格和当前可得性仍需核验 |
| author_route | 找到公开论文作者/机构及相关生长工作，只代表可以由用户进一步询问的线索 |
| unknown | 没有明确来源，或资料过期/无法核实 |

用sourcing对象保留public_people、institutions、source_urls、checked_at、claim_ids和规格说明；仅保存公开的专业来源。不会自动发送邮件或联系作者。拿到用户明确的联系授权仍需先解析接收者。

用户点选某个材料要求“展开生长/合成调研”时，创建research_depth=synthesis的新任务，复用原始历史证据，仅补读必要全文、参数、设备与失败经验；不要自动对整个材料库做昂贵的全面合成调研。HTML展示已缓存结果，离线点击不会触发联网计算，不能承诺实时展开研究。

scores对象可保留physics_importance、experimental_feasibility、overall和各分量的理由/证据；缺失信息留null。不从“评分字段存在”推断评分已经获得物理验证。research_depth不会自动提升synthesis.status。

新事实/修正用新claim_id并设置supersedes_claim_id；新判断用新assessment_id并设置supersedes_assessment_id。旧记录完整保留。assessment的新旧对象须为同一material-topic组合；旧事实的supersedes必须属于同一研究对象。相同run_id + 相同原始JSON为幂等重试；不同内容的同名run会被拒绝。

`reuse`格式为`{"kind":"claims","id":"claim:已有编号"}`；kind可为documents/materials/topics/searches/claims/assessments。复用不意味着重新核验；报告中保持原证据日期，过期的时效判断仍需新评估。

## 7. 边界与保护

- 查询的reported_total如果存在，complete_for_query必须与结果IDs数量一致；不一致一律partial。结果IDs应为唯一来源记录，不把数据库去重数与源端总条目数混用。
- run导入是单事务，任何身份、证据或格式冲突都回滚。推荐tier至少须有带出处的claim，但物理硬门槛仍由研究协议和报告校验负责；数据库不证明实验正确性。
- 只保存普通公开HTTP(S)来源地址，不存API密钥、Authorization、Cookie、浏览器状态、签名下载URL。脚本拒绝常见凭据字段/URL参数；不能识别所有藏在自由文本中的秘密，导入前仍需检查。
- snapshot通过SQLite backup API生成，检查integrity_check和foreign_key_check；不依赖“复制.db再漏掉-wal”的不安全方式。
- 搜索实体默认显示最新元数据；查看旧run的payload可以恢复当时证据和判断。数据库是研究记录，不是全世界文献的完整镜像。
- 本脚本不自动安排周期任务、不发送消息、不宣称Google Scholar已访问。只有真实浏览器检索成功时才能记录相应查询结果。
