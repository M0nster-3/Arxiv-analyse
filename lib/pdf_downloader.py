"""
PDF 下载器。

- 仅走 arxiv.org
- 10 并发线程，每次请求随机等待 2~3 秒
- 5 次重试，指数退避
- 断点续传：.tmp 原子写入，清理残留
"""

import logging
import random
import time
import threading
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "ArxivAnalyse/2.0 (academic-research; mailto:user@example.com)"}
CONNECT_TIMEOUT = 15
READ_TIMEOUT = 180
RETRIES = 5
RETRY_BACKOFF_BASE = 10
MIN_PDF_SIZE = 1_000
PDF_MAGIC = b"%PDF"

DELAY_MIN = 2.0
DELAY_MAX = 3.0

_lock = threading.Lock()
_stats = {"done": 0, "failed": 0}


def reset_stats():
    with _lock:
        _stats["done"] = 0
        _stats["failed"] = 0


def cleanup_incomplete(papers_dir: Path):
    """清理所有残留 .tmp 文件和过小的 PDF。"""
    cleaned = 0
    for tmp in papers_dir.rglob("*.tmp"):
        tmp.unlink(missing_ok=True)
        cleaned += 1
    for pdf in papers_dir.rglob("paper.pdf"):
        if pdf.stat().st_size < MIN_PDF_SIZE:
            pdf.unlink(missing_ok=True)
            cleaned += 1
    if cleaned:
        logger.info(f"清理 {cleaned} 个残留文件")


def _validate_pdf(path: Path) -> bool:
    if not path.exists():
        return False
    size = path.stat().st_size
    if size < MIN_PDF_SIZE:
        return False
    with open(path, "rb") as f:
        header = f.read(4)
    return header == PDF_MAGIC


def download_one(
    arxiv_id: str,
    year: int | None,
    papers_dir: Path,
    pdf_url: str | None = None,
) -> tuple[str, int]:
    """下载单篇 PDF。

    Returns: (相对路径, 文件字节数)
    Raises: RuntimeError
    """
    safe = arxiv_id.replace("/", "_")
    year_str = str(year) if year else "unknown"
    paper_dir = papers_dir / year_str / safe
    pdf_path = paper_dir / "paper.pdf"
    tmp_path = paper_dir / "paper.pdf.tmp"

    if _validate_pdf(pdf_path):
        size = pdf_path.stat().st_size
        return str(pdf_path.relative_to(papers_dir)), size

    paper_dir.mkdir(parents=True, exist_ok=True)
    tmp_path.unlink(missing_ok=True)

    url = pdf_url or f"https://arxiv.org/pdf/{arxiv_id}"
    last_err = None

    for attempt in range(1, RETRIES + 1):
        time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

        try:
            resp = requests.get(
                url, headers=HEADERS,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                stream=True,
            )

            if resp.status_code == 429:
                wait = RETRY_BACKOFF_BASE * attempt * 2
                logger.warning(f"  [{arxiv_id}] 429 限流，等待 {wait}s...")
                time.sleep(wait)
                continue

            resp.raise_for_status()

            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=16384):
                    f.write(chunk)

            if not _validate_pdf(tmp_path):
                size = tmp_path.stat().st_size if tmp_path.exists() else 0
                tmp_path.unlink(missing_ok=True)
                raise RuntimeError(
                    f"无效 PDF (大小={size}B, 可能是 HTML 错误页)"
                )

            size = tmp_path.stat().st_size
            tmp_path.rename(pdf_path)

            with _lock:
                _stats["done"] += 1

            return str(pdf_path.relative_to(papers_dir)), size

        except Exception as e:
            last_err = e
            tmp_path.unlink(missing_ok=True)
            if attempt < RETRIES:
                wait = RETRY_BACKOFF_BASE * attempt + random.uniform(0, 5)
                logger.debug(
                    f"  [{arxiv_id}] 重试 {attempt}/{RETRIES}: {e}, "
                    f"等待 {wait:.0f}s"
                )
                time.sleep(wait)

    _cleanup_empty(paper_dir)
    with _lock:
        _stats["failed"] += 1
    raise RuntimeError(f"下载失败 ({RETRIES}次): {last_err}")


def _cleanup_empty(d: Path):
    try:
        if d.exists() and not any(d.iterdir()):
            d.rmdir()
    except OSError:
        pass
