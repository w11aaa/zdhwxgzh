# kaoyan_collector

第一版目标：把 `MediaCrawler` 的多平台内容采集结果，统一落到一份 SQLite 里，服务“计算机考研信息”素材池。

## 目录

- `collect.py`：调用 `MediaCrawler` 抓取单个平台，原始数据输出到 `raw_data/`
- `ingest.py`：把各平台 `jsonl` 内容文件导入统一 SQLite
- `run_pipeline.py`：串起“采集 + 导入”
- `schema.py`：统一表结构
- `normalize.py`：各平台字段映射
- `topic_filter.py`：考研主题相关性打标
- `query_topics.py`：从库里筛候选选题
- `generate_draft.py`：调用 `xhs_ai_publisher` 的 LLMService 生成小红书草稿
- `publish_draft.py`：把草稿导入 `xhs_ai_publisher` 的内容池
- `auto_pipeline.py`：一条命令跑采集、入库、选题、生成、出图、发布

## 统一库位置

默认数据库：

`F:\Automated_operation\kaoyan_collector\data\kaoyan_content.db`

原始抓取文件：

`F:\Automated_operation\kaoyan_collector\raw_data\{platform}\jsonl\`

## 先决条件

1. `MediaCrawler` 依赖已安装
2. 可以正常运行 `python MediaCrawler/main.py --help`
3. 需要扫码登录的平台，首次执行时按提示完成登录

## 用法

公考雷达结构化采集：

```bash
python -m kaoyan_collector.gongkaoleida_crawler --category sydw --max_items 20 --page 1
```

如果不依赖公考雷达登录态，可开启“标题索引 + 原公告补全”模式：

```bash
python -m kaoyan_collector.gongkaoleida_crawler --category sydw --max_items 20 --page 1 --use_origin_search
```

分类别采集（当前已支持）：

- `all`：全部公告
- `gwy`：公务员
- `sydw`：事业单位
- `guoqi`：国企
- `teacher`：教师
- `medical`：医疗
- `xuandiao`：选调

也可以直接传自定义列表路径：

```bash
python -m kaoyan_collector.gongkaoleida_crawler --list_path /exam_search/1-78 --max_items 20
```

公考公告直出微信公众号草稿：

```bash
python -m kaoyan_collector.gongkao_wechat_pipeline --category 事业单位 --require_deadline --days_to_deadline 7
```

如果只生成 payload / markdown / html，不推送到公众号草稿箱：

```bash
python -m kaoyan_collector.gongkao_wechat_pipeline --category 事业单位 --require_deadline --skip_publish
```

单个平台采集：

```bash
python -m kaoyan_collector.collect --platform xhs --crawler_max_notes_count 20
```

单个平台导入最新内容文件：

```bash
python -m kaoyan_collector.ingest --platform xhs
```

一条命令跑“采集 + 导入”：

```bash
python -m kaoyan_collector.run_pipeline --platforms xhs,wb,zhihu --crawler_max_notes_count 20
```

开启评论抓取：

```bash
python -m kaoyan_collector.run_pipeline --platforms xhs,wb --get_comment
```

查询候选选题：

```bash
python -m kaoyan_collector.query_topics --platform xhs --limit 10
```

按关键词筛选并输出 JSON：

```bash
python -m kaoyan_collector.query_topics --keyword 408 --json
```

生成一篇小红书草稿：

```bash
python -m kaoyan_collector.generate_draft --platform xhs --keyword 408
```

## 当前范围

当前已经支持把以下平台的“内容主表”统一入库：

- `xhs`
- `dy`
- `bili`
- `wb`
- `zhihu`
- `tieba`
- `ks`

评论、创作者和定时调度可以在下一步接上。

## 主题打标

导入时会额外写入 4 个字段：

- `is_relevant`：是否判定为“计算机考研相关”
- `relevance_score`：规则分
- `relevance_label`：`relevant` / `review` / `noise`
- `relevance_reason`：命中的正负关键词

目前先用可解释规则过滤明显噪声，例如 `408` 误命中的房产帖。

## 草稿生成说明

`generate_draft.py` 会复用 `xhs_ai_publisher` 里的 `LLMService`，因此需要先有可用模型配置：

- `~/.xhs_system/settings.json`
- 或 `XHS_LLM_BASE_URL`、`XHS_LLM_MODEL`、`XHS_LLM_API_KEY` 这类环境变量

生成结果默认写到：

`F:\Automated_operation\kaoyan_collector\drafts\`

导入到发布助手内容池：

```bash
python -m kaoyan_collector.publish_draft --draft F:\Automated_operation\kaoyan_collector\drafts\draft_xxx.json
```

全自动执行（当前默认会自动点“暂存离开”保存为草稿）：

```bash
python -m kaoyan_collector.auto_pipeline --platforms xhs,wb,zhihu --topic_platform xhs --auto_publish
```

如果只想自动到“生成草稿 + 出图”为止：

```bash
python -m kaoyan_collector.auto_pipeline --skip_publish
```
