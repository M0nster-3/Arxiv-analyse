# Arxiv Analyse

arXiv 数学相关论文采集管线：Kaggle 元数据建库、本地 PDF 镜像、OAI-PMH 增量同步。一个入口，四个命令；转写、拆解和分析阶段尚未实现。

## 快速开始

```bash
bash setup.sh && source .venv/bin/activate

python main.py init        # 首次：配置 Kaggle → 下载元数据 → 建库
python main.py download    # 下载 PDF（当前默认 100 并发，可用 --workers 调整）
python main.py update      # 日常增量同步
python main.py stats       # 查看统计
```

## 管线阶段

| 阶段 | 命令 | 说明 | 状态 |
|------|------|------|------|
| 1. 初始化 | `init` | Kaggle 元数据 → SQLite + meta.json + 年份文件夹 | ✅ |
| 2. PDF 下载 | `download` | 从 arxiv.org 下载 PDF，状态写回 SQLite | ✅ |
| 3. 增量同步 | `update` | 从最新 published 日期走 OAI-PMH 补齐新论文 | ✅ |
| 4. 转写 | - | PDF → Markdown | 🔲 |
| 5. 拆解 | - | Markdown → 结构化 MD | 🔲 |
| 6. 分析 | - | 结构化 MD → 分析报告 | 🔲 |

## 数据集概览

| 项目 | 当前本地状态 |
|------|------|
| 元数据总数 | **762,346** 篇，均有 `meta.json` |
| 数据库下载状态 | `downloaded` **755,285** / `failed` **7,061** / `pending` **0** |
| 磁盘 PDF 文件 | **755,285** 个 |
| PDF 字节总量 | DB 记录 **612,803,687,189 bytes**；磁盘实际 **612,768,003,356 bytes**（约 **570.7 GiB**） |
| 本地占用 | `data/` **585G**；`data/papers/` **578G**；Kaggle cache **5.0G**；SQLite **1.1G** |
| 时间范围 | **1989-04-15** ~ **2026-06-18**（按 `published`） |
| 过滤口径 | 记录的任一 `category` 以 `math.` 开头；`primary_category` 不一定是数学分类 |
| 主分类 | math.AP / math.CO / math.PR / math.AG / cs.IT / math.OC / math.NT / math.NA / math-ph 等 |
| 数据来源 | [Kaggle arXiv 数据集](https://www.kaggle.com/datasets/Cornell-University/arxiv)（Cornell University 官方） |
| Kaggle 导入 | `2026-06-24T21:50:16Z` |
| 增量同步 | OAI-PMH 代码已实现；当前本地 `sync_state` 尚无 `oai_last_harvest` 记录 |

论文总数按年分布（Top 10）：

```
2025    55,607 篇
2024    51,992 篇
2023    47,930 篇
2020    46,104 篇
2022    45,417 篇
2021    45,140 篇
2019    43,044 篇
2018    40,204 篇
2017    38,656 篇
2016    36,512 篇
```

> **注意**：当前有 **7,061** 条 `failed` 记录，年份从 **1992** 分布到 **2026**，并不只集中在 2000 年前。重试前建议按 `error` 和年份抽查，不要默认全部都是早期论文 404。
>
> 当前盘点还发现 **69** 个 `downloaded` 记录的磁盘文件大小与 DB `pdf_size` 不一致，其中 **64** 个实际 `paper.pdf` 是 0 字节。后续做 PDF 转写前应先校验或修复这些文件。
>
> 当前数据集截至 **2026-06-18**。如需获取更新的论文，运行 `python main.py update`。

## 自包含设计

整个项目可以复制到任意位置运行，不依赖也不污染 home 目录：

```
Arxiv-analyse/
├── main.py                 # 唯一入口
├── lib/                    # 业务逻辑
│   ├── database.py
│   ├── kaggle_loader.py
│   ├── oai_client.py
│   └── pdf_downloader.py
├── .venv/                  # Python 虚拟环境
├── data/
│   ├── .kaggle/            # Kaggle 凭证（不是 ~/.kaggle）
│   │   └── kaggle.json
│   ├── cache/              # Kaggle 数据集缓存（不是 ~/.cache/kagglehub）
│   ├── arxiv_analyse.db    # SQLite 数据库
│   └── papers/             # 论文文件
│       ├── 2025/
│       │   └── 2501.00001/
│       │       ├── meta.json
│       │       └── paper.pdf
│       └── ...
├── requirements.txt
├── setup.sh
└── .gitignore
```

Kaggle 凭证和数据集缓存全部在 `data/` 下，通过 `KAGGLE_CONFIG_DIR` 和 `KAGGLEHUB_CACHE` 环境变量重定向。删除项目目录即完全清理，不留残留。

### meta.json 格式

```json
{
  "arxiv_id": "2501.00001",
  "title": "On the Uniform Continuity of Functions on Compact Metric Spaces",
  "authors": ["Alice Wang", "Bob Chen"],
  "summary": "We study the relationship between...",
  "primary_category": "math.GN",
  "categories": ["math.GN", "math.FA"],
  "published": "2025-01-01T00:00:00Z",
  "updated": "2025-01-15T00:00:00Z",
  "arxiv_url": "https://arxiv.org/abs/2501.00001",
  "pdf_url": "https://arxiv.org/pdf/2501.00001",
  "pdf_path": "2025/2501.00001/paper.pdf"
}
```

## 命令详解

### init — 首次初始化

交互式输入 Kaggle 用户名和 API Key（保存在项目内 `data/.kaggle/`），然后自动下载数据集（缓存在 `data/cache/`）、过滤数学论文、写入 SQLite、生成年份文件夹和 meta.json。

```bash
python main.py init                    # 标准流程
python main.py init --force-download   # 强制重下数据集
python main.py init --reconfigure      # 重新配置凭证
python main.py init --category cs      # 改为计算机科学
```

### download — 下载 PDF

当前代码默认 `DOWNLOAD_WORKERS=100`、`DOWNLOAD_BATCH=10000`。每个请求随机延迟 2~3 秒，5 次重试；长期运行或遇到限流时建议显式降低并发。

```bash
python main.py download                # 下载全部
python main.py download --year 2024    # 只下载 2024 年
python main.py download --workers 20   # 调整并发数
python main.py download --loop         # 循环重试直到全部成功
```

### update — 增量同步

自动查询数据库最新论文日期，通过 OAI-PMH 拉取之后的新论文。

当前本地数据库的 `source` 全部为 `kaggle`，且 `sync_state` 中没有 `oai_last_harvest`，说明当前快照尚未记录过 OAI 增量同步。

```bash
python main.py update
```

### stats / retry

```bash
python main.py stats                   # 查看统计
python main.py retry                   # 重置全部 failed → pending
python main.py retry --year 2024       # 只重置某年
```

## 断点续跑

所有操作支持 Ctrl+C 安全退出后继续：

- **init**：基于 arxiv_id 去重，重复运行安全
- **download**：使用 `.tmp` 原子写入，下载单篇时校验 `%PDF` 和最小文件大小；当前命令入口没有自动调用 `cleanup_incomplete()`
- **update**：从数据库最新日期继续

## 注意事项

- 全量数学相关论文 **762,346** 篇，PDF 磁盘实际总量约 **570.7 GiB**，建议预留 600 GB 以上磁盘空间
- SQLite 数据库约 **1.1G**，本地 `data/` 占用约 **585G**
- 建议按年份下载：`--year 2024`
- 当前实现默认 100 线程；如果不是短时间补齐，建议用 `--workers 10` 或 `--workers 20`
- 当前 `failed` 有 **7,061** 条，分布在 1992~2026；是否值得重试需要看 `error` 字段
- 转写 PDF 前建议先处理 69 个 DB/磁盘大小不一致的 `downloaded` 记录
