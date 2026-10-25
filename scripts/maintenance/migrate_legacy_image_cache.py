import argparse
import hashlib
import shutil
import sqlite3
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

SAFE_REMOTE_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}


def normalize_url(raw_url):
    value = str(raw_url or "").strip()
    if not value:
        return None
    if value.startswith("//"):
        value = f"https:{value}"
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        return None
    netloc = host
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    query = parsed.query
    if query:
        query = urlencode(sorted(parse_qsl(query, keep_blank_values=True)), doseq=True)
    return urlunparse((scheme, netloc, parsed.path or "/", "", query, ""))


def iter_known_image_urls(db_path: Path):
    if not db_path.exists():
        return

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        for table, column in (("movies", "cover"), ("actresses", "avatar")):
            try:
                rows = conn.execute(
                    f"SELECT {column} AS image_url FROM {table} WHERE {column} IS NOT NULL AND {column} != ''"
                )
            except sqlite3.Error:
                continue
            for row in rows:
                image_url = normalize_url(row["image_url"])
                if image_url:
                    yield image_url
    finally:
        conn.close()


def build_plan(db_path: Path, cache_root: Path, limit: int = 0):
    proxy_dir = cache_root / "proxy"
    seen = set()
    plan = []

    for image_url in iter_known_image_urls(db_path):
        if image_url in seen:
            continue
        seen.add(image_url)

        legacy_name = hashlib.md5(image_url.encode("utf-8")).hexdigest()
        target_name = hashlib.sha256(image_url.encode("utf-8")).hexdigest()
        for ext in SAFE_REMOTE_IMAGE_EXTENSIONS:
            legacy_path = cache_root / f"{legacy_name}{ext}"
            target_path = proxy_dir / f"{target_name}{ext}"
            if legacy_path.exists() and not target_path.exists():
                plan.append((legacy_path, target_path))
                if limit and len(plan) >= limit:
                    return plan
                break
    return plan


def main():
    parser = argparse.ArgumentParser(description="Copy legacy image cache files into data/cover_cache/proxy.")
    default_root = Path(__file__).resolve().parents[1]
    parser.add_argument("--db", default=str(default_root / "data" / "movies.db"), help="SQLite database path.")
    parser.add_argument("--cache-root", default=str(default_root / "data" / "cover_cache"), help="Image cache root.")
    parser.add_argument("--apply", action="store_true", help="Actually copy files. Default is dry-run.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum files to copy. 0 means no limit.")
    args = parser.parse_args()

    db_path = Path(args.db)
    cache_root = Path(args.cache_root)
    plan = build_plan(db_path, cache_root, args.limit)

    total_bytes = sum(source.stat().st_size for source, _target in plan if source.exists())
    print(f"legacy_cache_candidates={len(plan)}")
    print(f"estimated_copy_mb={total_bytes / 1024 / 1024:.2f}")
    print(f"mode={'apply' if args.apply else 'dry-run'}")

    if not args.apply:
        for source, target in plan[:20]:
            print(f"would_copy {source} -> {target}")
        if len(plan) > 20:
            print(f"... {len(plan) - 20} more")
        return

    copied = 0
    for source, target in plan:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            continue
        shutil.copy2(source, target)
        copied += 1
    print(f"copied={copied}")


if __name__ == "__main__":
    main()
