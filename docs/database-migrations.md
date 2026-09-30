# 数据库结构变更与发布

本说明适用于当前源码中的版本化迁移机制，是否已进入正式镜像以 Release 为准。

## 添加结构变更

已发布的 `migrations/001_initial.sql` 和后续迁移保持字节不变。新增递增编号文件，例如现有002之后添加 `003_add_source_type.sql`；不要同时维护一份独立的“最新建表 SQL”。同时在包内 `src/ezbookkeeping_importer/adapters/persistence/migration_sql/` 添加指向该权威文件的同名符号链接，沿用现有相对链接路径；不能复制成独立 SQL。构建门禁会拒绝根目录与包内链不一致。脚本只描述该版本增量 DDL 和必要数据转换，迁移引擎负责新增版本的历史记录。

每个版本由引擎放在独立事务中执行，不在脚本内 BEGIN/COMMIT/ROLLBACK，不在默认自动迁移中使用非事务操作、外部服务调用或大规模回填。删列、不可逆转换、长期锁表等变更先设计维护与数据恢复方案。添加非空字段时必须明确现有行如何获得有效值，不能依靠新库测试推断旧库可升级。

## 生成与验证

准备显式隔离 PostgreSQL 测试实例，在进程环境提供合成的 `EBKI_TEST_DATABASE_URL`；不要使用生产库或真实 `.env`。生成脚本在随机 schema 中依次执行完整迁移链，事务回滚清理：

```sh
uv run python scripts/generate-schema-contracts.py
uv run python scripts/generate-schema-contracts.py --check
uv run python scripts/generate-schema-contracts.py --resources-only
```

每个版本的结构签名和脚本 SHA256 随包提供。SQL 是手工变更源，生成结果不能手改掩盖结构变化；SQL修改后未同步资源、已发布脚本改写或真实数据库再生成结果漂移必须让门禁失败。在 PostgreSQL 17/18 的 Bookworm、Alpine 镜像分别验证签名一致性。

共享提取器在命名空间规范化后统一对象集合顺序，保留重复数量和每表列顺序，避免数据库 locale 导致误报。仅修复签名表示时重新生成契约及其摘要，不改已发布 SQL 或脚本摘要，也不新增数据库迁移。

验收需包含旧库带数据升级、新库执行完整链结果一致、跨版本升级、失败版本事务回滚/再启动、低权限最新库和权限不足时明确拒绝。保持已有采集、查重、入账及UNKNOWN恢复测试，不能只检查表存在。

## 部署与回退

部署权限、迁移历史校验、每版事务和故障恢复见[数据库初始化与版本迁移](operations.md#数据库初始化与版本迁移)；停旧 worker、配对备份与应用回退条件见[镜像升级与回退](operations.md#镜像升级与回退)。
