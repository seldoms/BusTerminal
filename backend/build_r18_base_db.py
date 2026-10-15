import argparse
import gzip
import io
import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path


R18_LATEST_DUMP_URL = "https://r18.dev/dumps/latest"
DMM_IMAGE_BASE = "https://pics.dmm.co.jp/"
DMM_ACTRESS_IMAGE_BASE = "https://pics.dmm.co.jp/mono/actjpgs/"


def parse_args():
    parser = argparse.ArgumentParser(description="Build a portable actress base database from an R18.dev SQL dump.")
    parser.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parents[1] / "data" / "actress_base.db"),
        help="Output SQLite database path. Default: data/actress_base.db",
    )
    parser.add_argument("--dump-file", help="Local r18dotdev_dump_*.sql.gz file. If omitted, stream latest dump.")
    parser.add_argument("--dump-url", default=R18_LATEST_DUMP_URL, help="Dump URL when --dump-file is omitted.")
    parser.add_argument("--reset", action="store_true", help="Delete the output DB before importing.")
    parser.add_argument("--max-actresses", type=int, default=0, help="Maximum actresses to import. 0 means all.")
    parser.add_argument("--include-movies", action="store_true", help="Import video metadata from the dump.")
    parser.add_argument(
        "--max-videos",
        type=int,
        default=0,
        help="Maximum videos to import when --include-movies is set. 0 means all.",
    )
    parser.add_argument("--batch-size", type=int, default=2000, help="SQLite batch size.")
    return parser.parse_args()


def open_dump(args):
    if args.dump_file:
        return gzip.open(args.dump_file, "rb"), args.dump_file

    req = urllib.request.Request(args.dump_url, headers={"User-Agent": "BusTerminal R18 base importer/1.0"})
    resp = urllib.request.urlopen(req, timeout=90)
    return gzip.GzipFile(fileobj=resp), resp.geturl()


def pg_value(value):
    if value == "\\N":
        return None

    result = []
    i = 0
    while i < len(value):
        char = value[i]
        if char == "\\" and i + 1 < len(value):
            next_char = value[i + 1]
            if next_char == "t":
                result.append("\t")
            elif next_char == "n":
                result.append("\n")
            elif next_char == "r":
                result.append("\r")
            else:
                result.append(next_char)
            i += 2
            continue
        result.append(char)
        i += 1
    return "".join(result)


def parse_copy_header(line):
    table = line.split("COPY ", 1)[1].split(" ", 1)[0]
    columns = line.split("(", 1)[1].split(")", 1)[0]
    return table, [column.strip().strip('"') for column in columns.split(",")]


def parse_copy_row(line, columns):
    values = [pg_value(value) for value in line.rstrip("\n").split("\t")]
    return dict(zip(columns, values))


def normalize_missing(value):
    if value in (None, "", "null", "None", "----"):
        return None
    return value


def dmm_image(path):
    path = normalize_missing(path)
    if not path:
        return None
    if path.startswith("http://") or path.startswith("https://"):
        return path
    suffix = "" if path.lower().endswith((".jpg", ".jpeg", ".png", ".webp")) else ".jpg"
    return DMM_IMAGE_BASE + path.lstrip("/") + suffix


def dmm_actress_image(path):
    path = normalize_missing(path)
    if not path:
        return None
    if path.startswith("http://") or path.startswith("https://"):
        return path
    suffix = "" if path.lower().endswith((".jpg", ".jpeg", ".png", ".webp")) else ".jpg"
    return DMM_ACTRESS_IMAGE_BASE + path.lstrip("/") + suffix


def lookup_item(items, item_id):
    if not item_id:
        return None
    item = items.get(str(item_id))
    if not item:
        return None
    return {key: value for key, value in item.items() if value}


def category_name(category):
    if not category:
        return None
    return category.get("name_ja") or category.get("name_en")


def compact_json(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def build_actress(row, now):
    source_id = row.get("id")
    name = normalize_missing(row.get("name_kanji")) or normalize_missing(row.get("name_romaji")) or normalize_missing(row.get("name_kana"))
    name_en = normalize_missing(row.get("name_romaji"))

    aliases = []
    names = []
    for value in (name, row.get("name_romaji"), row.get("name_kanji"), row.get("name_kana")):
        value = normalize_missing(value)
        if value and value not in names:
            names.append(value)
        if value and value != name and value not in aliases:
            aliases.append(value)

    actress_id = f"r18:{source_id}"
    return {
        "row": (
            actress_id,
            name or actress_id,
            dmm_actress_image(row.get("image_url")),
            ", ".join(aliases),
            name_en,
            "r18",
            str(source_id),
            now,
            now,
            now,
        ),
        "names": [
            (actress_id, item, "primary" if item == name else "alias", "r18", now)
            for item in names
        ],
        "cache": {
            "id": actress_id,
            "name": name or actress_id,
            "name_en": name_en,
            "source_id": str(source_id),
        },
    }


def build_movie(row, makers, labels, series, now):
    content_id = row.get("content_id")
    code = normalize_missing(row.get("dvd_id")) or content_id
    title_en = normalize_missing(row.get("title_en"))
    title_ja = normalize_missing(row.get("title_ja"))
    title = title_ja or title_en or code

    detail = {
        "source": "r18",
        "r18_content_id": content_id,
        "code": code,
        "dvd_id": row.get("dvd_id"),
        "title_en": title_en,
        "title_ja": title_ja,
        "comment_en": normalize_missing(row.get("comment_en")),
        "comment_ja": normalize_missing(row.get("comment_ja")),
        "runtime_mins": row.get("runtime_mins"),
        "sample_url": normalize_missing(row.get("sample_url")),
        "maker": lookup_item(makers, row.get("maker_id")),
        "label": lookup_item(labels, row.get("label_id")),
        "series": lookup_item(series, row.get("series_id")),
        "gallery": {
            "full_first": dmm_image(row.get("gallery_full_first")),
            "full_last": dmm_image(row.get("gallery_full_last")),
            "thumb_first": dmm_image(row.get("gallery_thumb_first")),
            "thumb_last": dmm_image(row.get("gallery_thumb_last")),
        },
        "tags": [],
        "stars": [],
    }

    return {
        "content_id": content_id,
        "movie_id": code,
        "row": (
            code,
            title,
            title_ja or title_en or "",
            dmm_image(row.get("jacket_full_url")) or dmm_image(row.get("jacket_thumb_url")),
            f"https://r18.dev/videos/vod/movies/detail/-/id={content_id}/",
            row.get("release_date") or "",
            now,
            compact_json(detail),
        ),
    }


def init_output_db(output):
    os.environ["DATABASE_URL"] = f"sqlite:///{output.as_posix()}"
    from database import db  # noqa: F401 - importing initializes and migrates the output schema

    conn = sqlite3.connect(output)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def init_temp_tables(conn):
    conn.executescript(
        """
        CREATE TEMP TABLE IF NOT EXISTS r18_selected_movies (
            content_id TEXT PRIMARY KEY,
            movie_id TEXT NOT NULL
        );
        CREATE TEMP TABLE IF NOT EXISTS r18_video_actress (
            content_id TEXT NOT NULL,
            actress_id TEXT NOT NULL
        );
        CREATE TEMP TABLE IF NOT EXISTS r18_video_category (
            content_id TEXT NOT NULL,
            category_id TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS temp.idx_r18_video_actress_content ON r18_video_actress (content_id);
        CREATE INDEX IF NOT EXISTS temp.idx_r18_video_category_content ON r18_video_category (content_id);
        """
    )


def flush_actresses(conn, actress_rows, name_rows):
    if actress_rows:
        conn.executemany(
            """
            INSERT INTO actresses (
                id, name, avatar, aliases, name_en, source, source_id,
                source_updated_at, last_updated, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                avatar = COALESCE(excluded.avatar, actresses.avatar),
                aliases = excluded.aliases,
                name_en = excluded.name_en,
                source = excluded.source,
                source_id = excluded.source_id,
                source_updated_at = excluded.source_updated_at,
                last_updated = excluded.last_updated
            """,
            actress_rows,
        )
        actress_rows.clear()

    if name_rows:
        conn.executemany(
            """
            INSERT INTO actress_names (actress_id, name, name_type, source, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(actress_id, name) DO UPDATE SET
                name_type = COALESCE(excluded.name_type, actress_names.name_type),
                source = COALESCE(excluded.source, actress_names.source)
            """,
            name_rows,
        )
        name_rows.clear()


def flush_movies(conn, movie_rows, selected_rows):
    if movie_rows:
        conn.executemany(
            """
            INSERT INTO movies (id, title, original_title, cover, detail_url, release_date, scraped_at, detail_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                original_title = excluded.original_title,
                cover = excluded.cover,
                detail_url = excluded.detail_url,
                release_date = excluded.release_date,
                scraped_at = excluded.scraped_at,
                detail_json = excluded.detail_json
            """,
            movie_rows,
        )
        movie_rows.clear()

    if selected_rows:
        conn.executemany(
            "INSERT OR IGNORE INTO r18_selected_movies (content_id, movie_id) VALUES (?, ?)",
            selected_rows,
        )
        selected_rows.clear()


def flush_relations(conn, actress_rows, category_rows):
    if actress_rows:
        conn.executemany(
            "INSERT INTO r18_video_actress (content_id, actress_id) VALUES (?, ?)",
            actress_rows,
        )
        actress_rows.clear()

    if category_rows:
        conn.executemany(
            "INSERT INTO r18_video_category (content_id, category_id) VALUES (?, ?)",
            category_rows,
        )
        category_rows.clear()


def select_relation_rows(conn, table, batch_content_ids):
    placeholders = ",".join("?" for _ in batch_content_ids)
    rows = conn.execute(
        f"SELECT content_id, {'actress_id' if table == 'r18_video_actress' else 'category_id'} AS item_id "
        f"FROM {table} WHERE content_id IN ({placeholders})",
        batch_content_ids,
    ).fetchall()
    grouped = {}
    for row in rows:
        grouped.setdefault(row["content_id"], []).append(row["item_id"])
    return grouped


def merge_movie_relations(conn, actress_cache, categories, batch_size):
    selected = conn.execute("SELECT content_id, movie_id FROM r18_selected_movies ORDER BY content_id")
    update_rows = []
    total = 0
    next_progress = 10000

    while True:
        batch = selected.fetchmany(batch_size)
        if not batch:
            break

        content_ids = [row["content_id"] for row in batch]
        movie_ids = [row["movie_id"] for row in batch]
        movie_by_id = {
            row["id"]: row["detail_json"]
            for row in conn.execute(
                f"SELECT id, detail_json FROM movies WHERE id IN ({','.join('?' for _ in movie_ids)})",
                movie_ids,
            )
        }
        actress_links = select_relation_rows(conn, "r18_video_actress", content_ids)
        category_links = select_relation_rows(conn, "r18_video_category", content_ids)

        for item in batch:
            content_id = item["content_id"]
            movie_id = item["movie_id"]
            detail_json = movie_by_id.get(movie_id)
            if not detail_json:
                continue

            detail = json.loads(detail_json)
            stars = []
            for actress_id in actress_links.get(content_id, []):
                actress = actress_cache.get(str(actress_id))
                if not actress:
                    continue
                stars.append(
                    {
                        "id": actress["id"],
                        "name": actress.get("name"),
                        "name_en": actress.get("name_en"),
                        "source": "r18",
                        "source_id": actress.get("source_id"),
                    }
                )

            tags = []
            for category_id in category_links.get(content_id, []):
                name = category_name(categories.get(str(category_id)))
                if name and name not in tags:
                    tags.append(name)

            if not stars and not tags:
                continue

            detail["stars"] = stars
            detail["tags"] = tags
            update_rows.append((compact_json(detail), movie_id))
            total += 1

        if update_rows:
            conn.executemany("UPDATE movies SET detail_json = ? WHERE id = ?", update_rows)
            conn.commit()
            update_rows.clear()

        if total >= next_progress:
            print(f"  merged movie relations: {total}", flush=True)
            next_progress += 10000

    print("  updating actress video counts...", flush=True)
    conn.execute("DROP TABLE IF EXISTS r18_actress_counts")
    conn.execute(
        """
        CREATE TEMP TABLE r18_actress_counts AS
        SELECT actress_id, COUNT(DISTINCT content_id) AS video_count
        FROM r18_video_actress
        GROUP BY actress_id
        """
    )
    conn.execute("CREATE INDEX idx_r18_actress_counts_id ON r18_actress_counts (actress_id)")
    conn.execute(
        """
        UPDATE actresses
        SET video_count = (
            SELECT video_count FROM r18_actress_counts
            WHERE r18_actress_counts.actress_id = actresses.source_id
        )
        WHERE source = 'r18'
          AND EXISTS (
            SELECT 1 FROM r18_actress_counts
            WHERE r18_actress_counts.actress_id = actresses.source_id
          )
        """
    )
    conn.commit()
    return total


def main():
    args = parse_args()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    if args.reset:
        for suffix in ("", "-shm", "-wal"):
            target = Path(str(output) + suffix)
            if target.exists():
                target.unlink()

    conn = init_output_db(output)
    if args.include_movies:
        init_temp_tables(conn)

    print(f"Output DB: {output}", flush=True)
    print(f"Include movies: {args.include_movies}, max_videos={args.max_videos if args.max_videos else 'all'}", flush=True)
    print(f"Batch size: {args.batch_size}", flush=True)

    stream, source = open_dump(args)
    print(f"Source dump: {source}", flush=True)

    now = time.time()
    actress_cache = {}
    selected_content_ids = set()
    categories = {}
    makers = {}
    labels = {}
    series = {}
    actress_rows = []
    actress_name_rows = []
    movie_rows = []
    selected_movie_rows = []
    relation_actress_rows = []
    relation_category_rows = []
    counts = {
        "actresses": 0,
        "movies": 0,
        "video_actress_links": 0,
        "video_category_links": 0,
    }
    relation_progress = {
        "video_actress_links": 1000000,
        "video_category_links": 1000000,
    }
    started_at = time.time()

    try:
        text = io.TextIOWrapper(stream, encoding="utf-8", errors="replace")
        current_table = None
        current_columns = []

        for line in text:
            if line.startswith("COPY public."):
                current_table, current_columns = parse_copy_header(line)
                continue

            if not current_table:
                continue

            if line == "\\.\n":
                current_table = None
                current_columns = []
                continue

            if current_table == "public.derived_actress":
                if args.max_actresses and counts["actresses"] >= args.max_actresses:
                    continue
                row = parse_copy_row(line, current_columns)
                actress = build_actress(row, now)
                actress_rows.append(actress["row"])
                actress_name_rows.extend(actress["names"])
                actress_cache[str(row["id"])] = actress["cache"]
                counts["actresses"] += 1
                if len(actress_rows) >= args.batch_size:
                    flush_actresses(conn, actress_rows, actress_name_rows)
                    conn.commit()
                if counts["actresses"] % 10000 == 0:
                    print(f"  actresses: {counts['actresses']}", flush=True)

            elif current_table == "public.derived_category":
                row = parse_copy_row(line, current_columns)
                categories[str(row["id"])] = {
                    "id": row["id"],
                    "name_en": normalize_missing(row.get("name_en")),
                    "name_ja": normalize_missing(row.get("name_ja")),
                }

            elif current_table == "public.derived_maker":
                row = parse_copy_row(line, current_columns)
                makers[str(row["id"])] = {
                    "id": row["id"],
                    "name_en": normalize_missing(row.get("name_en")),
                    "name_ja": normalize_missing(row.get("name_ja")),
                }

            elif current_table == "public.derived_label":
                row = parse_copy_row(line, current_columns)
                labels[str(row["id"])] = {
                    "id": row["id"],
                    "name_en": normalize_missing(row.get("name_en")),
                    "name_ja": normalize_missing(row.get("name_ja")),
                }

            elif current_table == "public.derived_series":
                row = parse_copy_row(line, current_columns)
                series[str(row["id"])] = {
                    "id": row["id"],
                    "name_en": normalize_missing(row.get("name_en")),
                    "name_ja": normalize_missing(row.get("name_ja")),
                }

            elif current_table == "public.derived_video" and args.include_movies:
                if args.max_videos and counts["movies"] >= args.max_videos:
                    continue
                row = parse_copy_row(line, current_columns)
                movie = build_movie(row, makers, labels, series, now)
                movie_rows.append(movie["row"])
                selected_movie_rows.append((movie["content_id"], movie["movie_id"]))
                selected_content_ids.add(movie["content_id"])
                counts["movies"] += 1
                if len(movie_rows) >= args.batch_size:
                    flush_movies(conn, movie_rows, selected_movie_rows)
                    conn.commit()
                if counts["movies"] % 10000 == 0:
                    print(f"  movies: {counts['movies']}", flush=True)

            elif current_table == "public.derived_video_actress" and args.include_movies:
                row = parse_copy_row(line, current_columns)
                content_id = row.get("content_id")
                if content_id in selected_content_ids:
                    relation_actress_rows.append((content_id, row.get("actress_id")))
                    counts["video_actress_links"] += 1
                    if counts["video_actress_links"] >= relation_progress["video_actress_links"]:
                        print(f"  video actress links: {counts['video_actress_links']}", flush=True)
                        relation_progress["video_actress_links"] += 1000000
                if len(relation_actress_rows) >= args.batch_size:
                    flush_relations(conn, relation_actress_rows, relation_category_rows)
                    conn.commit()

            elif current_table == "public.derived_video_category" and args.include_movies:
                row = parse_copy_row(line, current_columns)
                content_id = row.get("content_id")
                if content_id in selected_content_ids:
                    relation_category_rows.append((content_id, row.get("category_id")))
                    counts["video_category_links"] += 1
                    if counts["video_category_links"] >= relation_progress["video_category_links"]:
                        print(f"  video category links: {counts['video_category_links']}", flush=True)
                        relation_progress["video_category_links"] += 1000000
                if len(relation_category_rows) >= args.batch_size:
                    flush_relations(conn, relation_actress_rows, relation_category_rows)
                    conn.commit()

        flush_actresses(conn, actress_rows, actress_name_rows)
        if args.include_movies:
            flush_movies(conn, movie_rows, selected_movie_rows)
            flush_relations(conn, relation_actress_rows, relation_category_rows)
        conn.commit()

        merged_movies = 0
        if args.include_movies:
            print("  merging movie relations...", flush=True)
            merged_movies = merge_movie_relations(conn, actress_cache, categories, args.batch_size)
    finally:
        stream.close()
        conn.close()

    elapsed = time.time() - started_at
    print("Done.", flush=True)
    for key, value in counts.items():
        print(f"{key}: {value}", flush=True)
    if args.include_movies:
        print(f"movies_with_merged_relations: {merged_movies}", flush=True)
    print(f"Elapsed: {elapsed:.1f}s", flush=True)
    print(f"Portable DB ready: {output}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user.", flush=True)
        sys.exit(130)
