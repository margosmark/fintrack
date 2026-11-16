package com.margosmark.fintracker.screens

import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.junit4.ComposeContentTestRule
import com.margosmark.fintracker.TestStack
import com.margosmark.fintracker.finance.FinanceRepository
import com.margosmark.fintracker.navigation.DeepLinkTarget
import com.margosmark.fintracker.session.AuthState
import com.margosmark.fintracker.tokenResponseBody
import com.margosmark.fintracker.ui.AppScope
import com.margosmark.fintracker.ui.RepositorySections
import com.margosmark.fintracker.ui.SessionDestinationPreserver
import com.margosmark.fintracker.ui.masking.MaskController
import com.margosmark.fintracker.ui.masking.MaskReason
import com.margosmark.fintracker.ui.screens.SignInActions
import java.io.IOException
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.flow.MutableStateFlow
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.RecordedRequest

/**
 * QA round-2 support (t_e86830d2), @qa. Round-1 probes for AC1.3 / AC3.2 are
 * deliberately NOT reused — the wiring under test changed on 09b656ef. These
 * helpers build an AppScope that mirrors productionScope()'s assembly
 * (ui/AppScope.kt) with only the server faked (MockWebServer), per docs/53
 * (probes: local synthetic stack only):
 *
 *  - sections      → RepositorySections over FinanceRepository (production type)
 *  - auth          → StateFlow fed by the REAL AuthSessionManager's listener
 *  - destinations  → SessionDestinationPreserver(session) — the production preserver
 *  - signInActions → executor → session.login — the production call path
 *  - targetExists  → same shape as productionScope's repository-backed lambda,
 *                    injectable when a probe must control the gate's timing.
 */
fun qaProductionShapedScope(
    stack: TestStack,
    authFlow: MutableStateFlow<AuthState>,
    targetExistsOverride: ((DeepLinkTarget) -> Boolean)? = null,
): AppScope {
    val repository = FinanceRepository(stack.client)
    val mask = MaskController(initiallyMasked = true)
    val notice = MutableStateFlow<String?>(null)
    val destinations = SessionDestinationPreserver(stack.session)
    val executor = Executors.newCachedThreadPool { r -> Thread(r, "qa-r2-scope").apply { isDaemon = true } }
    val signInActions = object : SignInActions {
        override fun signIn(email: String, password: String, onFailure: (String) -> Unit) {
            executor.execute {
                try {
                    stack.session.login(email, password)
                } catch (e: Throwable) {
                    onFailure("qa: sign-in failed: ${e::class.simpleName}")
                }
            }
        }

        override fun submitTotp(code: String, onFailure: (String) -> Unit) = onFailure("qa: TOTP not exercised")

        override fun exploreDemo() {
            executor.execute { runCatching { stack.session.loginDemo() } }
        }
    }
    return AppScope(
        sections = RepositorySections(repository),
        auth = authFlow,
        mask = mask,
        signInActions = signInActions,
        destinations = destinations,
        onSignOut = {
            mask.mask(MaskReason.LOGOUT)
            destinations.explicitSignOut()
            executor.execute { runCatching { stack.session.logout() } }
        },
        logoutNotice = notice,
        targetExists = targetExistsOverride ?: { target -> qaProductionTargetExists(repository, target) },
    )
}

/**
 * The same shape as productionScope()'s targetExists lambda (ui/AppScope.kt):
 * non-numeric ids are false without a network call; extant + tenant-scoped
 * lookups answer via the session-scoped repository (the server's
 * owned_or_404 makes foreign == deleted); IOException fails OPEN; any other
 * server-confirmed error answers false.
 */
fun qaProductionTargetExists(repository: FinanceRepository, target: DeepLinkTarget): Boolean = when (target) {
    is DeepLinkTarget.Account -> {
        val id = target.accountId.toIntOrNull()
        if (id == null) {
            false
        } else {
            try {
                repository.account(id)
                true
            } catch (_: IOException) {
                true
            } catch (_: Exception) {
                false
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
                repository.positions(accountId).items.any { it.securityId == holdingId }
            } catch (_: IOException) {
                true
            } catch (_: Exception) {
                false
            }
        }
    }
    else -> true
}

/**
 * Path-routed fake backend for the deep-link probes. Accounts are served per
 * numeric id (5 renders "ACCOUNT-FIVE"), unknown numeric ids render
 * "ACCOUNT-<id>", [notFoundIds] answer the owned_or_404 404 shape, and
 * [expireMode] simulates a provably-dead session (domain 401 + rejected
 * refresh). Every path it sees is recorded in [paths].
 */
class QaRound2Dispatcher : Dispatcher() {
    @Volatile
    var expireMode = false

    @Volatile
    var notFoundIds: Set<Int> = emptySet()

    /** Hold the account-detail existence response open for this long (ordering probes). */
    @Volatile
    var delayAccountMs = 0L

    val paths = CopyOnWriteArrayList<String>()

    private val accountDetail = Regex("/accounts/([0-9]+)\\z")

    override fun dispatch(request: RecordedRequest): MockResponse {
        val url = request.requestUrl
        val path = url?.encodedPath ?: ""
        paths += (url?.let { it.encodedPath + it.query.orEmpty().let { q -> if (q.isEmpty()) "" else "?$q" } } ?: path)

        if (expireMode) {
            return if (path.contains("/auth/refresh")) {
                qaErr(401, "Invalid or revoked refresh token", "invalid_or_revoked_refresh_token")
            } else {
                qaErr(401, "Could not validate credentials", "invalid_token")
            }
        }
        if (path.endsWith("/auth/login")) {
            return MockResponse()
                .setResponseCode(200)
                .setHeader("Set-Cookie", "refresh_token=rt-qa-1; Path=/api/v1/auth; HttpOnly")
                .setHeader("Content-Type", "application/json")
                .setBody(tokenResponseBody("access-qa-1", 1, "a@example.com"))
        }
        if (path.endsWith("/auth/logout")) return MockResponse().setResponseCode(200)
        if (path.endsWith("/auth/refresh")) {
            return qaErr(401, "Invalid or revoked refresh token", "invalid_or_revoked_refresh_token")
        }
        if (path.endsWith("/accounts/ledger")) {
            val id = url?.queryParameter("account_id") ?: "0"
            return qaOk(qaLedgerJson(id))
        }
        val account = accountDetail.find(path)
        if (account != null) {
            val id = account.groupValues[1].toInt()
            val response = if (id in notFoundIds) {
                qaErr(404, "Not found", "not_found")
            } else {
                val name = when (id) {
                    5 -> "ACCOUNT-FIVE"
                    0 -> "ACCOUNT-ZERO"
                    else -> "ACCOUNT-$id"
                }
                qaOk(qaAccountJson(id, name))
            }
            if (delayAccountMs > 0) {
                response.setHeadersDelay(delayAccountMs, java.util.concurrent.TimeUnit.MILLISECONDS)
            }
            return response
        }
        val body = when {
            path.contains("/portfolio/positions") -> Fixtures.POSITIONS_JSON
            path.contains("/fx/status") -> Fixtures.FX_JSON
            path.contains("/transactions/7/events") -> Fixtures.EVENTS_JSON
            path.contains("/transactions") -> Fixtures.TRANSACTIONS_JSON
            path.contains("/accounts") -> Fixtures.ACCOUNTS_JSON
            else -> "{}"
        }
        return MockResponse()
            .setResponseCode(200)
            .setHeader("Content-Type", "application/json")
            .setBody(body)
    }
}

fun qaOk(body: String): MockResponse = MockResponse()
    .setResponseCode(200)
    .setHeader("Content-Type", "application/json")
    .setBody(body)

fun qaErr(status: Int, detail: String, code: String): MockResponse = MockResponse()
    .setResponseCode(status)
    .setHeader("Content-Type", "application/json")
    .setBody("""{"detail":"$detail","code":"$code"}""")

/** Account row JSON in the backend's exact shape (docs/08 / FinanceJson). */
fun qaAccountJson(id: Int, name: String): String =
    """{"id":$id,"name":"$name","type":"tfsa","currency":"CAD","cash_balance":"10.00",""" +
        """"investment_value":"100.00","total_value":"110.00","starting_balance":"5.00","last_sync_at":null,""" +
        """"provider_status":null,"pending_import_count":0}"""

/** Ledger page JSON; the row description names the account so a wrong-id render is visible. */
fun qaLedgerJson(accountId: String): String =
    """{"items":[{"transaction_id":7,"date":"2026-10-01","description":"LEDGER-$accountId","amount":"-4.50",""" +
        """"running_balance":"105.50"}],"starting_balance":"110.00","current_balance":"105.50","total":1}"""

/** Every text string AND content description in the semantics tree — the TalkBack-visible corpus. */
fun ComposeContentTestRule.qaCorpus(): List<String> {
    val out = mutableListOf<String>()
    val withText = SemanticsMatcher.keyIsDefined(SemanticsProperties.Text)
    val withCd = SemanticsMatcher.keyIsDefined(SemanticsProperties.ContentDescription)
    (onAllNodes(withText, useUnmergedTree = true).fetchSemanticsNodes() +
        onAllNodes(withCd, useUnmergedTree = true).fetchSemanticsNodes())
        .forEach { node ->
            if (SemanticsProperties.Text in node.config) {
                node.config[SemanticsProperties.Text].forEach { out += it.text }
            }
            if (SemanticsProperties.ContentDescription in node.config) {
                node.config[SemanticsProperties.ContentDescription].forEach { out += it }
            }
        }
    return out
}

/** Thread-safe "the gate actually ran" signal for the blocking-gate probes. */
class GateSignals {
    val started = AtomicInteger(0)
    val release = java.util.concurrent.CountDownLatch(1)
    val blockedGate: (DeepLinkTarget) -> Boolean = {
        started.incrementAndGet()
        if (!release.await(20, java.util.concurrent.TimeUnit.SECONDS)) {
            throw AssertionError("qa: the existence gate was never released")
        }
        false
    }
}
