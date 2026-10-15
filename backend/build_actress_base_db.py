import argparse
import os
import sys
import time
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(description="Build a portable Netflav actress base database.")
    parser.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parents[1] / "data" / "actress_base.db"),
        help="Output SQLite database path. Default: data/actress_base.db",
    )
    parser.add_argument("--start-page", type=int, default=1, help="First Netflav actress page to fetch.")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="How many pages to fetch. 0 means all pages reported by Netflav.",
    )
    parser.add_argument(
        "--detail-limit",
        type=int,
        default=0,
        help="How many actress detail pages to fetch. 0 means all actresses from fetched pages.",
    )
    parser.add_argument("--delay", type=float, default=1.2, help="Delay between actress detail requests.")
    parser.add_argument("--force-refresh", action="store_true", help="Ignore local cache in the output DB.")
    parser.add_argument("--save-movies", action="store_true", help="Save first-page Netflav movie docs from each actress page.")
    parser.add_argument("--reset", action="store_true", help="Delete the output DB before building.")
    parser.add_argument("--cooldown-after-failures", type=int, default=8, help="Pause after this many consecutive detail failures.")
    parser.add_argument("--cooldown-seconds", type=float, default=60.0, help="Cooldown duration after repeated failures.")
    parser.add_argument("--skip-existing-detail", action="store_true", help="Skip detail fetch when actor already has basic metadata.")
    parser.add_argument("--skip-failed-pages", action="store_true", help="Continue when a Netflav actress list page fails.")
    return parser.parse_args()


def main():
    args = parse_args()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    if args.reset:
        for suffix in ("", "-shm", "-wal"):
            target = Path(str(output) + suffix)
            if target.exists():
                target.unlink()

    os.environ["DATABASE_URL"] = f"sqlite:///{output.as_posix()}"

    from database import db
    from netflav_scraper import netflav_scraper

    print(f"Output DB: {output}", flush=True)
    print("Source: https://netflav.com/actress", flush=True)
    print(
        f"Options: start_page={args.start_page}, max_pages={args.max_pages or 'all'}, "
        f"detail_limit={args.detail_limit or 'all'}, delay={args.delay}s, save_movies={args.save_movies}",
        flush=True,
    )

    first_page = None
    try:
        first_page = netflav_scraper.get_actress_page(page=args.start_page, force_refresh=args.force_refresh)
        total_pages = int(first_page.get("pages") or args.start_page)
    except Exception as exc:
        if not args.skip_failed_pages or args.max_pages <= 0:
            raise
        print(f"[{args.start_page}] actress list failed: {exc}", flush=True)
        print("Continuing because --skip-failed-pages is enabled and --max-pages is set.", flush=True)
        total_pages = args.start_page + args.max_pages - 1

    if args.max_pages > 0:
        end_page = min(total_pages, args.start_page + args.max_pages - 1)
    else:
        end_page = total_pages

    pages_done = 0
    list_count = 0
    detail_count = 0
    skipped_detail = 0
    failed_detail = 0
    failed_pages = 0
    consecutive_failures = 0
    started_at = time.time()

    for page in range(args.start_page, end_page + 1):
        try:
            page_data = first_page if page == args.start_page and first_page is not None else netflav_scraper.get_actress_page(page=page, force_refresh=args.force_refresh)
        except Exception as exc:
            failed_pages += 1
            print(f"\n[{page}/{end_page}] actress list failed: {exc}", flush=True)
            if not args.skip_failed_pages:
                raise
            if args.cooldown_seconds > 0:
                print(f"  page cooldown: sleeping {args.cooldown_seconds}s", flush=True)
                time.sleep(args.cooldown_seconds)
            continue

        docs = page_data.get("docs") or []
        pages_done += 1
        list_count += len(docs)

        print(f"\n[{page}/{end_page}] actress list: {len(docs)} items", flush=True)

        for index, item in enumerate(docs, start=1):
            list_data = netflav_scraper._build_actress_data(item)
            db.save_actress(list_data)

            if args.detail_limit and detail_count >= args.detail_limit:
                continue

            name = item.get("name") or item.get("name_en") or item.get("_id")
            if args.skip_existing_detail:
                existing = db.get_actress(list_data["id"])
                if existing and (
                    existing.get("metadata_last_attempt_at")
                    or existing.get("source_updated_at")
                    or (existing.get("birth_date") and existing.get("height") and existing.get("avatar"))
                ):
                    skipped_detail += 1
                    print(f"  skip existing: p{page} #{index}/{len(docs)} {name}", flush=True)
                    continue

            try:
                detail = netflav_scraper.get_actress_detail(
                    item,
                    force_refresh=args.force_refresh,
                    save_movies=args.save_movies,
                )
                detail_count += 1
                consecutive_failures = 0
                birthday = detail.get("birth_date") if detail else None
                print(
                    f"  detail {detail_count}: p{page} #{index}/{len(docs)} {name} "
                    f"birthday={birthday or '-'}",
                    flush=True,
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                failed_detail += 1
                consecutive_failures += 1
                print(f"  detail failed: p{page} #{index}/{len(docs)} {name}: {exc}", flush=True)
                if args.cooldown_after_failures > 0 and consecutive_failures >= args.cooldown_after_failures:
                    print(
                        f"  cooldown: {consecutive_failures} consecutive failures, sleeping {args.cooldown_seconds}s",
                        flush=True,
                    )
                    time.sleep(max(args.cooldown_seconds, 0))
                    consecutive_failures = 0

            time.sleep(max(args.delay, 0))

    elapsed = time.time() - started_at
    print("\nDone.", flush=True)
    print(
        f"Pages: {pages_done}, list actresses: {list_count}, detail actresses: {detail_count}, "
        f"skipped existing: {skipped_detail}, failed detail: {failed_detail}, failed pages: {failed_pages}",
        flush=True,
    )
    print(f"Elapsed: {elapsed:.1f}s", flush=True)
    print(f"Portable DB ready: {output}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user.", flush=True)
        sys.exit(130)
