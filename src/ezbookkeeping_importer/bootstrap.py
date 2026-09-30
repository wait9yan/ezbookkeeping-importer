from contextlib import ExitStack

from .adapters.llm.openai import AIClient
from .adapters.network import SERVICE_TIMEOUT_SECONDS
from .adapters.banks.cmb import BankParser
from .adapters.evidence_store import EvidenceStore
from .adapters.ezbookkeeping.client import EzBookkeepingClient
from .adapters.mail import MailClient
from .adapters.persistence.postgres import PostgresStore
from .config import Settings, validate_command
from .domain.errors import ImporterError


class Runtime:
    def __init__(
        self,
        settings: Settings,
        *,
        command: str = "run",
        action: str | None = None,
        account_id: str | None = None,
    ):
        self.settings = settings
        self.capabilities = validate_command(
            settings, command, action=action, account_id=account_id
        )
        self._ledger: EzBookkeepingClient | None = None
        self._parser: BankParser | None = None
        self._evidence: EvidenceStore | None = None
        self.ai: AIClient | None = None
        # Each constructed resource is registered immediately; a later constructor failure
        # closes everything already created before the exception reaches the CLI.
        with ExitStack() as resources:
            store_options = (
                {"create_database": True} if "create_database" in self.capabilities else {}
            )
            self.store = PostgresStore(settings.database_url.get_secret_value(), **store_options)
            resources.callback(self.store.close)
            if "ledger" in self.capabilities:
                self._ledger = EzBookkeepingClient(
                    settings.ledger_url,
                    settings.ledger_token.get_secret_value(),
                    timeout=SERVICE_TIMEOUT_SECONDS,
                )
                resources.callback(self._ledger.close)
            if "pipeline" in self.capabilities:
                self._parser = BankParser(
                    timezone=settings.timezone, context=settings.mail.source_id
                )
                if "ai" in self.capabilities:
                    assert settings.ai_url is not None and settings.ai_model is not None
                    self.ai = AIClient(
                        settings.ai_url,
                        settings.ai_token.get_secret_value(),
                        settings.ai_model,
                        timeout=SERVICE_TIMEOUT_SECONDS,
                    )
                    resources.callback(self.ai.close)
            if "evidence" in self.capabilities:
                self._evidence = EvidenceStore(settings.evidence_dir)
            self._resources = resources.pop_all()

    @property
    def optional_ledger(self) -> EzBookkeepingClient | None:
        return self._ledger

    @property
    def ledger(self) -> EzBookkeepingClient:
        if self._ledger is None:
            raise ImporterError("ledger capability is not configured for this command")
        return self._ledger

    @property
    def parser(self) -> BankParser:
        if self._parser is None:
            raise ImporterError("parser capability is not configured for this command")
        return self._parser

    @property
    def evidence(self) -> EvidenceStore:
        if self._evidence is None:
            raise ImporterError("evidence capability is not configured for this command")
        return self._evidence

    def mail(self):
        if "mail" not in self.capabilities:
            raise ImporterError("mail capability is not configured for this command")
        return MailClient(self.settings.mail)

    def close(self):
        self._resources.close()
