# 考公信息运营 Agent 系统

**Agent 开发工程师** ｜ 独立开发 ｜ 2025.06 – 至今

**技术栈**

Python · SQLite · FAISS 向量数据库 · BGE Embedding · DeepSeek API · LangGraph · NetworkX · Playwright · SSE 流式推送 · 微信公众号 API · HTML/CSS/JS

**项目地址**：https://github.com/w11aaa/zdhwxgzh

---

**项目描述**

从零设计并实现了一套面向考公内容运营的 Multi-Agent 智能系统。系统从粉笔、公考雷达等多源自动采集公告（已入库 737 条），基于 RAG 语义搜索 + 知识图谱构建自然语言查询能力，通过 4 个专业 Agent 协作完成内容生成、质检、自动发布全流程，并提供 Web/微信双端的智能对话服务。

---

**工作内容**

**① Multi-Agent 协作架构**：设计 CrawlerAgent / EditorAgent / QAAgent / PublisherAgent 4 个专业 Agent，通过统一的 AgentMessage 协议通信。基于 LangGraph 将业务流程建模为有状态图（recommend → search_origin → generate → qa_check → publish），支持条件路由与错误恢复。每步记录了 reasoning（决策原因）到 agent_steps 审计表，实现 Agent 决策全链路可追溯。

**② RAG 语义搜索 + 知识图谱**：采用 BAAI/bge-small-zh-v1.5（512 维）对 737 条公告原文做向量化，构建 FAISS 索引。实现向量语义 + 关键词精确的混合检索，通过 RRF（Reciprocal Rank Fusion）融合排序，支持"不限专业""快截止的教师岗"等口语化查询。在 SQLite 上构建轻量图数据库，自动抽取 1524 节点、2211 条实体关系，支持同地区/同类型公告的关联推荐。

**③ Agent 记忆与学习**：实现三层记忆架构（短期会话上下文 / 长期跨会话经验 / 学习模式自动提取），支持记忆衰减、关键词检索。预置 6 条种子最佳实践，Agent 可从失败步骤中自动学习经验教训。

**④ 大模型内容生成 + 质量保障**：基于公告结构化字段生成公众号文章，内置标题模板引擎和适合人群提醒模块（覆盖 8 种考试类型）。接入 DeepSeek 进行草稿质检，自研事实一致性评测脚本，从招聘人数、地区、截止日期、原文链接、无关内容 5 个维度量化评估（评测通过率 100%）。质检不通过自动阻断发布。

**⑤ 人机协同 + 诊断恢复**：构建审批状态机（DRAFT→PENDING_APPROVAL→APPROVED→PUBLISHED），支持驳回重新生成、超时自动处理。实现 Token 成本追踪（按模型/任务/天分组）和失败诊断引擎（覆盖 12 种微信 API 错误码 + 8 类通用异常模式）。

**⑥ 智能对话与数据采集**：实现 ChatGPT 风格 Web 对话面板，SSE 流式推送实时展示 Agent 思考过程。微信端接入公众号消息回调，支持自然语言查公告、AI 对话。采集端支持 18 种考试类型、粉笔 API / 公考雷达双源，附件的 .xlsx/.docx 智能解析。

---

**项目成果**

- 系统入库 **737 条公告**，原文覆盖率 **96.5%**，覆盖 18+ 考试类型
- RAG 向量索引：bge-small-zh-v1.5 · 512 维 · 737 文档，检索 <50ms
- 知识图谱：1524 节点 / 2211 关系边
- Multi-Agent：4 专业 Agent + 编排器，9 个注册工具
- 事实一致性评测 5 维度 **100% 通过**，26/26 测试套件全通过
- 12 种微信 API 错误码 + 8 类通用异常的自动诊断覆盖
- 代码量 8000+ 行，15+ 模块，已开源发布
