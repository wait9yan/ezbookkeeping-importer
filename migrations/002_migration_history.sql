-- 首次升级保留已发布 v1 的时间和业务数据；基线摘要由已验证的迁移链传入。
ALTER TABLE schema_version ADD COLUMN script_sha256 text NOT NULL
 DEFAULT current_setting('ebki.migration_baseline_sha256');
ALTER TABLE schema_version ALTER COLUMN script_sha256 DROP DEFAULT;
ALTER TABLE schema_version ADD CONSTRAINT schema_version_script_sha256_check
 CHECK(script_sha256 ~ '^[a-f0-9]{64}$');
COMMENT ON COLUMN schema_version.script_sha256 IS '迁移脚本摘要';
