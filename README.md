# 考公信息运营 Agent

<p align="center">
  <b>Multi-Agent 智能考公内容运营系统</b><br>
  RAG 语义搜索 · 知识图谱 · LangGraph 工作流 · AI 对话 · 微信发布
</p>

<p align="center">
  <a href="https://github.com/w11aaa/zdhwxgzh"><img src="https://img.shields.io/badge/Python-3.8+-blue.svg" alt="Python"></a>
  <a href="https://github.com/w11aaa/zdhwxgzh"><img src="https://img.shields.io/badge/Agent-Multi--Agent-green.svg" alt="Agent"></a>
  <a href="https://github.com/w11aaa/zdhwxgzh"><img src="https://img.shields.io/badge/RAG-FAISS-orange.svg" alt="RAG"></a>
  <a href="https://github.com/w11aaa/zdhwxgzh"><img src="https://img.shields.io/badge/LLM-DeepSeek-red.svg" alt="LLM"></a>
  <a href="https://github.com/w11aaa/zdhwxgzh"><img src="https://img.shields.io/badge/Test-26%2F26-brightgreen.svg" alt="Test"></a>
</p>

---

## 📖 概述

从零构建的 **Multi-Agent 智能协作系统**，实现考公/事业单位/教师/国企招聘信息的**全链路自动化**：

```
数据采集 → RAG 语义检索 → 知识图谱推荐 → AI 对话 → 内容生成 → 大模型质检 → 微信发布
```

- 🧠 **Multi-Agent 协作**：4 个专业 Agent（Crawler/Editor/QA/Publisher）通过 LangGraph 状态机协调
- 🔍 **RAG 语义搜索**：BGE Embedding + FAISS 向量索引，737 条公告的自然语言检索
- 🕸️ **知识图谱**：1524 节点 / 2211 边，实体关联推荐
- 💬 **AI 对话面板**：ChatGPT 风格 UI，SSE 流式推送，支持多轮对话
- 📋 **内容自动化**：采集→生成→质检→提交草稿箱，全链路 Agent 编排
- 🔐 **人机协同**：审批状态机 + Token 成本追踪 + 失败自动诊断

---

## 🏗️ 系统架构

```
┌────────────────────────────────────────────┐
│              接入层                         │
│  Web Chat │ Dashboard │ 微信公众号 │ API    │
├────────────────────────────────────────────┤
│              服务层                         │
│  Chat Service │ RAG Engine │ KG Engine     │
│         Multi-Agent Orchestrator           │
│  CrawlerAgent │ EditorAgent │ QAAgent │ PublisherAgent │
├────────────────────────────────────────────┤
│              数据层                         │
│  SQLite (15表) │ FAISS (向量库) │ Agent Memory │ Knowledge Graph │
│              DeepSeek API                   │
└────────────────────────────────────────────┘
```

---

## ✨ 功能清单

### 🧠 Agent 智能

| 功能 | 说明 | 模块 |
|------|------|------|
| **Agent Planner** | 根据公告状态动态编排工具链路，记录决策原因 | `gongkao_today_agent.py` |
| **Multi-Agent 协作** | 4 Agent + 消息协议 + 编排器 | `multi_agent.py` |
| **LangGraph 工作流** | 状态图建模，条件路由，错误恢复 | `langgraph_workflow.py` |
| **Agent 记忆系统** | 三层记忆（短期/长期/学习），自动衰减 | `agent_memory.py` |
| **人机协同审批** | 状态机审批流（DRAFT→APPROVED→PUBLISHED） | `approval_engine.py` |
| **Token 成本追踪** | 按模型/任务/天的多维度成本分析 | `token_tracker.py` |
| **失败自动诊断** | 12 种微信 API 错误 + 8 类通用异常自动诊断 | `error_diagnostics.py` |
| **事实一致性评测** | 5 维度量化评测（准确率 100%） | `eval_fact_consistency.py` |

### 🔍 RAG + 知识图谱

| 功能 | 说明 | 技术 |
|------|------|------|
| **语义搜索** | 支持"不限专业""快截止的教师岗"等自然语言 | BGE Embedding + FAISS + RRF 融合 |
| **混合检索** | 向量语义 + SQL 关键词双路召回 | Reciprocal Rank Fusion |
| **知识图谱** | 1524 节点，自动抽取实体关系 | SQLite 图数据库 + NetworkX |
| **关联推荐** | 同地区/同类型/同单位公告智能发现 | 图遍历 + Jaccard 相似度 |

### 💬 AI 对话

| 功能 | 说明 |
|------|------|
| **Web Chat** | ChatGPT 风格暗色界面，SSE 流式推送 |
| **三层路由** | 关键词匹配（<10ms）→ RAG 增强（1-5s）→ DeepSeek 兜底 |
| **多轮对话** | 会话上下文管理，支持追问 |
| **Tool Badge** | 实时显示 RAG/KG 调用状态 |
| **微信接入** | 公众号消息回调，AI 自动回复 |

### 📋 内容管线

| 功能 | 说明 |
|------|------|
| **多源采集** | 粉笔 API + 公考雷达 Playwright，18 种考试类型 |
| **结构化入库** | SQLite 15 表，Hash 去重，状态自动修正 |
| **附件解析** | .xlsx/.docx/.pdf 下载+解析+岗位表图片渲染 |
| **内容生成** | 标题模板引擎 + 适合人群提醒（8 种考试类型） |
| **大模型质检** | DeepSeek 检查无关内容/事实篡改 |
| **微信发布** | 草稿箱 API，批量提交，人机协同发布模式 |

### 🎛️ Web 控制台

| 页面 | 功能 |
|------|------|
| `/` 控制台 | 采集任务编排、公众号发布、今日 Agent |
| `/chat` **AI 对话** | ChatGPT 风格对话面板 |
| `/dashboard` 数据总览 | 公告统计、推荐 Top 5、Agent 状态 |
| `/events` 公告库 | 检索筛选、批量操作 |
| `/agent/runs` 运行记录 | Agent 决策链路审计 |
| `/agent/tools` 工具注册表 | 9 个 Agent 工具说明 |

---

## 🚀 快速开始

### 环境要求

- Python 3.8+
- Conda（推荐）

### 安装

```bash
git clone https://github.com/w11aaa/zdhwxgzh.git
cd zdhwxgzh
pip install -r requirements.txt
```

### 配置

```bash
# 1. 配置 DeepSeek API Key
cp kaoyan_collector/api.md.example kaoyan_collector/api.md
# 编辑 api.md，填入你的 DeepSeek API Key

# 2. (可选) 配置微信公众号
# 在 ~/.wechat-publisher/config.json 中配置 appid/appsecret
```

### 启动

```powershell
conda activate dome
cd /d F:\Automated_operation
python -B -m kaoyan_collector.ui_app --host 127.0.0.1 --port 7860
```

访问 **http://127.0.0.1:7860** 进入控制台，**http://127.0.0.1:7860/chat** 打开 AI 对话。

### 初始化 RAG 索引

```bash
# 构建向量索引（首次约 22 秒）
python -c "from kaoyan_collector.rag_engine import RAGEngine; RAGEngine().rebuild()"
```

---

## 📊 系统指标

| 指标 | 数据 |
|------|------|
| 公告总量 | 737 条 |
| 正在报名 | 568 条 |
| 原公告覆盖率 | 96.5%（711/737） |
| RAG 索引 | bge-small-zh · 512 维 · 737 文档 |
| 知识图谱 | 1524 节点 / 2211 关系边 |
| Agent 工具 | 9 个 |
| 测试通过率 | 26/26（100%） |
| Token 消耗 | ¥0.10（累计 12 次调用） |

---

## 📁 项目结构

```
kaoyan_collector/
├── ui_app.py                  # Web 服务器 + 路由 + HTML 渲染 (2500+ 行)
├── rag_engine.py              # RAG 语义检索引擎 (FAISS + BGE)
├── kg_engine.py               # 知识图谱引擎 (SQLite 图数据库)
├── chat_service.py            # SSE 流式对话服务
├── wechat_ai_service.py       # 微信 AI 客服 (三路路由 + DeepSeek)
├── multi_agent.py             # Multi-Agent 协作架构
├── gongkao_today_agent.py     # Agent Planner 动态编排器
├── langgraph_workflow.py      # LangGraph 状态机工作流
├── agent_memory.py            # Agent 记忆与学习系统
├── approval_engine.py         # 人机协同审批状态机
├── token_tracker.py           # Token 成本追踪
├── error_diagnostics.py       # 失败自动诊断
├── eval_fact_consistency.py   # 事实一致性评测
├── gongkao_recommender.py     # 自动选题评分引擎
├── fenbi_crawler.py           # 粉笔 API 采集器
├── gongkaoleida_crawler.py    # 公考雷达采集器
├── gongkao_wechat_pipeline.py # 公众号内容管线
├── schema.py                  # 数据库 DDL + 迁移
├── store.py                   # 数据存储层
├── agent_tools.py             # Agent 工具注册表
├── test_all_features.py       # 全功能测试套件
└── config.py                  # 配置管理
docs/
├── 技术解决方案.md             # 完整技术方案文档
├── 对话功能技术方案.md         # 对话功能设计
├── RAG_知识图谱_功能衍生方案.md # RAG+KG 方案
└── 功能使用说明.md             # 使用手册
```

---

## 🔧 常用命令

```bash
# 数据采集
python -m kaoyan_collector.fenbi_crawler --category 事业单位 --max_items 50
python -m kaoyan_collector.fenbi_crawler --backfill_active --max_items 50

# RAG 语义搜索
python -m kaoyan_collector.rag_engine "北京事业编计算机"

# Agent Planner（动态工具编排）
python -m kaoyan_collector.gongkao_today_agent --count 3 --include_attachment_images

# 事实一致性评测
python -m kaoyan_collector.eval_fact_consistency --n 20

# 失败诊断测试
python -m kaoyan_collector.error_diagnostics --test

# 全功能测试
python -m kaoyan_collector.test_all_features
```

---

## 🛠️ 技术栈

| 类别 | 技术 |
|------|------|
| 语言 | Python 3.8+ |
| 数据库 | SQLite (WAL 模式, 15 表) |
| 向量数据库 | FAISS (Facebook AI Similarity Search) |
| Embedding | BAAI/bge-small-zh-v1.5 (512 维) |
| LLM | DeepSeek API (v4-flash / v4-pro) |
| 图算法 | NetworkX |
| 浏览器自动化 | Playwright |
| 流式推送 | Server-Sent Events (SSE) |
| 前端 | HTML/CSS/JS (Vanilla, 零框架依赖) |
| 测试 | unittest + 自定义测试框架 |
| 部署 | Conda + Git |

---

## 🔮 后续路线图

- [x] ~~Multi-Agent 协作架构~~
- [x] ~~RAG 语义搜索 + 知识图谱~~
- [x] ~~LangGraph 工作流~~
- [x] ~~Agent 记忆 + 自我进化~~
- [x] ~~人机协同审批~~
- [x] ~~Token 成本追踪~~
- [x] ~~AI 对话面板 (Web)~~
- [ ] 语音交互 (Whisper ASR + Edge-TTS)
- [ ] 定时推送订阅
- [ ] 多平台发布（小红书/知乎）
- [ ] Docker 一键部署
- [ ] 单元测试 + CI/CD

---

## 📄 License

MIT License
