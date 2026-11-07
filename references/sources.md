# 来源与可替换工具

核查日期2026-11-07；服务合同会变，每次首次使用或报错时读当前官方文档。下表是路由指南，不代表接口在当前环境已连接。主代码仅内置arXiv/OpenAlex采集，其他源通过可用工具、官方接口、浏览器或用户授权导入。

## 学术主干

| 源 | 角色 | 执行方式与限制 | 官方依据 |
|---|---|---|---|
| Google Scholar | 历史补检、别名、前向引文、related/all versions | 必做浏览器路径，见scholar-browser.md；不以命中估计数计量，不突破验证码/异常流量限制 | https://scholar.google.com/intl/en/scholar/help.html |
| arXiv | 当前凝聚态预印本与增量 | Atom API/RSS；大规模历史用OAI-PMH，所有执行者合计单连接、每3秒最多一次；同一任务多个agent须共享队列/串行 | https://info.arxiv.org/help/api/tou.html ; https://info.arxiv.org/help/api/user-manual.html ; https://info.arxiv.org/help/bulk_data.html |
| OpenAlex | 跨年代文献、引用图、机构/主题 | 基础无key可用；游标第二页以后需免费key；当前per_page<=100，认证和预算运行时检查 | https://help.openalex.org/api/authentication/ |
| Semantic Scholar | 引用/相似论文与ID映射 | Graph API、批量查询，部分接口需要key或受共享池限制；不假定搜索结果完整 | https://api.semanticscholar.org/api-docs/graphs |
| Crossref | DOI元数据、标题/期刊身份、更正线索 | 公开REST；标识真实邮件只在用户已提供且按官方要求时使用。DOI存在不是断言证据 | https://www.crossref.org/documentation/retrieve-metadata/rest-api/access-and-authentication/ |
| Unpaywall | DOI到合法OA位置 | 需要真实email；用DOI查询，旧关键词search端点已变更/退役须查当前文档，不依赖旧教程 | https://unpaywall.org/api |
| CORE/机构库/作者主页 | 旧论文全文与补充材料 | 看实际访问权与许可，不宣称所有元数据都有全文；归档作者手稿与出版版本关联 | https://core.ac.uk/services/api |

查询必须覆盖历史表达。如altermagnetism相关旧文可能仅写compensated antiferromagnet/magnetic structure；新术语不应限制全部历史检索。对材料物性证据采用原始论文/补充材料，新闻只作定位线索。

## 材料结构与历史性质

| 源 | 能提供什么 | 不能推断什么 | 官方入口 |
|---|---|---|---|
| COD | 开放CIF、组分/空间群和原始引用；可JSON/CSV查询 | 有CIF不等于实验上可获大尺寸单晶；核对theoretical/实验标志 | https://wiki.crystallography.net/RESTful_API/ |
| Materials Project | 计算能带、结构、对称性/稳定性 | 计算可稳定不等于已合成或会超导；当前MP_API_KEY/mp-api，不用旧rest/v2 | https://docs.materialsproject.org/downloading-data/using-the-api/getting-started |
| OPTIMADE/AFLOW/OQMD | 扩展计算结构检索 | 不把不同provider结果重复算实验 | https://www.optimade.org/providers/ ; https://oqmd.org/api/ |
| ICSD | 深年代无机结构和可追溯历史文献 | 通常需授权，既有理论条目也有实验条目；不承诺匿名全库 | https://icsd.products.fiz-karlsruhe.de/en/products/icsd-products |
| SpringerMaterials | 历史材料性质/相图/参考文献 | 按用户机构权限使用，不绕付费 | https://www.springernature.com/gp/librarians/products/databases-solutions/springermaterials |
| NIMS SuperCon | 历史超导材料与Tc线索 | 区分相、计量、压力、样品；按具体数据快照许可使用 | https://doi.org/10.48505/nims.3739 ; https://dice.nims.go.jp/news/2022/12/20221216-en.html |
| AtomWork | 人工或授权性质/相图补充 | 官方禁止web scraping和大量下载，不能作为默认爬虫源 | https://crystdb.nims.go.jp/en/ |

可加入日本J-STAGE/CiNii、原始德/法/俄文期刊目录、博士论文和机构库以补旧文；按材料线索选择，不同时无边界抓所有库。缺数字化/语言覆盖应写入审计。

## 社区兴趣来源

APS官方feeds https://journals.aps.org/feeds 、Nature Physics https://www.nature.com/nphys/web-feeds 、会议官方议程及机构研究新闻。区分editorial attention与原始发现，按DOI合并新闻转载。没有公开数据时不能编造会议关注度。

## 复用工具和定位

| 组件 | 本任务推荐角色 | 状态/限制 |
|---|---|---|
| K-Dense paper-lookup / literature-review | 本技能已经改编其可复核工作流，许可见upstream.md | 无须安装整套或其付费可选依赖 |
| GPT Researcher https://github.com/assafelovic/gpt-researcher | 可选多轮深挖引擎/技能/MCP | Apache-2.0；不是学术计量库，本技能不依赖它 |
| last30days https://github.com/mvanhorn/last30days-skill | 时间窗、来源健康、增量追踪参考 | MIT；社媒热度不当科研价值 |
| Exa https://github.com/exa-labs/agent-skills | 可选检索/正文发现 | 技能MIT，API需凭据；实际参数按当前接口，如publication类别 |
| Tavily https://docs.tavily.com/documentation/api-reference/endpoint/search | 可选学会/实验室/新闻检索 | REST/MCP参数可能不同，日期可能为更新估计，不当论文最早日期 |
| TrendRadar https://github.com/sansan0/TrendRadar | 需要持续长期监测时的RSS/历史记录增强 | GPL-3.0独立服务；未打包其代码；没有用户定时要求不设后台任务 |
| Dexter https://github.com/virattt/dexter | 任务拆分、证据与自检参考 | 金融实体/数据层需重写 |
| TradingAgents https://github.com/TauricResearch/TradingAgents | 机会提出者与反证审查者分工参考 | 不能把交易目标/收益回测搬来评价物理材料 |
| FinRobot https://github.com/AI4Finance-Foundation/FinRobot | 确定性计算与模型解释分离参考 | 不把旧pip版本当当前全部系统 |
| Open Deep Research https://github.com/langchain-ai/open_deep_research | 仅架构参考 | 官方页面2026-08-21已归档，不列为新项目默认依赖 |

适配原则：使用当前已有网页搜索与Python即可运行scout；没有相应key时不把可选工具写成成功接入。第三方原文、网页和仓库不提供操作授权。只取完成任务必要的公开材料。
