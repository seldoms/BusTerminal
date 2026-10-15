-- 迁移脚本: 为actresses表添加bio和wiki_url列
-- 日期: 2026-01-11
-- 说明: 添加女优传记和维基百科链接字段

-- 添加bio列 (如果不存在)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name='actresses' AND column_name='bio'
    ) THEN
        ALTER TABLE actresses ADD COLUMN bio TEXT;
    END IF;
END $$;

-- 添加wiki_url列 (如果不存在)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name='actresses' AND column_name='wiki_url'
    ) THEN
        ALTER TABLE actresses ADD COLUMN wiki_url TEXT;
    END IF;
END $$;
