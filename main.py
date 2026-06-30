#!/usr/bin/env python3
"""
Arxiv Analyse — 统一入口。

  python main.py init                # 首次：配置 Kaggle → 下载 → 建库 → 生成文件夹
  python main.py download            # 下载 PDF（10 并发）
  python main.py download --year 2024
  python main.py update              # 一键增量同步
  python main.py stats               # 查看统计
  python main.py retry               # 重试失败的下载
"""

import argparse
import json
import logging
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from lib.database import (
    init_db, get_db, upsert_papers_batch, mark_downloaded, mark_failed,
    get_pending_papers, count_by_status, reset_failed_to_pending,
    get_stats, get_year_stats, get_category_stats,
    get_latest_published, get_sync_state, set_sync_state,
    extract_year, safe_id, now_iso,
)
from lib.kaggle_loader import (
    ensure_kaggle_env, has_credentials, setup_credentials,
    download_dataset, iter_math_papers,
)
from lib.oai_client import harvest, get_server_time
from lib.pdf_downloader import download_one, cleanup_incomplete

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

PAPERS_DIR = ROOT / "data" / "papers"
BATCH_SIZE = 5000
DOWNLOAD_WORKERS = 100
DOWNLOAD_BATCH = 10000

_interrupted = False

def _handle_signal(sig, frame):
    global _interrupted
    if _interrupted:
        logger.warning("强制退出")
        sys.exit(1)
    _interrupted = True
    logger.warning("收到中断信号，当前任务完成后停止（再按一次强制退出）")

signal.signal(signal.SIGINT, _handle_signal)


# ── meta.json 写入 ────────────────────────────────────

def write_meta_json(meta: dict):
    """将单篇论文的 meta.json 写到 {year}/{arxiv_id}/meta.json"""
    year = extract_year(meta["arxiv_id"], meta.get("published"))
    year_str = str(year) if year else "unknown"
    sid = safe_id(meta["arxiv_id"])
    paper_dir = PAPERS_DIR / year_str / sid
    paper_dir.mkdir(parents=True, exist_ok=True)

    out = {
        "arxiv_id": meta["arxiv_id"],
        "title": meta["title"],
        "authors": meta["authors"],
        "summary": meta["summary"],
        "primary_category": meta["primary_category"],
        "categories": meta["categories"],
        "published": meta.get("published"),
        "updated": meta.get("updated"),
        "arxiv_url": meta.get("arxiv_url", f"https://arxiv.org/abs/{meta['arxiv_id']}"),
        "pdf_url": meta.get("pdf_url", f"https://arxiv.org/pdf/{meta['arxiv_id']}"),
        "pdf_path": f"{year_str}/{sid}/paper.pdf",
    }

    meta_path = paper_dir / "meta.json"
    meta_path.write_text(
        json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ══════════════════════════════════════════════════════
#  init — 首次初始化
# ══════════════════════════════════════════════════════

def cmd_init(args):
    """首次运行：配置 Kaggle → 下载数据集 → 导入 SQLite → 生成 meta.json"""
    init_db()
    PAPERS_DIR.mkdir(parents=True, exist_ok=True)

    # ── 1. Kaggle 凭证（保存在项目 data/.kaggle/ 内） ──
    ensure_kaggle_env()

    if not has_credentials() or args.reconfigure:
        print()
        print("=" * 50)
        print("  Kaggle 认证配置")
        print("=" * 50)
        print()
        print("  前往 https://www.kaggle.com/settings/api")
        print("  点击 Create New Token 获取 username 和 key")
        print()
        username = input("  Kaggle 用户名: ").strip()
        api_key = input("  Kaggle API Key: ").strip()
        if not username or not api_key:
            logger.error("用户名和 API Key 不能为空")
            sys.exit(1)
        setup_credentials(username, api_key)
        print()
    else:
        logger.info("Kaggle 凭证已存在（使用 --reconfigure 重新配置）")

    # ── 2. 下载数据集（缓存在项目 data/cache/ 内） ──
    json_path = download_dataset(force=args.force_download)

    # ── 3. 解析 + 写入 ──
    category = args.category
    logger.info(f"导入分类: {category}.*")
    logger.info("解析 + 写入 SQLite + 生成 meta.json...")
    logger.info("")

    conn = get_db()
    batch = []
    meta_batch = []
    total_matched = 0
    total_written = 0
    start_time = time.time()

    for meta in iter_math_papers(json_path, category):
        if _interrupted:
            break

        total_matched += 1
        batch.append(meta)
        meta_batch.append(meta)

        if len(batch) >= BATCH_SIZE:
            upsert_papers_batch(conn, batch)
            conn.commit()
            total_written += len(batch)

            for m in meta_batch:
                write_meta_json(m)

            elapsed = time.time() - start_time
            rate = total_matched / elapsed if elapsed > 0 else 0
            logger.info(
                f"  匹配 {total_matched:,} | 写入 {total_written:,} | "
                f"{rate:,.0f} 篇/秒"
            )
            batch = []
            meta_batch = []

    if batch:
        upsert_papers_batch(conn, batch)
        conn.commit()
        total_written += len(batch)
        for m in meta_batch:
            write_meta_json(m)

    set_sync_state(conn, "kaggle_imported", "true")
    set_sync_state(conn, "kaggle_import_time", now_iso())
    conn.commit()
    conn.close()

    elapsed = time.time() - start_time
    print()
    print("=" * 55)
    print("  初始化完成")
    print("=" * 55)
    print(f"  匹配论文:    {total_matched:,}")
    print(f"  写入数据库:  {total_written:,}")
    print(f"  耗时:        {elapsed:.1f}s")
    print()
    print("  所有数据存储在项目 data/ 目录内（凭证、缓存、数据库、文件）")
    print()
    print("  下一步:")
    print("    python main.py download          # 下载 PDF")
    print("    python main.py stats             # 查看统计")
    print()


# ══════════════════════════════════════════════════════
#  download — 下载 PDF
# ══════════════════════════════════════════════════════

def _download_worker(row: dict) -> tuple[str, str | None, int, str | None]:
    """线程内下载单篇。返回 (arxiv_id, pdf_path, size, error)"""
    aid = row["arxiv_id"]
    try:
        pdf_path, size = download_one(
            aid, year=row["year"], papers_dir=PAPERS_DIR,
            pdf_url=row.get("pdf_url"),
        )
        return aid, pdf_path, size, None
    except Exception as e:
        return aid, None, 0, str(e)


def cmd_download(args):
    """下载 PDF，10 并发 + 随机延迟。"""
    init_db()
    PAPERS_DIR.mkdir(parents=True, exist_ok=True)

    workers = args.workers
    year = args.year

    conn = get_db()
    pending = count_by_status(conn, "pending", year=year)
    conn.close()

    if pending == 0:
        logger.info("没有待下载的论文")
        return

    logger.info(f"待下载: {pending:,} 篇, 并发: {workers}")
    logger.info(f"策略: arxiv.org, 随机 2~3s 延迟/线程, 5 次重试")
    logger.info("")

    round_num = 0
    while True:
        round_num += 1
        if round_num > 1:
            conn = get_db()
            reset_count = reset_failed_to_pending(conn, year=year)
            conn.commit()
            conn.close()
            if reset_count == 0:
                logger.info("全部完成")
                break
            logger.info(f"第 {round_num} 轮: 重置 {reset_count} 篇 failed → pending")

        total_done = 0
        total_failed = 0

        while not _interrupted:
            conn = get_db()
            batch = get_pending_papers(conn, limit=DOWNLOAD_BATCH, year=year)
            remaining = count_by_status(conn, "pending", year=year)
            conn.close()

            if not batch:
                break

            logger.info(f"  批次 {len(batch)} 篇, 剩余 ~{remaining:,}")

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(_download_worker, row): row
                    for row in batch
                    if not _interrupted
                }

                for fut in as_completed(futures):
                    if _interrupted:
                        break
                    row = futures[fut]
                    aid, pdf_path, size, error = fut.result()

                    conn = get_db()
                    if pdf_path:
                        mark_downloaded(conn, aid, pdf_path, size)
                        conn.commit()
                        total_done += 1
                        logger.info(f"  ✓ [{row['year']}] {aid} ({size/1024:.0f}KB)")
                    else:
                        mark_failed(conn, aid, error)
                        conn.commit()
                        total_failed += 1
                        logger.warning(f"  ✗ [{row['year']}] {aid}: {error}")
                    conn.close()

        logger.info(f"本轮: {total_done} 成功, {total_failed} 失败")

        if _interrupted or not args.loop:
            break
        if total_failed == 0:
            break
        logger.info("等待 60s 后开始下一轮...")
        time.sleep(60)


# ══════════════════════════════════════════════════════
#  update — 一键增量同步
# ══════════════════════════════════════════════════════

def cmd_update(args):
    """查询 SQLite 最新日期 → OAI-PMH 增量同步 → 写入 SQLite + meta.json"""
    init_db()
    PAPERS_DIR.mkdir(parents=True, exist_ok=True)

    conn = get_db()
    latest = get_latest_published(conn)
    conn.close()

    if not latest:
        logger.error("数据库为空，请先运行 init")
        sys.exit(1)

    category = args.category
    logger.info(f"数据库最新论文: {latest}")
    logger.info(f"从 {latest} 开始增量同步 ({category})...")
    logger.info("")

    conn = get_db()
    total_new = 0
    total_pages = 0
    start_time = time.time()

    sync_start = get_server_time()

    try:
        for records, token in harvest(
            set_spec=category, from_date=latest,
        ):
            if _interrupted:
                break

            total_pages += 1

            filtered = [
                r for r in records
                if any(c.startswith(category) for c in r.get("categories", []))
            ]

            if filtered:
                upsert_papers_batch(conn, filtered)
                conn.commit()
                total_new += len(filtered)

                for meta in filtered:
                    write_meta_json(meta)

            elapsed = time.time() - start_time
            logger.info(
                f"  第 {total_pages} 页: +{len(filtered)} 条 | "
                f"累计 {total_new:,} | {elapsed:.0f}s"
            )

            if token:
                set_sync_state(conn, "oai_resumption_token", token)
                conn.commit()

    except Exception as e:
        logger.error(f"同步出错: {e}")
        conn.commit()
        conn.close()
        raise

    if not _interrupted:
        set_sync_state(conn, "oai_last_harvest", sync_start)
        conn.execute("DELETE FROM sync_state WHERE key='oai_resumption_token'")
        conn.commit()

    conn.close()

    elapsed = time.time() - start_time
    print()
    print("=" * 55)
    print("  增量同步完成")
    print("=" * 55)
    print(f"  新增论文:    {total_new:,}")
    print(f"  同步页数:    {total_pages}")
    print(f"  耗时:        {elapsed:.0f}s")
    if not _interrupted and total_new > 0:
        print()
        print("  下一步: python main.py download")
    print()


# ══════════════════════════════════════════════════════
#  stats — 统计
# ══════════════════════════════════════════════════════

def cmd_stats(args):
    init_db()
    conn = get_db()
    stats = get_stats(conn)
    year_stats = get_year_stats(conn)
    cat_stats = get_category_stats(conn, limit=20)
    kaggle_time = get_sync_state(conn, "kaggle_import_time")
    oai_last = get_sync_state(conn, "oai_last_harvest")
    latest = get_latest_published(conn)
    conn.close()

    print()
    print("=" * 60)
    print("  Arxiv Analyse — 数据统计")
    print("=" * 60)
    print()
    print(f"  论文总数:     {stats['total']:,}")
    print(f"  已下载 PDF:   {stats['downloaded']:,}")
    print(f"  下载失败:     {stats['failed']:,}")
    print(f"  待下载:       {stats['pending']:,}")
    print()
    print(f"  Kaggle 导入:  {kaggle_time or '未导入'}")
    print(f"  OAI 最后同步: {oai_last or '从未同步'}")
    print(f"  最新论文日期: {latest or 'N/A'}")

    if year_stats:
        print()
        print(f"  {'年份':>6s}  {'总数':>8s}  {'已下载':>8s}  {'失败':>6s}  {'待下载':>8s}")
        print("  " + "-" * 44)
        for row in year_stats:
            y = row["year"] if row["year"] else "未知"
            print(
                f"  {y:>6}  {row['total']:>8,}  "
                f"{row['downloaded']:>8,}  {row['failed']:>6,}  "
                f"{row['pending']:>8,}"
            )

    if cat_stats:
        print()
        print("  分类 (Top 20):")
        for row in cat_stats:
            print(f"    {row['primary_category']:20s} {row['c']:>8,}")

    print()


# ══════════════════════════════════════════════════════
#  retry — 重试失败
# ══════════════════════════════════════════════════════

def cmd_retry(args):
    init_db()
    conn = get_db()
    count = reset_failed_to_pending(conn, year=args.year)
    conn.commit()
    conn.close()

    if count == 0:
        logger.info("没有失败的论文")
        return

    logger.info(f"重置 {count} 篇 failed → pending")
    year_hint = f" --year {args.year}" if args.year else ""
    logger.info(f"运行: python main.py download{year_hint}")


# ══════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Arxiv Analyse — arXiv 数学论文采集管线",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="首次初始化（Kaggle 下载 + 建库）")
    p_init.add_argument("--category", default="math", help="分类（默认 math）")
    p_init.add_argument("--force-download", action="store_true", help="强制重下数据集")
    p_init.add_argument("--reconfigure", action="store_true", help="重新配置 Kaggle 凭证")

    p_dl = sub.add_parser("download", help="下载 PDF")
    p_dl.add_argument("--year", type=int, help="只下载指定年份")
    p_dl.add_argument("--workers", type=int, default=DOWNLOAD_WORKERS,
                       help=f"并发线程数（默认 {DOWNLOAD_WORKERS}）")
    p_dl.add_argument("--loop", action="store_true", help="循环重试直到全部成功")

    p_up = sub.add_parser("update", help="一键增量同步（OAI-PMH）")
    p_up.add_argument("--category", default="math", help="分类（默认 math）")

    sub.add_parser("stats", help="显示统计信息")

    p_retry = sub.add_parser("retry", help="重试失败的下载")
    p_retry.add_argument("--year", type=int, help="只重试指定年份")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        print()
        print("快速开始:")
        print("  python main.py init       # 首次运行")
        print("  python main.py download   # 下载 PDF")
        print("  python main.py update     # 增量更新")
        print("  python main.py stats      # 查看统计")
        sys.exit(0)

    cmd_map = {
        "init": cmd_init,
        "download": cmd_download,
        "update": cmd_update,
        "stats": cmd_stats,
        "retry": cmd_retry,
    }
    cmd_map[args.command](args)


if __name__ == "__main__":
    main()
