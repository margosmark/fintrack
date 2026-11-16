package com.margosmark.fintracker.navigation

import android.os.Bundle
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.AccountBalance
import androidx.compose.material.icons.filled.Home
import androidx.compose.material.icons.filled.ReceiptLong
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import androidx.navigation.NavHostController
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import androidx.navigation.navArgument
import com.margosmark.fintracker.session.AuthState
import com.margosmark.fintracker.state.ScreenState
import com.margosmark.fintracker.state.StatePane
import com.margosmark.fintracker.ui.AppScope
import com.margosmark.fintracker.ui.masking.MaskAuthEffects
import com.margosmark.fintracker.ui.masking.MaskLifecycleObserver
import com.margosmark.fintracker.ui.screens.AccountDetailScreen
import com.margosmark.fintracker.ui.screens.AccountsScreen
import com.margosmark.fintracker.ui.screens.AppBarMaskToggle
import com.margosmark.fintracker.ui.screens.DemoBadge
import com.margosmark.fintracker.ui.screens.GlanceScreen
import com.margosmark.fintracker.ui.screens.HoldingDetailScreen
import com.margosmark.fintracker.ui.screens.SettingsScreen
import com.margosmark.fintracker.ui.screens.SignInScreen
import com.margosmark.fintracker.ui.screens.TransactionsScreen
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/** Stable tags so tests can assert which destination is on screen. */
object ScreenTags {
    const val SIGN_IN = "screen_sign_in"
    const val GLANCE = "screen_glance"
    const val ACCOUNTS = "screen_accounts"
    const val ACCOUNT_DETAIL = "screen_account_detail"
    const val HOLDING_DETAIL = "screen_holding_detail"
    const val TRANSACTIONS = "screen_transactions"
    const val SETTINGS = "screen_settings"
    const val UNAVAILABLE = "screen_unavailable"
}

/** Stable tags for the shared top app bar's conditional actions. */
object AppBarTags {
    /** The 48dp back action — present on pushed detail destinations only. */
    const val BACK = "app_bar_back"
}

/**
 * The v1 navigation graph.
 *
 * Deep links are NOT wired through NavHost's auto-attributes; they resolve
 * through [resolveDeepLinkUri] FIRST (auth + shape gate; existence and
 * tenant scoping for id routes are enforced SERVER-side by the
 * tenant-scoped `owned_or_404` on load — a stale id renders the honest
 * NotFound state) and fold into the graph's start destination. Navigation
 * therefore only ever sees an already-approved route, and the security gate
 * lives in one testable function.
 *
 * Session-driven navigation (docs/53 AC1.3; security S1):
 *  - session ends → note the current FILLED route (pattern + resolved
 *    arguments, never the bare pattern) as the intended destination (unless
 *    the sign-out was explicit — that drops it) and land on sign-in;
 *  - session returns → consume the destination once and revalidate by
 *    loading it fresh; nothing is trusted from before the session. A
 *    preserved destination that CAME FROM the launch deep link (an id
 *    target) is owned by the existence/tenant gate: while no verdict
 *    exists yet the consume-navigation hands off to that gate, and once a
 *    verdict exists the verdict IS the navigation — no id destination
 *    renders before the gate's answer (AC3.2, QA F2'). A verdict belongs
 *    to the session that consumed it: session end invalidates it, and a
 *    SAME-identity re-auth whose preserved destination is the link's own
 *    route re-runs the gate (fresh verdict, fresh fetch) instead of
 *    swallowing the entry (AC1.3 Consume, QA R3-1); an identity change
 *    never re-validates the old identity's link verdict (S1). A
 *    destination from the user's own browsing is returned to directly;
 *  - an identity CHANGE at commit hard-clears the preserved destination
 *    (AuthSessionManager) — one identity never consumes another's route.
 *
 * [deepLinkUri] is the launch intent's data, if any.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun FinTrackerApp(
    scope: AppScope,
    targetExists: (DeepLinkTarget) -> Boolean = scope.targetExists,
    deepLinkUri: String? = null,
    navController: NavHostController = rememberNavController(),
) {
    val auth by scope.auth.collectAsState()
    val logoutNotice by scope.logoutNotice.collectAsState()
    val mask = scope.mask
    val authenticated = auth as? AuthState.Authenticated
    val userId = authenticated?.user?.id
    val demo = authenticated?.isDemo ?: false

    // --- deep-link gate (pure shell function, fed the REAL session) --------
    val sessionSnapshot = SessionSnapshot(isAuthenticated = authenticated != null, tenantId = null)
    val deepLinkTarget = parseDeepLink(deepLinkUri)

    val startDestination: String = when {
        deepLinkUri == null -> if (authenticated != null) Routes.GLANCE else Routes.SIGN_IN
        deepLinkTarget == null -> Routes.UNAVAILABLE
        // Signed-out deep link: land on sign-in; the EFFECT below notes the
        // intended destination for post-re-auth return (AC1.3). No existence
        // check at composition time — the background gate handles it.
        authenticated == null -> Routes.SIGN_IN
        deepLinkTarget is DeepLinkTarget.Account || deepLinkTarget is DeepLinkTarget.Holding -> {
            // Id route: defer the existence check to the background gate below.
            // Start at GLANCE; the gate navigates to the target or UNAVAILABLE
            // so the destination never renders before the check (docs/53 AC3.2).
            Routes.GLANCE
        }
        else -> deepLinkTargetRoute(deepLinkTarget)
    }

    // Note the signed-out deep link's destination for post-re-auth return
    // (AC1.3). In an EFFECT, not composition: a recomposition must never
    // re-note and clobber a fresher expiry-preserved destination — the
    // session effect below owns the last word on every auth transition, so
    // this effect is declared BEFORE it and never re-runs on a mere
    // recomposition.
    LaunchedEffect(deepLinkUri, authenticated) {
        if (deepLinkUri == null || authenticated != null) return@LaunchedEffect
        val target = parseDeepLink(deepLinkUri) ?: return@LaunchedEffect
        scope.destinations.note(deepLinkTargetRoute(target))
    }

    // NavHost back-stack state — needed by both the deep-link gate and the
    // session-driven navigation below.
    val backStackEntry by navController.currentBackStackEntryAsState()
    val currentRoute = backStackEntry?.destination?.route
    // Arguments captured at composition WITH the route. The session effect
    // below must read them from here, never live: when auth flips, this
    // composable recomputes [startDestination], and NavHost RESETS its
    // stack to the new start destination — by the time the effect body
    // runs, the live backStackEntry is already the post-reset entry and
    // the route the user was actually on (and its id arguments) exist only
    // in this captured pair. Losing them loses the AC1.3 preservation
    // (QA F1').
    val currentArgs = backStackEntry?.arguments

    // Background deep-link existence gate (AC3.2): for id routes, verify the
    // target exists and belongs to the session's tenant BEFORE the destination
    // renders. Runs on the IO dispatcher — targetExists may block. Keyed on
    // currentRoute != null so it waits for the NavHost to install its graph.
    var deepLinkGateDone by remember { mutableStateOf(false) }
    // The gate's verdict, published BEFORE it navigates. The session effect
    // below consults it: while a preserved deep-link destination is pending
    // and no verdict exists yet, the gate owns the navigation (QA F2'); once
    // a verdict exists, THAT decides what renders — the session effect may
    // consume the entry but must never override the verdict with its own
    // navigation (a fast gate racing a slow effect pass would otherwise let
    // a deleted/foreign target's route reach the screen).
    var deepLinkGateVerdict by remember { mutableStateOf<String?>(null) }
    // The identity the verdict above was delivered for. Session end
    // invalidates the verdict itself (below) but keeps this id, so a
    // SAME-identity re-auth can re-establish the verdict with a FRESH
    // existence check (docs/53 lifecycle Consume; QA R3-1) while an
    // identity change never re-validates another identity's link target
    // (security S1: no fetch, no verdict, nothing of A's route for B).
    var deepLinkGateVerdictUser by remember { mutableStateOf<Int?>(null) }
    // One-shot navigation request published by the gate and consumed by the
    // effect declared AFTER the NavHost (see the gate navigation at the
    // bottom of this composable) — never navigated from the IO-resumed
    // coroutine itself (that races the NavHost's graph install/reset, QA
    // R3-1).
    var deepLinkGateNavigateTo by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(deepLinkUri, authenticated, currentRoute != null) {
        if (authenticated == null) {
            // Session end invalidates the latched verdict: a verdict is a
            // statement about the session that consumed it, and docs/53's
            // Consume rule revalidates everything a returning user touches
            // ("nothing is trusted from before the session"). A same-identity
            // re-auth whose preserved destination IS the link's own route
            // re-runs the gate below instead of silently swallowing it
            // (QA R3-1: the link-launched expiry interleaving).
            deepLinkGateDone = false
            deepLinkGateVerdict = null
            return@LaunchedEffect
        }
        if (deepLinkGateDone || deepLinkUri == null) return@LaunchedEffect
        if (currentRoute == null) return@LaunchedEffect
        val target = parseDeepLink(deepLinkUri) ?: return@LaunchedEffect
        if (target !is DeepLinkTarget.Account && target !is DeepLinkTarget.Holding) return@LaunchedEffect
        val verdictUser = deepLinkGateVerdictUser
        if (verdictUser != null) {
            // The gate may answer once per session (QA F2'). After a session
            // end it re-runs ONLY for a same-identity re-auth whose preserved
            // destination is the link's own route (the R3-1 interleaving): a
            // different identity's commit (S1) and an explicit-sign-out
            // re-login (destination dropped) must never re-validate — and
            // never re-fetch — the old link target.
            val pending = scope.destinations.pending
            val sameIdentityReauth = verdictUser == userId &&
                pending == deepLinkTargetRoute(target)
            if (!sameIdentityReauth) return@LaunchedEffect
        }
        val exists = withContext(Dispatchers.IO) { targetExists(target) }
        deepLinkGateDone = true
        deepLinkGateVerdictUser = userId
        val verdict = if (exists) deepLinkTargetRoute(target) else Routes.UNAVAILABLE
        deepLinkGateVerdict = verdict
        deepLinkGateNavigateTo = verdict
    }

    // --- masking (docs/53 AC2) --------------------------------------------
    // Session transitions (logout / expiry / identity change) re-mask.
    MaskAuthEffects(mask = mask, authenticatedUserId = userId)
    // Background / device lock re-mask before the OS snapshot (AC2.3).
    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner, mask) {
        val observer = MaskLifecycleObserver(mask)
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }

    // --- session-driven navigation (AC1.3) ---------------------------------
    // Keyed on (auth, graphReady): before NavHost installs the graph there
    // is no back-stack entry to read or navigate on — the effect simply
    // re-runs once the graph exists (and re-reads whatever auth became in
    // the meantime), so no transition is ever lost to the boot race.
    LaunchedEffect(auth, currentRoute != null) {
        val current = currentRoute
        if (current == null) return@LaunchedEffect
        if (authenticated == null) {
            // Note the FILLED route (pattern + resolved arguments), never the
            // route pattern: navigating to the literal "account/{accountId}"
            // after re-auth renders account 0/garbage and the intended
            // account never loads (QA F1'). An argument we cannot fill — or
            // cannot trust to be a plain id token — is not preserved at all.
            val preserveable = filledRoute(current, currentArgs)
            if (preserveable != null &&
                preserveable != Routes.SIGN_IN &&
                preserveable != Routes.UNAVAILABLE
            ) {
                scope.destinations.note(preserveable)
            }
            if (current != Routes.SIGN_IN) {
                navController.navigate(Routes.SIGN_IN) { popUpTo(0) { inclusive = true } }
            }
        } else if (current == Routes.SIGN_IN || current == null) {
            // Re-auth consumption (QA F2', AC1.3): a preserved destination
            // that CAME FROM the launch deep link is owned by the existence/
            // tenant gate — this effect never navigates it. While the gate
            // has not answered yet, it hands off (no id destination may
            // render before the verdict); once the gate has answered, its
            // verdict already IS the navigation, so the entry is merely
            // consumed. A pending destination of the user's own browsing
            // (not the deep link's route) is consumed and returned to as
            // usual — the launch link is a boot artifact, not a hijack.
            val launchTarget = parseDeepLink(deepLinkUri)
            val deepLinkIdTarget = launchTarget is DeepLinkTarget.Account ||
                launchTarget is DeepLinkTarget.Holding
            val pending = scope.destinations.pending
            val gateOwnsThisDestination = deepLinkIdTarget &&
                pending != null && launchTarget != null &&
                pending == deepLinkTargetRoute(launchTarget)
            when {
                gateOwnsThisDestination && deepLinkGateVerdict == null -> {
                    // Hand-off: the gate effect above owns the navigation.
                }
                gateOwnsThisDestination -> {
                    // The gate delivered its verdict (target or the generic
                    // unavailable pane) — consume the entry and stay put.
                    scope.destinations.consume()
                }
                else -> {
                    val destination = scope.destinations.consume() ?: Routes.GLANCE
                    if (destination != current) {
                        navController.navigate(destination) { popUpTo(0) { inclusive = true } }
                    }
                }
            }
        } else {
            // Already on a content route at first composition (deep-link
            // start): consume any pending entry but stay put.
            scope.destinations.consume()
        }
    }

    val returnToLabel = scope.destinations.pending?.let(::routeLabel)

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text("FinTracker")
                        if (demo) {
                            DemoBadge(Modifier.padding(start = 12.dp))
                        }
                    }
                },
                // Pushed detail destinations get an EXPLICIT 48dp back
                // action; root tabs keep no icon (tap floor docs/09: 44px
                // minimum, back/eye ship at 48dp so no shared small-button
                // token can undersize them).
                navigationIcon = {
                    if (currentRoute == Routes.ACCOUNT_DETAIL || currentRoute == Routes.HOLDING_DETAIL) {
                        IconButton(
                            onClick = { navController.popBackStack() },
                            modifier = Modifier
                                .size(48.dp)
                                .testTag(AppBarTags.BACK),
                        ) {
                            Icon(
                                Icons.AutoMirrored.Filled.ArrowBack,
                                contentDescription = "Back",
                            )
                        }
                    }
                },
                actions = {
                    // The account-detail privacy action lives HERE so it
                    // never scrolls away (docs/56 §2.6): the screen's own
                    // header passes showMask=false, so the MASK_TOGGLE tag
                    // still resolves to exactly one node.
                    if (currentRoute == Routes.ACCOUNT_DETAIL) {
                        AppBarMaskToggle(mask = mask)
                    }
                },
            )
        },
        bottomBar = {
            if (authenticated != null) {
                V1BottomBar(currentRoute = currentRoute) { route ->
                    if (route != currentRoute) {
                        navController.navigate(route) { popUpTo(Routes.GLANCE) { inclusive = false } }
                    }
                }
            }
        },
    ) { padding ->
        NavHost(
            navController = navController,
            startDestination = startDestination,
            modifier = Modifier.padding(padding),
        ) {
            composable(Routes.SIGN_IN) {
                SignInScreen(
                    authState = auth,
                    actions = scope.signInActions,
                    returnToLabel = returnToLabel,
                    notice = logoutNotice,
                )
            }
            composable(Routes.GLANCE) {
                GlanceScreen(
                    portfolio = remember { scope.sections.portfolio() },
                    accounts = remember { scope.sections.accounts() },
                    mask = mask,
                    demo = demo,
                    authState = auth,
                    onOpenAccount = { id ->
                        navController.navigate(Routes.accountDetail(id.toString()))
                    },
                    onOpenHolding = { accountId, securityId ->
                        navController.navigate(Routes.holdingDetail(accountId.toString(), securityId.toString()))
                    },
                )
            }
            composable(Routes.ACCOUNTS) {
                AccountsScreen(
                    accounts = remember { scope.sections.accountsList() },
                    mask = mask,
                    demo = demo,
                    authState = auth,
                    onOpenAccount = { id ->
                        navController.navigate(Routes.accountDetail(id.toString()))
                    },
                )
            }
            composable(
                route = Routes.ACCOUNT_DETAIL,
                arguments = listOf(navArgument(Routes.ARG_ACCOUNT_ID) { type = NavType.StringType }),
            ) { entry ->
                val accountId = entry.arguments?.getString(Routes.ARG_ACCOUNT_ID)?.toIntOrNull() ?: 0
                AccountDetailScreen(
                    accountId = accountId,
                    account = remember(accountId) { scope.sections.account(accountId) },
                    ledger = remember(accountId) { scope.sections.ledger(accountId) },
                    holdings = remember(accountId) { scope.sections.holdings(accountId) },
                    mask = mask,
                    demo = demo,
                    // Navigation-only drill-in, wired exactly like Glance's
                    // PositionRow (no transaction-detail route is invented).
                    onOpenHolding = { a, s ->
                        navController.navigate(Routes.holdingDetail(a.toString(), s.toString()))
                    },
                    // Ledger pagination through the EXISTING /transactions
                    // route — no new API (PM ruling).
                    ledgerMoreSource = { limit, offset ->
                        scope.sections.accountTransactions(accountId, limit, offset)
                    },
                    authState = auth,
                )
            }
            composable(
                route = Routes.HOLDING_DETAIL,
                arguments = listOf(
                    navArgument(Routes.ARG_ACCOUNT_ID) { type = NavType.StringType },
                    navArgument(Routes.ARG_HOLDING_ID) { type = NavType.StringType },
                ),
            ) { entry ->
                val accountId = entry.arguments?.getString(Routes.ARG_ACCOUNT_ID)?.toIntOrNull() ?: 0
                val securityId = entry.arguments?.getString(Routes.ARG_HOLDING_ID)?.toIntOrNull() ?: 0
                HoldingDetailScreen(
                    accountId = accountId,
                    securityId = securityId,
                    holding = remember(accountId, securityId) {
                        scope.sections.holding(accountId, securityId)
                    },
                    mask = mask,
                    demo = demo,
                    authState = auth,
                )
            }
            composable(Routes.TRANSACTIONS) {
                TransactionsScreen(
                    transactions = remember { scope.sections.transactions() },
                    eventsSource = { txId -> scope.sections.events(txId) },
                    mask = mask,
                    demo = demo,
                    authState = auth,
                )
            }
            composable(Routes.SETTINGS) {
                SettingsScreen(
                    mask = mask,
                    demo = demo,
                    authState = auth,
                    onSignOut = scope.onSignOut,
                )
            }
            composable(Routes.UNAVAILABLE) {
                Box(
                    modifier = Modifier
                        .fillMaxSize()
                        .testTag(ScreenTags.UNAVAILABLE),
                    contentAlignment = Alignment.Center,
                ) {
                    StatePane<Nothing>(ScreenState.Unavailable())
                }
            }
        }
    }

    // The gate's navigation, performed one composition pass AFTER the verdict
    // is published. Declared AFTER the NavHost so — by composition order — it
    // runs after the NavHost has installed/reset its graph for the current
    // auth state: the IO-resumed gate coroutine can publish its verdict only
    // as a later main-looper message, so this effect always navigates on top
    // of the settled graph (navigating from the IO resume itself races the
    // auth-flip's graph reset and the reset silently wipes the verdict's
    // destination — QA R3-1). One-shot: consumed before navigating, and a
    // disposed composition cancels this effect instead of navigating a dead
    // controller.
    LaunchedEffect(deepLinkGateNavigateTo) {
        val destination = deepLinkGateNavigateTo ?: return@LaunchedEffect
        deepLinkGateNavigateTo = null
        navController.navigate(destination) { popUpTo(0) { inclusive = true } }
    }
}

/** v1 bottom navigation: the four list-level destinations (docs/48 §2.2). */
@Composable
private fun V1BottomBar(currentRoute: String?, onNavigate: (String) -> Unit) {
    val items = listOf(
        BottomItem(Routes.GLANCE, "Home", Icons.Default.Home),
        BottomItem(Routes.ACCOUNTS, "Accounts", Icons.Default.AccountBalance),
        BottomItem(Routes.TRANSACTIONS, "Transactions", Icons.Default.ReceiptLong),
        BottomItem(Routes.SETTINGS, "Settings", Icons.Default.Settings),
    )
    NavigationBar {
        items.forEach { item ->
            NavigationBarItem(
                selected = currentRoute == item.route ||
                    (item.route == Routes.ACCOUNTS && currentRoute?.startsWith("account/") == true),
                onClick = { onNavigate(item.route) },
                icon = { Icon(item.icon, contentDescription = null) },
                label = { Text(item.label) },
            )
        }
    }
}

private data class BottomItem(val route: String, val label: String, val icon: androidx.compose.ui.graphics.vector.ImageVector)

/**
 * Map a [DeepLinkTarget] to its NavHost route string. Pure — no existence
 * check; the caller decides when to gate.
 */
fun deepLinkTargetRoute(target: DeepLinkTarget): String = when (target) {
    DeepLinkTarget.Glance -> Routes.GLANCE
    DeepLinkTarget.AccountsList -> Routes.ACCOUNTS
    DeepLinkTarget.Transactions -> Routes.TRANSACTIONS
    DeepLinkTarget.Settings -> Routes.SETTINGS
    is DeepLinkTarget.Account -> Routes.accountDetail(target.accountId)
    is DeepLinkTarget.Holding -> Routes.holdingDetail(target.accountId, target.holdingId)
}

private val ROUTE_PLACEHOLDER = Regex("\\{([A-Za-z][A-Za-z0-9_]*)\\}")

/** Id-token shape a route argument must have to be preserved (QA F1' + security S1). */
private val SAFE_ARG_VALUE = Regex("[A-Za-z0-9._-]+")

/**
 * Fill a route PATTERN with the back-stack entry's resolved arguments —
 * `("account/{accountId}", accountId=5)` → `"account/5"`.
 *
 * Returns the pattern untouched when it has no placeholders, and `null`
 * when any placeholder cannot be filled from [arguments] or when a value is
 * not a plain id token (slashes/braces would compose a DIFFERENT route —
 * such a destination is never preserved). Preservation always deals in
 * concrete routes: re-auth navigating to a literal `{accountId}` renders
 * account 0 and never fetches the intended account (docs/53 AC1.3, QA F1').
 */
fun filledRoute(pattern: String?, arguments: Bundle?): String? {
    if (pattern == null) return null
    var filled: String = pattern
    for (match in ROUTE_PLACEHOLDER.findAll(pattern)) {
        val name = match.groupValues[1]
        val value = arguments?.getString(name)
        if (value.isNullOrEmpty()) return null
        if (!SAFE_ARG_VALUE.matches(value)) return null
        filled = filled.replace("{${name}}", value)
    }
    return filled
}

/**
 * Friendly label for an internal route — what the sign-in screen tells the
 * user they'll return to (raw routes are never shown).
 */
fun routeLabel(route: String): String = when {
    route == Routes.GLANCE -> "Home"
    route == Routes.ACCOUNTS -> "Accounts"
    route == Routes.TRANSACTIONS -> "Transactions"
    route == Routes.SETTINGS -> "Settings"
    route.startsWith("account/") && route.contains("/holding/") -> "the holding you were viewing"
    route.startsWith("account/") -> "the account you were viewing"
    else -> "the previous screen"
}
