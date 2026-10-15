-- ============================================================================
-- BusTerminal movies 去重整改脚本 (2026-07-25)  [rev2: 修复 movie_actresses 合并 PK 冲突]
-- 方案: 物理删除 + 合并关联 (用户确认)
--   * 重复判定: 同 detail_url (R18 规范影片 ID) 的多行
--   * 保留规则: 每组保留 id 字典序最小 (标准番号优先, 如 ANPD-01 > ANPD001)
--   * 关联合并: 仅 movie_actresses 引用了重复行 (27803 行), 其余表 0 引用
--   * 空 URL: 86 条 detail_url 为空, 按用户确认直接删除
-- 前置: 已执行 movies_backup_20260725 / movie_actresses_backup_20260725 全量备份
-- ============================================================================

BEGIN;

-- 1) 物化重复映射: keep_id = 组内 MIN(id), dup_id = 其余行
CREATE TABLE dup_map (
    keep_id text NOT NULL,
    dup_id  text NOT NULL PRIMARY KEY
);

INSERT INTO dup_map (keep_id, dup_id)
SELECT g.keep_id, m.id
FROM (
    SELECT detail_url, min(id) AS keep_id
    FROM movies
    WHERE detail_url IS NOT NULL AND detail_url <> ''
    GROUP BY detail_url
    HAVING count(*) > 1
) g
JOIN movies m
  ON m.detail_url = g.detail_url AND m.id <> g.keep_id;

-- 2) movie_actresses: 处理冲突后再改指向
--    对每 (keep_id, actress_id):
--      * 若 keep 行已含该 actress      -> 所有 dup 行删 (信息已由 keep 行保留)
--      * 否则(keep 无该 actress)       -> 仅保留一个 dup 行(rn=1), 其余删 (避免 UPDATE 后互相 PK 冲突)
WITH dup_rows AS (
    SELECT m.keep_id,
           ma.movie_id,
           ma.actress_id,
           (EXISTS (SELECT 1 FROM movie_actresses k
                      WHERE k.movie_id = m.keep_id AND k.actress_id = ma.actress_id)) AS in_keep,
           row_number() OVER (PARTITION BY m.keep_id, ma.actress_id ORDER BY ma.movie_id) AS rn
    FROM movie_actresses ma
    JOIN dup_map m ON ma.movie_id = m.dup_id
)
DELETE FROM movie_actresses d
USING dup_rows r
WHERE d.movie_id = r.movie_id AND d.actress_id = r.actress_id
  AND (r.in_keep OR r.rn > 1);

-- 3) 其余 dup 行改指向 keep_id (此时无 PK 冲突)
UPDATE movie_actresses d
SET movie_id = m.keep_id
FROM dup_map m
WHERE d.movie_id = m.dup_id;

-- 4) 删除 movies 重复行
DELETE FROM movies WHERE id IN (SELECT dup_id FROM dup_map);

-- 5) 删除 86 条 detail_url 为空的记录 (用户确认直接删除, 备份已保留)
DELETE FROM movies WHERE detail_url IS NULL OR detail_url = '';

DROP TABLE dup_map;

COMMIT;

-- ===================== 验证 =====================
SELECT 'after_movies_total'        AS check_name, count(*) AS val FROM movies;
SELECT 'remaining_dup_url_groups'  AS check_name, count(*) AS val
  FROM (SELECT detail_url FROM movies WHERE detail_url <> '' GROUP BY detail_url HAVING count(*) > 1) t;
SELECT 'orphan_movie_actresses'    AS check_name, count(*) AS val
  FROM movie_actresses ma WHERE NOT EXISTS (SELECT 1 FROM movies m WHERE m.id = ma.movie_id);
SELECT 'empty_url_remaining'       AS check_name, count(*) AS val
  FROM movies WHERE detail_url IS NULL OR detail_url = '';
SELECT 'movie_actresses_after'     AS check_name, count(*) AS val FROM movie_actresses;
