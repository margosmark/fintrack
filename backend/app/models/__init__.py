"""Model package — re-exports every model module so SQLModel.metadata and
alembic autogenerate see all tables (docs/01)."""

from app.models.acb_pools import AcbPool
from app.models.accounts import INVESTMENT_ACCOUNT_TYPES, Account, AccountType
from app.models.assistant import AssistantAudit, AssistantProposal, AuditAction, ProposalStatus
from app.models.auth_factors import OidcLink, RecoveryCode, TotpFactor, WebAuthnCredential
from app.models.backup import BackupArtifact
from app.models.benchmark_prices import BenchmarkPrice
from app.models.bill_reminders import BillReminder, BillReminderMatchTerm
from app.models.budgets import Budget, BudgetCarryover
from app.models.categories import Category
from app.models.debts import Debt
from app.models.dividend_history import (
    DividendHistory,
    DividendHistoryRefresh,
    DividendHistoryState,
)
from app.models.etf_holdings import EtfAssetClass, EtfHolding, EtfHoldingSource
from app.models.fundamentals import Fundamentals, FundamentalsSource
from app.models.fx_rates import FxRate, FxSource
from app.models.goals import Goal
from app.models.import_batches import ImportBatch, ImportBatchStatus
from app.models.institutions import Institution
from app.models.learned_payees import LearnedPayee
from app.models.llm_suggestions import LlmSuggestion, SuggestionStatus
from app.models.mcp_audit import McpAudit
from app.models.mcp_tokens import McpToken, McpTokenPolicy
from app.models.mixins import TimestampMixin
from app.models.notifications import (
    NotificationChannel,
    NotificationDelivery,
    NotificationDeliveryAttempt,
    NotificationInboxRead,
    NotificationSetting,
)
from app.models.prices import Price, PriceSource
from app.models.provider_registry import (
    ProviderFundFamily,
    ProviderFundRegistry,
    ProviderFundStatus,
)
from app.models.reconciliation import ReconciliationItem
from app.models.recurring_suppressions import RecurringSuppression
from app.models.refresh_tokens import RefreshToken
from app.models.rule_tags import RuleTag
from app.models.rules import Rule, RuleMatchType
from app.models.securities import QuoteOutcome, Security, SecurityKind
from app.models.sharing import SharingAudit, SharingLink, SharingLinkStatus
from app.models.spending_alerts import SpendingAlert
from app.models.sync_agreements import ProviderStatus, SyncAgreement, SyncAgreementStatus
from app.models.tags import Tag
from app.models.tax_projections import TaxProjection
from app.models.trades import Trade, TradeAction
from app.models.transaction_events import TransactionEvent, TransactionEventKind
from app.models.transaction_receipts import TransactionReceipt
from app.models.transaction_tags import TransactionTag
from app.models.transactions import RecurringCadence, Transaction
from app.models.users import LlmProvider, User
from app.models.vault import VaultEnvelope
from app.models.watchlist import WatchlistItem

__all__ = [
    "INVESTMENT_ACCOUNT_TYPES",
    "AcbPool",
    "Account",
    "AccountType",
    "AssistantAudit",
    "AssistantProposal",
    "AuditAction",
    "BackupArtifact",
    "BenchmarkPrice",
    "BillReminder",
    "BillReminderMatchTerm",
    "Budget",
    "BudgetCarryover",
    "Category",
    "Debt",
    "DividendHistory",
    "DividendHistoryRefresh",
    "DividendHistoryState",
    "EtfAssetClass",
    "EtfHolding",
    "EtfHoldingSource",
    "Fundamentals",
    "FundamentalsSource",
    "FxRate",
    "FxSource",
    "Goal",
    "ImportBatch",
    "ImportBatchStatus",
    "Institution",
    "LearnedPayee",
    "LlmProvider",
    "LlmSuggestion",
    "McpAudit",
    "McpToken",
    "McpTokenPolicy",
    "NotificationChannel",
    "NotificationDelivery",
    "NotificationDeliveryAttempt",
    "NotificationInboxRead",
    "NotificationSetting",
    "OidcLink",
    "Price",
    "PriceSource",
    "ProposalStatus",
    "ProviderFundFamily",
    "ProviderFundRegistry",
    "ProviderFundStatus",
    "ProviderStatus",
    "QuoteOutcome",
    "ReconciliationItem",
    "RecoveryCode",
    "RecurringCadence",
    "RecurringSuppression",
    "RefreshToken",
    "Rule",
    "RuleMatchType",
    "RuleTag",
    "Security",
    "SecurityKind",
    "SharingAudit",
    "SharingLink",
    "SharingLinkStatus",
    "SpendingAlert",
    "SuggestionStatus",
    "SyncAgreement",
    "SyncAgreementStatus",
    "Tag",
    "TaxProjection",
    "TimestampMixin",
    "TotpFactor",
    "Trade",
    "TradeAction",
    "Transaction",
    "TransactionEvent",
    "TransactionEventKind",
    "TransactionReceipt",
    "TransactionTag",
    "User",
    "VaultEnvelope",
    "WatchlistItem",
    "WebAuthnCredential",
]
