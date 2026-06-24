# Arxiv Analyse

arXiv 数学论文采集管线。一个入口，四个命令。完全自包含。

## 快速开始

```bash
bash setup.sh && source .venv/bin/activate

python main.py init        # 首次：配置 Kaggle → 下载元数据 → 建库
python main.py download    # 下载 PDF（10 并发）
python main.py update      # 日常增量同步
python main.py stats       # 查看统计
```

## 管线阶段

| 阶段 | 命令 | 说明 | 状态 |
|------|------|------|------|
| 1. 初始化 | `init` | Kaggle 元数据 → SQLite + meta.json + 年份文件夹 | ✅ |
| 2. PDF 下载 | `download` | 10 并发从 arxiv.org 下载 | ✅ |
| 3. 增量同步 | `update` | OAI-PMH 补齐新论文 | ✅ |
| 4. 转写 | - | PDF → Markdown | 🔲 |
| 5. 拆解 | - | Markdown → 结构化 MD | 🔲 |
| 6. 分析 | - | 结构化 MD → 分析报告 | 🔲 |

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

10 并发线程从 arxiv.org 下载，每个线程随机延迟 2~3 秒，5 次重试。

```bash
python main.py download                # 下载全部
python main.py download --year 2024    # 只下载 2024 年
python main.py download --workers 5    # 调整并发数
python main.py download --loop         # 循环重试直到全部成功
```

### update — 增量同步

自动查询数据库最新论文日期，通过 OAI-PMH 拉取之后的新论文。

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
- **download**：.tmp 原子写入，启动时自动清理残留和坏文件（`%PDF` 校验）
- **update**：从数据库最新日期继续

## 注意事项

- 全部数学论文约 60-70 万篇，PDF 总量约 250-400 GB
- 建议按年份下载：`--year 2024`
- arxiv.org 对并发态度宽松，不建议超过 20 线程
