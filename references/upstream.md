# 上游来源和许可

本技能实际改编了 K-Dense Scientific Agent Skills 的学术检索和文献审查工作流；不是未经阅读的脚本打包，也不要求整套上游插件已安装。

读取日期：2026-11-07。读取到的文档版本：paper-lookup 2.4、literature-review 1.11（各自metadata last-reviewed 2026-09-30）。未能从官方服务核验上游commit SHA，因此只记录文档版本与获取日期，不宣称固定提交复现。

源文件：
- https://raw.githubusercontent.com/K-Dense-AI/scientific-agent-skills/main/skills/paper-lookup/SKILL.md
- https://raw.githubusercontent.com/K-Dense-AI/scientific-agent-skills/main/skills/literature-review/SKILL.md
- https://raw.githubusercontent.com/K-Dense-AI/scientific-agent-skills/main/LICENSE.md

实际沿用并改编：数据库按任务路由；明确检索约定；有界请求和来源日志；检查HTTP成功体内的错误；记录级去重与研究版本关联；原文断言核验；覆盖不足不等于不存在。替换医学案例为凝聚态对象，加入历史实验、单晶、材料身份与稀缺性；新增持久SQLite、Scholar浏览器路径、按需合成、双维评分与中文HTML。scripts/内代码为本技能编写，未执行或复制未取得完整源码的上游脚本。未复用其商业服务推广或付费图像生成依赖。

金融/通用项目作为架构参考而非安装依赖：GPT Researcher、last30days、TrendRadar、TradingAgents。对应许可证遵循各项目；本技能未复制这些项目的代码，尤其未混入GPL组件。它们的可选适配见sources.md。

以下保留实际改编文档的MIT许可：

MIT License

Copyright (c) 2025 K-Dense Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
