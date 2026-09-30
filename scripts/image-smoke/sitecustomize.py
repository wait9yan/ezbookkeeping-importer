"""仅用于隔离镜像验收：空邮箱及确定性阻塞点，不修改业务状态机。"""
import time
from pathlib import Path
from unittest.mock import patch

from ezbookkeeping_importer.adapters.ezbookkeeping.client import EzBookkeepingClient


def pause_at(name):
    gate = Path('/app/data', 'pause-' + name)
    if gate.exists():
        Path('/app/data', 'blocked-' + name).touch()
        while gate.exists():
            time.sleep(0.05)


class EmptyMailbox:
    def __init__(self, settings):
        pass

    def folders(self):
        pause_at('collection')
        Path('/app/data/mail-fixture-used').touch()
        return []

    def close(self):
        pass


original_create = EzBookkeepingClient.create


def controlled_create(self, payload):
    pause_at('before-send')
    return original_create(self, payload)


patch('ezbookkeeping_importer.bootstrap.MailClient', EmptyMailbox).start()
patch.object(EzBookkeepingClient, 'create', controlled_create).start()
