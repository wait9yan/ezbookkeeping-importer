"""普通 wheel/sdist 构建也必须验证迁移与派生契约，不依赖数据库。"""

from pathlib import Path
import sys

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        source = str(Path(self.root) / 'src')
        sys.path.insert(0, source)
        try:
            from ezbookkeeping_importer.adapters.persistence.migrations import (
                load_migrations, migration_sources,
            )
            packaged = load_migrations()
            authoritative = migration_sources(Path(self.root) / 'migrations')
            if len(packaged) != len(authoritative) or any(
                item.checksum != original[3]
                for item, original in zip(packaged, authoritative, strict=True)
            ):
                raise ValueError('打包迁移与权威 SQL 不一致')
        finally:
            sys.path.remove(source)
