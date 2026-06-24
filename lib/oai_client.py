"""
OAI-PMH 客户端 — 直接实现，不依赖 sickle。

arXiv OAI-PMH 接口：
  - Base URL: https://oaipmh.arxiv.org/oai (2025-03 起)
  - 支持 set 过滤（math / physics / cs 等）
  - 支持 arXiv 专有 metadata 格式（含作者、分类、摘要）
  - resumptionToken 分页，每页约 1000 条
"""

import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://oaipmh.arxiv.org/oai"
METADATA_PREFIX = "arXiv"
REQUEST_INTERVAL = 5  # 两次请求之间的秒数（对服务器友好）
REQUEST_TIMEOUT = 120
MAX_RETRIES = 5
RETRY_BACKOFF = 30

# XML 命名空间
NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "arxiv": "http://arxiv.org/OAI/arXiv/",
}


def _get_text(el: ET.Element, tag: str, ns: dict = NS) -> str:
    """安全提取子元素文本。"""
    child = el.find(tag, ns)
    return child.text.strip() if child is not None and child.text else ""


def _parse_authors(authors_el: ET.Element) -> list[str]:
    """解析 <authors> 下的 <author> 列表。"""
    if authors_el is None:
        return []
    names = []
    for author in authors_el.findall("arxiv:author", NS):
        keyname = _get_text(author, "arxiv:keyname")
        forenames = _get_text(author, "arxiv:forenames")
        if forenames and keyname:
            names.append(f"{forenames} {keyname}")
        elif keyname:
            names.append(keyname)
    return names


def _parse_record(record_el: ET.Element) -> dict | None:
    """解析单条 OAI-PMH record → 标准 meta dict。"""
    header = record_el.find("oai:header", NS)
    if header is None:
        return None

    # 跳过被删除的记录
    status = header.get("status", "")
    if status == "deleted":
        return None

    metadata = record_el.find("oai:metadata", NS)
    if metadata is None:
        return None

    arxiv_el = metadata.find("arxiv:arXiv", NS)
    if arxiv_el is None:
        return None

    arxiv_id = _get_text(arxiv_el, "arxiv:id")
    if not arxiv_id:
        return None

    title = _get_text(arxiv_el, "arxiv:title").replace("\n", " ")
    abstract = _get_text(arxiv_el, "arxiv:abstract").strip()
    created = _get_text(arxiv_el, "arxiv:created")  # YYYY-MM-DD
    updated = _get_text(arxiv_el, "arxiv:updated")  # YYYY-MM-DD 或空

    # 分类
    categories_str = _get_text(arxiv_el, "arxiv:categories")
    categories = categories_str.split() if categories_str else []
    primary_category = categories[0] if categories else ""

    # 作者
    authors_el = arxiv_el.find("arxiv:authors", NS)
    authors = _parse_authors(authors_el)

    # 日期标准化
    published = f"{created}T00:00:00Z" if created else None
    updated_iso = f"{updated}T00:00:00Z" if updated else None

    return {
        "arxiv_id": arxiv_id,
        "title": title,
        "authors": authors,
        "summary": abstract,
        "primary_category": primary_category,
        "categories": categories,
        "published": published,
        "updated": updated_iso,
        "arxiv_url": f"https://arxiv.org/abs/{arxiv_id}",
        "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
        "source": "oai",
    }


def _request_oai(params: dict) -> ET.Element:
    """发送 OAI-PMH 请求，带重试。返回解析后的 XML 根元素。"""
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                BASE_URL, params=params, timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": "ArxivAnalyse/2.0 OAI-PMH Harvester"},
            )
            resp.raise_for_status()
            return ET.fromstring(resp.content)
        except Exception as e:
            last_err = e
            wait = RETRY_BACKOFF * attempt
            logger.warning(f"  OAI 请求失败 (第 {attempt} 次): {e}")
            if attempt < MAX_RETRIES:
                logger.info(f"  等待 {wait}s 后重试...")
                time.sleep(wait)
    raise RuntimeError(f"OAI 请求失败（{MAX_RETRIES} 次重试）: {last_err}")


def harvest(set_spec: str = "math", from_date: str | None = None,
            until_date: str | None = None):
    """
    生成器：逐批从 OAI-PMH 获取记录。

    Args:
        set_spec: arXiv set（如 'math'、'cs'、'physics'）
        from_date: 起始日期 YYYY-MM-DD（增量同步用）
        until_date: 截止日期 YYYY-MM-DD（可选）

    Yields:
        (records: list[dict], token: str|None, total_hint: str)
        每次 yield 一批解析好的记录。
    """
    # 第一次请求
    params = {
        "verb": "ListRecords",
        "metadataPrefix": METADATA_PREFIX,
        "set": set_spec,
    }
    if from_date:
        params["from"] = from_date
    if until_date:
        params["until"] = until_date

    page = 0
    while True:
        page += 1
        logger.info(f"OAI 请求第 {page} 页...")

        root = _request_oai(params)

        # 检查错误
        error_el = root.find("oai:error", NS)
        if error_el is not None:
            code = error_el.get("code", "")
            msg = error_el.text or ""
            if code == "noRecordsMatch":
                logger.info("没有匹配的记录（可能已是最新）")
                return
            raise RuntimeError(f"OAI 错误 [{code}]: {msg}")

        # 解析记录
        list_records = root.find("oai:ListRecords", NS)
        if list_records is None:
            logger.warning("响应中没有 ListRecords 元素")
            return

        records = []
        for record_el in list_records.findall("oai:record", NS):
            meta = _parse_record(record_el)
            if meta is not None:
                records.append(meta)

        # 解析 resumptionToken
        token_el = list_records.find("oai:resumptionToken", NS)
        token = None
        if token_el is not None and token_el.text:
            token = token_el.text.strip()

        yield records, token

        if not token:
            logger.info("没有更多页面，harvest 完成")
            break

        # 用 resumptionToken 请求下一页
        params = {
            "verb": "ListRecords",
            "resumptionToken": token,
        }

        # 请求间隔
        time.sleep(REQUEST_INTERVAL)


def get_server_time() -> str:
    """获取 OAI 服务器当前时间（用于记录同步点）。"""
    root = _request_oai({"verb": "Identify"})
    # 直接返回 UTC 当前时间作为近似值
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
