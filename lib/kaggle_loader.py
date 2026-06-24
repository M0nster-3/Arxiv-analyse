"""
Kaggle 数据集下载与解析。

所有 Kaggle 相关文件（凭证、缓存）都保存在项目 data/ 下，
不污染用户 home 目录（~/.kaggle / ~/.cache）。
"""

import json
import logging
import os
import sys
from pathlib import Path
from email.utils import parsedate_to_datetime

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
KAGGLE_CONFIG_DIR = PROJECT_ROOT / "data" / ".kaggle"    # 凭证
KAGGLE_CACHE_DIR = PROJECT_ROOT / "data" / "cache"       # 数据集缓存

KAGGLE_HANDLE = "Cornell-University/arxiv"
KAGGLE_FILENAME = "arxiv-metadata-oai-snapshot.json"


def ensure_kaggle_env():
    """设置环境变量，让 kagglehub 把凭证和缓存都放到项目内。

    必须在 import kagglehub 之前调用。
    """
    KAGGLE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    KAGGLE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # kagglehub / kaggle CLI 共用的凭证目录
    os.environ["KAGGLE_CONFIG_DIR"] = str(KAGGLE_CONFIG_DIR)
    # kagglehub 专用缓存目录（默认 ~/.cache/kagglehub）
    os.environ["KAGGLEHUB_CACHE"] = str(KAGGLE_CACHE_DIR)


def has_credentials() -> bool:
    """检查项目内是否已有 Kaggle 凭证。"""
    cred = KAGGLE_CONFIG_DIR / "kaggle.json"
    if cred.exists():
        try:
            d = json.loads(cred.read_text())
            return bool(d.get("username")) and bool(d.get("key"))
        except Exception:
            pass
    return False


def setup_credentials(username: str, api_key: str):
    """写入 Kaggle 凭证到项目内 data/.kaggle/kaggle.json"""
    KAGGLE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    cred_path = KAGGLE_CONFIG_DIR / "kaggle.json"
    cred_path.write_text(
        json.dumps({"username": username, "key": api_key}),
        encoding="utf-8",
    )
    cred_path.chmod(0o600)
    logger.info(f"凭证已保存: {cred_path}")


def download_dataset(force: bool = False) -> Path:
    """通过 kagglehub 下载数据集，返回 JSON 文件路径。

    数据集缓存在 data/cache/ 下。
    """
    ensure_kaggle_env()

    try:
        import kagglehub
    except ImportError:
        logger.error("kagglehub 未安装，请运行: pip install kagglehub")
        sys.exit(1)

    logger.info(f"下载数据集: {KAGGLE_HANDLE}")
    logger.info(f"缓存目录:   {KAGGLE_CACHE_DIR}")
    logger.info("首次约 4-5 GB，后续使用本地缓存...")

    try:
        dataset_dir = kagglehub.dataset_download(KAGGLE_HANDLE, force_download=force)
    except Exception as e:
        logger.error(f"下载失败: {e}")
        logger.info("请检查 Kaggle 凭证是否正确，或重新运行 init --reconfigure")
        sys.exit(1)

    dataset_path = Path(dataset_dir)
    json_path = dataset_path / KAGGLE_FILENAME
    if not json_path.exists():
        candidates = list(dataset_path.glob("*.json"))
        if candidates:
            json_path = candidates[0]
        else:
            logger.error(f"数据集中未找到 JSON: {dataset_path}")
            sys.exit(1)

    logger.info(f"数据集路径: {json_path}")
    return json_path


# ── 记录解析 ─────────────────────────────────────────

def parse_record(raw: dict, category_prefix: str) -> dict | None:
    """Kaggle JSON → 标准 meta dict。不匹配分类返回 None。"""
    categories_str = raw.get("categories", "")
    categories = categories_str.split()
    primary = categories[0] if categories else ""

    if not any(c.startswith(category_prefix) for c in categories):
        return None

    arxiv_id = raw.get("id", "")
    if not arxiv_id:
        return None

    title = raw.get("title", "").strip().replace("\n", " ")
    abstract = raw.get("abstract", "").strip()

    authors_parsed = raw.get("authors_parsed")
    if authors_parsed:
        authors = []
        for parts in authors_parsed:
            last = parts[0] if len(parts) > 0 else ""
            first = parts[1] if len(parts) > 1 else ""
            name = f"{first} {last}".strip() if first else last
            if name:
                authors.append(name)
    else:
        authors_str = raw.get("authors", "")
        authors = [a.strip() for a in authors_str.split(",") if a.strip()]

    versions = raw.get("versions", [])
    published = None
    if versions:
        published = _parse_date(versions[0].get("created", ""))

    update_date = raw.get("update_date", "")
    updated = f"{update_date}T00:00:00Z" if update_date else None

    return {
        "arxiv_id": arxiv_id,
        "title": title,
        "authors": authors,
        "summary": abstract,
        "primary_category": primary,
        "categories": categories,
        "published": published,
        "updated": updated,
        "arxiv_url": f"https://arxiv.org/abs/{arxiv_id}",
        "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
        "source": "kaggle",
    }


def _parse_date(date_str: str) -> str | None:
    if not date_str:
        return None
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        for p in date_str.split():
            if len(p) == 4 and p.isdigit():
                return f"{p}-01-01T00:00:00Z"
    return None


def iter_math_papers(file_path: Path, category: str = "math"):
    """生成器：逐行解析 JSONL，yield 匹配分类的 meta dict。"""
    prefix = f"{category}." if "." not in category else category

    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            meta = parse_record(raw, prefix)
            if meta is not None:
                yield meta
