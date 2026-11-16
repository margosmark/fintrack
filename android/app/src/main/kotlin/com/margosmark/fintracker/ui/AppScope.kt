package com.margosmark.fintracker.ui

import android.content.Context
import com.margosmark.fintracker.finance.AccountRow
import com.margosmark.fintracker.finance.AccountsPage
import com.margosmark.fintracker.finance.FinanceRepository
import com.margosmark.fintracker.finance.LedgerPage
import com.margosmark.fintracker.finance.PositionRow
import com.margosmark.fintracker.finance.PositionsPage
import com.margosmark.fintracker.finance.TransactionEventsPage
import com.margosmark.fintracker.finance.TransactionsPage
import com.margosmark.fintracker.http.ApiException
import com.margosmark.fintracker.navigation.DeepLinkTarget
import com.margosmark.fintracker.session.AppContainer
import com.margosmark.fintracker.session.AuthSessionManager
import com.margosmark.fintracker.session.AuthState
import com.margosmark.fintracker.session.LogoutOutcome
import com.margosmark.fintracker.ui.masking.MaskController
import com.margosmark.fintracker.ui.masking.MaskReason
import com.margosmark.fintracker.ui.screens.PortfolioSection
import com.margosmark.fintracker.ui.screens.SectionPresenter
import com.margosmark.fintracker.ui.screens.SignInActions
import com.margosmark.fintracker.ui.screens.accountsSectionPresenter
import com.margosmark.fintracker.ui.screens.accountDetailPresenter
import com.margosmark.fintracker.ui.screens.accountHoldingsPresenter
import com.margosmark.fintracker.ui.screens.accountLedgerPresenter
import com.margosmark.fintracker.ui.screens.accountTransactionsPresenter
import com.margosmark.fintracker.ui.screens.eventsPresenter
import com.margosmark.fintracker.ui.screens.holdingDetailPresenter
import com.margosmark.fintracker.ui.screens.portfolioSectionPresenter
import com.margosmark.fintracker.ui.screens.transactionsSectionPresenter
import java.io.IOException as JvmIOException
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

/**
 * Where each screen gets its [SectionPresenter]. An interface so the nav
 * graph can be driven by fakes (no sockets in nav tests) while production
 * binds every method straight to the read-only repository.
 */
interface SectionSources {
    fun portfolio(): SectionPresenter<PortfolioSection>
    fun accounts(): SectionPresenter<AccountsPage>
    fun accountsList(): SectionPresenter<AccountsPage>
    fun transactions(): SectionPresenter<TransactionsPage>
    fun account(accountId: Int): SectionPresenter<AccountRow>
    fun ledger(accountId: Int): SectionPresenter<LedgerPage>
    /** Account-scoped positions (`GET /portfolio/positions?account_id=…`). */
    fun holdings(accountId: Int): SectionPresenter<PositionsPage>
    fun holding(accountId: Int, securityId: Int): SectionPresenter<PositionRow>
    fun events(transactionId: Int): SectionPresenter<TransactionEventsPage>
    /**
     * ONE "Show N more" page of the account ledger — `GET /transactions
     * ?limit=&offset=&account_id=` (docs/52 §3; no new API). Same
     * account-scoped, split-excluded, committed-only population as
     * `/accounts/{id}/ledger`, ordered `date desc, id desc` when no search
     * runs, so the caller's `offset` is counted from the END of the
     * ascending ledger the screen is showing.
     */
    fun accountTransactions(accountId: Int, limit: Int, offset: Int): SectionPresenter<TransactionsPage>
}

/** Production: every section loads through [FinanceRepository] (GET-only). */
class RepositorySections(private val repository: FinanceRepository) : SectionSources {
    override fun portfolio() = portfolioSectionPresenter(repository)
    override fun accounts() = accountsSectionPresenter(repository)
    override fun accountsList() = accountsSectionPresenter(repository)
    override fun transactions() = transactionsSectionPresenter(repository)
    override fun account(accountId: Int) = accountDetailPresenter(repository, accountId)
    override fun ledger(accountId: Int) = accountLedgerPresenter(repository, accountId)
    override fun holdings(accountId: Int) = accountHoldingsPresenter(repository, accountId)
    override fun holding(accountId: Int, securityId: Int) =
        holdingDetailPresenter(repository, accountId, securityId)
    override fun events(transactionId: Int) = eventsPresenter(repository, transactionId)
    override fun accountTransactions(accountId: Int, limit: Int, offset: Int) =
        accountTransactionsPresenter(repository, accountId, limit, offset)
}

/**
 * Intended-destination preserve/consume (docs/53 AC1.3). An EXPLICIT
 * sign-out drops the destination — [explicitSignOut] arms that, and the
 * next [consume] disarms it.
 */
interface DestinationPreserver {
    val pending: String?
    fun note(route: String)
    fun consume(): String?
    fun explicitSignOut()
}

class InMemoryDestinationPreserver : DestinationPreserver {
    @Volatile
    override var pending: String? = null
        private set

    @Volatile
    private var signedOutExplicitly = false

    override fun note(route: String) {
        if (!signedOutExplicitly) pending = route
    }

    override fun consume(): String? {
        signedOutExplicitly = false
        return pending.also { pending = null }
    }

    override fun explicitSignOut() {
        signedOutExplicitly = true
        pending = null
    }
}

/** Production: the auth session manager's own preserve/consume seam. */
class SessionDestinationPreserver(private val session: AuthSessionManager) : DestinationPreserver {
    @Volatile
    private var signedOutExplicitly = false

    override val pending: String?
        get() = session.pendingDestination

    override fun note(route: String) {
        if (signedOutExplicitly) return
        try {
            session.noteDestination(route)
        } catch (_: IllegalArgumentException) {
            // Not an internal route (defensive: nav routes always are).
        }
    }

    override fun consume(): String? {
        signedOutExplicitly = false
        return session.consumePendingDestination()
    }

    override fun explicitSignOut() {
        signedOutExplicitly = true
    }
}

/**
 * Everything the navigation graph needs, assembled ONCE at the activity
 * seam. Production builds it with [productionScope]; tests build it from
 * fakes (see `UiTestSupport`) without touching sockets or Keystore.
 */
class AppScope(
    val sections: SectionSources,
    /** Single source of session truth — the auth session manager's listener feeds it. */
    val auth: StateFlow<AuthState>,
    val mask: MaskController,
    val signInActions: SignInActions,
    val destinations: DestinationPreserver,
    /** Re-masks FIRST (AC2.3), then runs the blocking logout sequence off the main thread. */
    val onSignOut: () -> Unit,
    /** Non-null = the honest offline-logout disclosure to show on sign-in. */
    val logoutNotice: StateFlow<String?> = MutableStateFlow(null),
    /** Tenant-scoped existence check for id-route deep links (docs/53 AC3.2). */
    val targetExists: (DeepLinkTarget) -> Boolean = { true },
)

/**
 * Production API base (docs/54 §1 S5): the deployed HTTPS stack's API root,
 * injected at the wiring seam on purpose. Never a default on [AppContainer]
 * (security M4: no build-config API base field of any kind) and
 * never a test default — tests use the emulator/dev alias or their own
 * MockWebServer URLs.
 */
const val PRODUCTION_API_BASE_URL = "https://fintrac.princessandmark.com/api/v1"

/**
 * Production wiring (docs/52 Option A + docs/54):
 *
 *  - [AppContainer] builds the auth/http/session stack the parent card
 *    delivered. Its own `session` manager has no state listener (constructor
 *    default), so the UI binds a SECOND [AuthSessionManager] over the SAME
 *    authApi/apiClient/purgeRegistry WITH a listener into [authFlow]; its
 *    `init` rebinds `ApiClient.sessionExpiredListener` to this instance —
 *    the one the UI drives. The container's manager stays unused. (Editing
 *    AppContainer is the auth card's file; this consumes it as delivered.)
 *  - Base URL: production wiring passes [PRODUCTION_API_BASE_URL] EXPLICITLY
 *    (docs/54 §1 S5 — the native client targets the production HTTPS endpoint;
 *    `ApiClient`'s startup `require()` stays the fail-closed enforcement point
 *    and is untouched). [AppContainer]'s constructor default remains the
 *    emulator/dev alias, so no test or gate build can receive a production
 *    host by omission — and build.gradle.kts still declares no build-config
 *    base-URL field (security M4).
 *  - Boot restore runs once, off the main thread: purge + single-flight
 *    refresh. Masking is already ON (nothing to leak before it lands).
 */
fun productionScope(context: Context): AppScope {
    val appContext = context.applicationContext
    val container = AppContainer(appContext, baseUrl = PRODUCTION_API_BASE_URL)

    val authFlow = MutableStateFlow<AuthState>(AuthState.SignedOut)
    val session = AuthSessionManager(
        authApi = container.authApi,
        client = container.apiClient,
        purgeRegistry = container.purgeRegistry,
        listener = { authFlow.value = it },
    )

    val mask = MaskController(initiallyMasked = true)
    val repository = FinanceRepository(container.apiClient)
    val notice = MutableStateFlow<String?>(null)
    val destinations = SessionDestinationPreserver(session)

    val executor: ExecutorService = Executors.newCachedThreadPool { runnable ->
        Thread(runnable, "app-scope").apply { isDaemon = true }
    }

    val signInActions = sessionSignInActions(session, executor)

    val scope = AppScope(
        sections = RepositorySections(repository),
        auth = authFlow,
        mask = mask,
        signInActions = signInActions,
        destinations = destinations,
        onSignOut = {
            // Re-mask BEFORE the network call so the switcher snapshot can
            // never contain the last revealed frame (AC2.3).
            mask.mask(MaskReason.LOGOUT)
            destinations.explicitSignOut()
            executor.execute {
                try {
                    val outcome = session.logout()
                    notice.value = if (outcome == LogoutOutcome.REVOCATION_UNCONFIRMED) {
                        "Signed out on this device. The server could not confirm sign-out (offline)."
                    } else {
                        null
                    }
                } catch (_: Throwable) {
                    // Belt and braces: logoutSequence clears unconditionally,
                    // but an unexpected error must not kill the thread silently (security M1).
                    notice.value = "Signed out on this device. The server could not confirm sign-out."
                }
            }
        },
        logoutNotice = notice,
        targetExists = { target ->
            when (target) {
                is DeepLinkTarget.Account -> {
                    val id = target.accountId.toIntOrNull()
                    if (id == null) {
                        false
                    } else {
                        try {
                            repository.account(id)
                            true
                        } catch (_: JvmIOException) {
                            true // Can't verify; let the screen fail naturally
                        } catch (_: Exception) {
                            false // Server confirmed error (404, 403, etc.)
                        }
                    }
                }
                is DeepLinkTarget.Holding -> {
                    val accountId = target.accountId.toIntOrNull()
                    val holdingId = target.holdingId.toIntOrNull()
                    if (accountId == null || holdingId == null) {
                        false
                    } else {
                        try {
                            val page = repository.positions(accountId)
                            page.items.any { it.securityId == holdingId }
                        } catch (_: JvmIOException) {
                            true
                        } catch (_: Exception) {
                            false
                        }
                    }
                }
                else -> true
            }
        },
    )

    // Boot restore: purge re-runs, then a single refresh. SignedOut stays
    // SignedOut on reject/unreachable — the sign-in screen simply shows.
    executor.execute {
        try {
            session.restoreSession()
        } catch (_: Throwable) {
            // Fail closed: stay signed out.
        }
    }

    return scope
}

/**
 * The PRODUCTION [SignInActions] over a real [AuthSessionManager], defined
 * once so the wiring the sign-in flow tests drive is literally the code the
 * app runs (t_696bc09d), not a copy of it.
 *
 * Contract each method holds:
 *
 *  - every call hops to [executor] and returns immediately — the screen owns
 *    its in-flight state for the whole round trip;
 *  - FAILURE answers exactly once through `onFailure` with a generic,
 *    honest message ([signInFailureMessage]; docs/48 §1.4);
 *  - SUCCESS never calls back: a password attempt answers as an
 *    [AuthState.TotpRequired] (or [AuthState.Authenticated]) through the
 *    session LISTENER, and the second factor as [AuthState.Authenticated].
 *    The screen releases its in-flight state by observing that state change
 *    (`SignInScreen` keys it to the auth state captured at click time).
 */
fun sessionSignInActions(
    session: AuthSessionManager,
    executor: ExecutorService,
): SignInActions = object : SignInActions {
    override fun signIn(email: String, password: String, onFailure: (String) -> Unit) {
        executor.execute {
            try {
                // TotpChallenge / Authenticated both land through the
                // session listener; nothing else to do here.
                session.login(email, password)
            } catch (e: Throwable) {
                onFailure(signInFailureMessage(e))
            }
        }
    }

    override fun submitTotp(code: String, onFailure: (String) -> Unit) {
        executor.execute {
            try {
                session.loginWithTotp(code)
            } catch (e: Throwable) {
                onFailure(signInFailureMessage(e))
            }
        }
    }

    override fun exploreDemo() {
        executor.execute {
            try {
                session.loginDemo()
            } catch (_: Throwable) {
                // Demo failure surfaces through auth state; keep it calm.
            }
        }
    }
}

/**
 * Sign-in failure copy: generic by contract (docs/48 §1.4 — never confirm
 * whether an account exists; never echo server detail).
 */
fun signInFailureMessage(error: Throwable): String = when (error) {
    is ApiException -> when (error.code) {
        "invalid_credentials", "invalid_email_or_password" ->
            "Email or password is incorrect."
        "invalid_totp_code" ->
            "That code didn't work. Check your authenticator app and try again."
        "rate_limited", "too_many_attempts" ->
            "Too many attempts. Wait a moment and try again."
        else -> "Sign-in failed. Please try again."
    }
    is JvmIOException -> "Couldn't reach FinTracker. Check your connection and try again."
    else -> "Sign-in failed. Please try again."
}
