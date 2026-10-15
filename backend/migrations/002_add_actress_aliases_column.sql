-- 增加 actresses 表的 aliases (别名) 列
-- 适配 PostgreSQL 和 SQLite

-- 1. 为 PostgreSQL 增加列 (如果不存在)
DO $$ 
BEGIN 
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='actresses' AND column_name='aliases') THEN
        ALTER TABLE actresses ADD COLUMN aliases TEXT;
    END IF;
END $$;

-- 2. 为 SQLite 增加列 (由于 SQLite 不支持 DO 语法，直接执行 ALTER。
-- 如果在 SQLite 环境下运行此脚本，建议忽略错误或手动执行)
-- ALTER TABLE actresses ADD COLUMN aliases TEXT;
