import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { motion } from "framer-motion";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  Suspense,
  lazy,
} from "react";
import type { ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeftRight,
  Bell,
  LogOut,
  Menu,
  Moon,
  Search,
  Settings as SettingsIcon,
  Sun,
  TrendingUp,
  ChartColumn,
  Users,
  X,
  Zap,
} from "lucide-react";
import { useAuth } from "../../auth/AuthContext";
import { useTheme } from "../../theme/ThemeProvider";
import { fetchGlobalSearch } from "../../lib/globalSearchQuery";
import { VaultLocked } from "../e2ee/VaultLocked";
import { useVaultEnrollment, useVaultUnlocked } from "../e2ee/useUnlock";
import {
  createSecurity,
  lookupSecurity,
  resolvedListing,
  SecurityLookupResult,
} from "../../api/securities";
import type { SecurityInput, SecurityLookupCandidate } from "../../api/securities";
import { CandidatePicker } from "../investments/SymbolSuggestion";
import { apiErrorMessage } from "../transactions/format";
import {
  INBOX_QUERY_KEY,
  getNotificationInbox,
  markAllNotificationsRead,
  markNotificationRead,
  unreadBadgeLabel,
} from "../../api/notifications";
import { useToast } from "../ui/Toast";
import { EASE, routeFade } from "../ui/motion";
import { listAccounts } from "../../api/accounts";
// M100: the grouping shared with the /accounts index page, so the drawer's
// "Accounts" section, the More panel and the page they open cannot drift.
import { TYPE_GROUPS } from "../../lib/accountGroups";
import { formatMoney } from "../../lib/format";
import { MobileTabBar } from "../shell/MobileTabBar";
import { TopBar } from "../shell/TopBar";
import { TruthBar } from "../shell/TruthBar";
import {
  PRIMARY_GROUP,
  PRIMARY_TABS,
  TOOLS_GROUP,
  TOUR_TARGETS,
} from "../shell/navGroups";
import { ScheduledBackupRunner } from "../ScheduledBackupPanel";
import { ScheduledExportRunner } from "../ScheduledExportPanel";
import { useDialogFocus } from "../ui/dialogA11y";
import { PullToRefreshIndicator, usePullToRefresh } from "../ui/PullToRefresh";
import { Tour } from "../tour/Tour";
import {
  MaskChip,
  MaskLiveRegion,
  MaskToggleRow,
  focusMaskChip,
} from "../privacy/MaskChip";
import { maskAmountsInText, toggleMasked, useMasked } from "../../lib/mask";
import { PageMetaProvider } from "../../lib/pageMeta";

// M95 P1 — the command palette ships as its OWN chunk: its registry carries
// the whole guide-copy table, and the entry chunk keeps a 700,000 B ceiling
// (docs/09 §Bundle facts). Mounted only while open, so the first ⌘K pays for
// the fetch and every later one is instant.
const CommandPalette = lazy(() => import("./CommandPalette"));

/** SSR-safe media query state (vite SPA — window always present). */
function useMinWidth(px: number): boolean {
  const [matches, setMatches] = useState(
    () => window.matchMedia(`(min-width: ${px}px)`).matches,
  );
  useEffect(() => {
    const mql = window.matchMedia(`(min-width: ${px}px)`);
    const onChange = (e: MediaQueryListEvent) => setMatches(e.matches);
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, [px]);
  return matches;
}

/** M95 P1 — which modifier label the palette trigger prints (⌘K vs Ctrl+K). */
function isMacPlatform(): boolean {
  return (
    typeof navigator !== "undefined" &&
    /Mac|iPhone|iPad|iPod/.test(navigator.platform)
  );
}

/**
 * Shared layout for every guarded route (M185, docs/47 §1).
 *
 * The LEFT RAIL IS RETIRED: desktop chrome is `TopBar` (brand → five primary
 * tabs → More → right cluster) over `TruthBar`, both sticky as ONE block so a
 * page card can never outrank them; below md the chrome is the fixed appbar +
 * the truth line + `MobileTabBar` (five tabs, centre `Add`) over the drawer,
 * which carries the same three destination groups plus the session controls.
 * Routes are unchanged — only the chrome that reaches them is.
 *
 * One window keydown listener for the whole shell (M72 F1): palette → inbox →
 * search → More panel → account menu → drawer, innermost first. The More panel
 * is the shell Escape router's FIFTH consumer (docs/09 §Escape routing gains
 * exactly one more consumer, never a second listener).
 */
export function AppShell() {
  const { user, logout } = useAuth();
  const { theme, toggleTheme } = useTheme();
  const location = useLocation();
  const isDesktop = useMinWidth(768);
  // M139 (docs/09 § Display masking): ONE subscription re-renders the whole
  // shell — and because this codebase has no React.memo and no useMemo around
  // money formatting, that re-render walks down through <Outlet/> into every
  // mounted page, so each fmtCad call re-reads the ambient mask. That is what
  // makes ⌘⇧H instant with no prop threading and no API round-trip (D3).
  useMasked();
  // M72 (docs/09 §Mobile navigation): below md the destinations live in an
  // off-canvas drawer; at md+ they live in the TopBar's More panel.
  const [drawerOpen, setDrawerOpen] = useState(false);
  // M185: the More popover + the account menu are two more shell SURFACES on
  // the one Escape router, never listeners of their own.
  const [moreOpen, setMoreOpen] = useState(false);
  const [accountOpen, setAccountOpen] = useState(false);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const toast = useToast();
  // M72 pull-to-refresh: a full invalidation re-reads every active query from
  // the server — the gesture never serves local data (docs/09).
  const ptr = usePullToRefresh(() => queryClient.invalidateQueries(), {
    disabled: drawerOpen,
  });
  const searchRef = useRef<HTMLDivElement>(null);
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  // M95 P1 — the ⌘K command palette. It is one more consumer of the shell's
  // single Escape router below, never a second keydown listener of its own.
  const [paletteOpen, setPaletteOpen] = useState(false);
  const paletteOpenedByShortcut = useRef(false);
  const paletteShortcutLauncher = useRef<HTMLButtonElement | null>(null);
  const closePalette = useCallback(() => {
    setPaletteOpen(false);
  }, []);
  useEffect(() => {
    if (paletteOpen || !paletteOpenedByShortcut.current) return;
    const timer = window.setTimeout(() => {
      paletteOpenedByShortcut.current = false;
      const target = paletteShortcutLauncher.current?.isConnected
        ? paletteShortcutLauncher.current
        : mainRef.current;
      target?.focus({ preventScroll: true });
    }, 0);
    return () => window.clearTimeout(timer);
  }, [paletteOpen]);
  const [market, setMarket] = useState<{
    status: "idle" | "pending" | "found" | "ambiguous" | "miss";
    result?: SecurityLookupResult;
    candidates?: SecurityLookupCandidate[];
  }>({ status: "idle" });

  // M72 drawer: focus hand-off follows the shared dialog contract
  // (dialogA11y — focus in on open, back to the toggle on close), a route
  // change closes it, and `inert` parks it out of the focus order and the
  // accessibility tree while it sits off-canvas. Escape is NOT handled here:
  // the shell's single keydown router owns it (see `escapeState` below).
  const navRef = useRef<HTMLElement | null>(null);
  const toggleRef = useRef<HTMLButtonElement | null>(null);
  const mainRef = useRef<HTMLElement | null>(null);
  const drawerActive = !isDesktop && drawerOpen;
  useDialogFocus(navRef, drawerActive);
  // ROUTE-FOCUS HANDOFF (docs/09 "Keyboard contract"): leaving the chrome
  // through a tab, the More panel or the bottom tab bar must park focus in the
  // routed page, not on the control that was just used — otherwise every
  // navigation strands a keyboard user at the top of the chrome and they walk
  // it again to reach the content (WCAG 2.4.3).
  const prevPath = useRef<string | null>(null);
  const [routeFocus, setRouteFocus] = useState(0);
  useEffect(() => {
    // First paint is NOT a navigation: the skip link stays the first stop on
    // a freshly loaded page. A remount (StrictMode) sees the same pathname
    // twice, so identity — not "have I run before" — decides.
    const navigated =
      prevPath.current !== null && prevPath.current !== location.pathname;
    prevPath.current = location.pathname;
    setDrawerOpen(false);
    setMoreOpen(false);
    setAccountOpen(false);
    if (navigated) setRouteFocus((n) => n + 1);
  }, [location.pathname]);
  // Two-phase on purpose: this effect runs in the NEXT commit, after every
  // surface closed above has already handed focus back to its opener
  // (`useDialogFocus` restores the `More` button when the panel unmounts). A
  // hand-off issued from the closing effect would be undone by that restore,
  // leaving focus parked in the chrome.
  useEffect(() => {
    if (routeFocus === 0) return;
    const target = mainRef.current;
    const active = document.activeElement as HTMLElement | null;
    if (target === null || active === null) return;
    // Already on the routed page (the page put focus there itself) — the
    // hand-off never moves focus that is already in the content.
    if (active === target || target.contains(active)) return;
    // A modal owns focus (TransactionEditor, the ⌘K palette): a route change
    // underneath it must not steal focus out of the dialog.
    if (
      active !== document.body &&
      active.closest('[role="dialog"],[aria-modal="true"]') !== null
    ) {
      return;
    }
    // preventScroll: the shell deliberately does not reset scroll on a route
    // change, and focus management must not smuggle a scroll in with it.
    target.focus({ preventScroll: true });
  }, [routeFocus]);
  useEffect(() => {
    const node = navRef.current as (HTMLElement & { inert?: boolean }) | null;
    if (node) node.inert = !isDesktop && !drawerOpen;
  }, [isDesktop, drawerOpen]);

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedQuery(query), 200);
    return () => clearTimeout(timer);
  }, [query]);

  const trimmedQuery = debouncedQuery.trim();
  // M136 rule 7c: `useVaultUnlocked` subscribes to `vaultchange`, so unlocking
  // inline re-renders the shell so the shared lock status stays current.
  const vaultUnlocked = useVaultUnlocked();
  const vaultEnrollment = useVaultEnrollment();
  // Live vault state handed to the shared search queryFn
  // (lib/globalSearchQuery): the queryFn re-reads it around the async token
  // generation, so a lock landing mid-flight can never send a stale
  // blind-index token from this surface either.
  const liveVaultUnlocked = useRef(vaultUnlocked);
  liveVaultUnlocked.current = vaultUnlocked;
  const search = useQuery({
    // docs/09 "Query keys": the shared global-search pair — same key AND
    // same fetch function as the ⌘K palette, so both surfaces read one
    // cached `GET /search` result. Pinned by queryKeyShapes.
    queryKey: ["search", trimmedQuery, vaultUnlocked],
    queryFn: ({ signal }) =>
      fetchGlobalSearch(trimmedQuery, vaultUnlocked, liveVaultUnlocked, signal),
    enabled: trimmedQuery.length >= 2,
  });
  const mergedTransactions = useMemo(
    () => (search.data?.transactions ?? []).map((t) => ({ id: t.id, payee: t.normalized_payee, category_name: t.category_name })),
    [search.data],
  );
  const lockedEncrypted = (search.data?.withheld ?? 0) > 0;
  const noSearchResults = search.data !== undefined && mergedTransactions.length === 0 &&
    search.data.categories.length === 0 && search.data.accounts.length === 0 &&
    search.data.securities.length === 0 && (search.data.tags ?? []).length === 0;

  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: () => listAccounts(200),
  });
  const accountItems = accounts.data?.items ?? [];
  // The rail's Tools gate, unchanged: the currency tracker owns /currencies,
  // so the whole Tools group stays behind `currency_tracker_enabled`.
  const toolsEnabled = user?.currency_tracker_enabled ?? false;

  // M51 inbox (docs/35): one fetch drives BOTH the shell unread badge and
  // the Dashboard critical-alert banner (same query key). The server gates the
  // response by the notification toggles, so hiding a type needs no client
  // settings read. Errors surface through the toast; the demo tenant sees
  // `demo_account_restricted` on mark-read like every other write button.
  const inbox = useQuery({
    queryKey: INBOX_QUERY_KEY,
    queryFn: () => getNotificationInbox(50),
    staleTime: 30_000,
    refetchInterval: 60_000,
    retry: false,
  });
  const unread = inbox.data?.unread_count ?? 0;
  const badgeLabel = unreadBadgeLabel(unread);
  const [inboxOpen, setInboxOpen] = useState(false);
  const inboxRef = useRef<HTMLDivElement>(null);
  // Focus-in on open / back to the bell on close — the SAME dialogA11y
  // contract the More panel follows (docs/09 Keyboard contract): without it an
  // Escape pressed from inside the panel unmounts the focused control and
  // drops focus on the body, stranding a keyboard user at the document start.
  const inboxPanelRef = useRef<HTMLDivElement | null>(null);
  useDialogFocus(inboxPanelRef, inboxOpen);
  useEffect(() => {
    if (!inboxOpen) return;
    // Escape is routed by the shell-level keydown router below — only the
    // outside-click half lives here.
    function onPointerDown(event: MouseEvent) {
      if (!inboxRef.current?.contains(event.target as Node | null))
        setInboxOpen(false);
    }
    window.addEventListener("mousedown", onPointerDown);
    return () => {
      window.removeEventListener("mousedown", onPointerDown);
    };
  }, [inboxOpen]);

  // The account menu closes on outside pointer-down the same way (Escape is
  // routed by the single listener below).
  const accountRef = useRef<HTMLDivElement>(null);
  const accountPanelRef = useRef<HTMLDivElement | null>(null);
  useDialogFocus(accountPanelRef, accountOpen);
  useEffect(() => {
    if (!accountOpen) return;
    function onPointerDown(event: MouseEvent) {
      if (!accountRef.current?.contains(event.target as Node | null))
        setAccountOpen(false);
    }
    window.addEventListener("mousedown", onPointerDown);
    return () => {
      window.removeEventListener("mousedown", onPointerDown);
    };
  }, [accountOpen]);

  const markRead = useMutation({
    mutationFn: markNotificationRead,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: INBOX_QUERY_KEY });
    },
    onError: (err) =>
      toast.error("Could not mark as read", apiErrorMessage(err)),
  });
  const markAllRead = useMutation({
    mutationFn: markAllNotificationsRead,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: INBOX_QUERY_KEY });
    },
    onError: (err) =>
      toast.error("Could not mark all read", apiErrorMessage(err)),
  });

  function openInboxItem(item: {
    id: string;
    link: string;
    read: boolean;
  }): void {
    setInboxOpen(false);
    navigate(item.link);
    if (!item.read) markRead.mutate(item.id);
  }

  // Stable identity: the Escape router below registers ONCE and reaches this
  // through the closure, so it must not capture a per-render function.
  const closeSearch = useCallback((): void => {
    setSearchOpen(false);
    setMarket({ status: "idle" });
  }, []);

  async function lookupMarketData(): Promise<void> {
    setMarket({ status: "pending" });
    const typed = query.trim().toUpperCase();
    try {
      const result = await lookupSecurity(typed);
      const listing = resolvedListing(result);
      if (listing === null) {
        // M142: a typed NAME that matched several listings is a CHOICE, not
        // a miss — the pick list is the affordance (docs/09).
        setMarket(
          result.match === "ambiguous"
            ? { status: "ambiguous", candidates: result.candidates }
            : { status: "miss" },
        );
        return;
      }
      setMarket({ status: "found", result });
    } catch {
      setMarket({ status: "miss" });
    }
  }

  // M142 sent the pick back through the lookup endpoint so the create-ready
  // payload (kind, listing currency, exchange) came from a fresh answer.
  // M143: a chooser row IS that payload now (docs/07 §Candidate resolution),
  // so the pick is applied directly.
  function pickMarketCandidate(candidate: SecurityLookupCandidate): void {
    setMarket({
      status: "found",
      result: {
        symbol: candidate.symbol,
        name: candidate.name,
        kind: candidate.kind,
        listing_currency: candidate.listing_currency,
        exchange: candidate.exchange,
        exchange_display: candidate.exchange_display,
        match: "direct",
        candidates: [],
      },
    });
  }

  const addFromMarket = useMutation({
    mutationFn: (listing: SecurityInput) => createSecurity(listing),
    onSuccess: (security) => {
      void queryClient.invalidateQueries({ queryKey: ["securities"] });
      toast.success(`Added ${security.symbol}`, "Fetching price history…");
      closeSearch();
      setQuery("");
      navigate(`/securities/${security.id}`);
    },
    onError: (err) =>
      toast.error("Could not add security", apiErrorMessage(err)),
  });
  // One narrowing per render: the "found" row renders exactly the listing the
  // Add button mutates (docs/08 `resolvedListing`), so they cannot disagree.
  const marketListing = market.result ? resolvedListing(market.result) : null;
  // Close on outside pointer-down like every other surface dialog (Escape is
  // routed by the single shell-level listener below).
  useEffect(() => {
    function onPointerDown(event: MouseEvent) {
      if (!searchRef.current?.contains(event.target as Node | null))
        closeSearch();
    }
    window.addEventListener("mousedown", onPointerDown);
    return () => {
      window.removeEventListener("mousedown", onPointerDown);
    };
  }, [closeSearch]);

  // M72 F1 — ONE window keydown listener for the whole shell. The drawer, the
  // search overlay and the notification panel used to register three
  // independent Escape listeners; a listener registered once with state read
  // from a ref cannot be unhooked by any render, however it is timed, and the
  // order in which surfaces react no longer depends on registration order.
  const escapeState = useRef({
    drawerActive,
    searchOpen,
    inboxOpen,
    paletteOpen,
    moreOpen,
    accountOpen,
  });
  // Layout effect, not passive: the ref must be current BEFORE the browser can
  // deliver another input event, so a keydown can never observe the state of a
  // render whose effects have not flushed yet.
  useLayoutEffect(() => {
    escapeState.current = {
      drawerActive,
      // A surface only counts as open while it is ON SCREEN: merely focusing
      // the search field (searchOpen) with no query must not swallow the
      // Escape that should close the drawer behind it.
      searchOpen: searchOpen && trimmedQuery.length >= 2,
      inboxOpen,
      paletteOpen,
      moreOpen,
      accountOpen,
    };
  });
  useEffect(() => {
    function onKeydown(event: KeyboardEvent) {
      // M95 P1 — ⌘K / Ctrl+K toggles the palette, handled INSIDE this same
      // listener on purpose: the shell owns exactly ONE window keydown
      // listener (docs/09 §Escape routing), and a second listener for the
      // shortcut would re-open the M72 F1 class of bug the single router exists
      // to close.
      // M139 ⌘⇧H / Ctrl+Shift+H — the display mask (docs/09 §Display
      // masking). Handled INSIDE this same listener for the same reason as
      // ⌘K above. Focus goes back to the chip so a keyboard user lands on what
      // they changed.
      if (
        (event.metaKey || event.ctrlKey) &&
        event.shiftKey &&
        (event.key === "h" || event.key === "H")
      ) {
        event.preventDefault();
        toggleMasked();
        focusMaskChip();
        return;
      }
      if (
        (event.metaKey || event.ctrlKey) &&
        (event.key === "k" || event.key === "K")
      ) {
        event.preventDefault();
        if (escapeState.current.paletteOpen) {
          closePalette();
        } else {
          const launchers = document.querySelectorAll<HTMLButtonElement>(
            '[data-testid="command-palette-trigger"], [data-testid="command-palette-trigger-mobile"]',
          );
          paletteShortcutLauncher.current =
            Array.from(launchers).find((launcher) => launcher.getClientRects().length > 0) ??
            Array.from(launchers).find((launcher) => launcher.isConnected) ??
            null;
          paletteOpenedByShortcut.current = true;
          setPaletteOpen(true);
        }
        return;
      }
      if (event.key !== "Escape") return;
      const {
        drawerActive: drawer,
        searchOpen: searchSurface,
        inboxOpen: inboxSurface,
        paletteOpen: palette,
        moreOpen: more,
        accountOpen: account,
      } = escapeState.current;
      // One layer per press, innermost first: an open popover inside the
      // drawer wins over the drawer itself, so a stray Escape never yanks the
      // nav shut behind a panel the user is still looking at. The palette is
      // the top-most layer (M95 P1) — one press closes ONLY the palette; the
      // next press reaches whatever sits behind it. M185 adds the More panel
      // and the account menu as surfaces ON this router (docs/09 §Escape
      // routing gains consumers, never listeners).
      if (palette) {
        closePalette();
      } else if (inboxSurface) {
        setInboxOpen(false);
      } else if (searchSurface) {
        closeSearch();
      } else if (more) {
        setMoreOpen(false);
      } else if (account) {
        setAccountOpen(false);
      } else if (drawer) {
        setDrawerOpen(false);
      }
    }
    window.addEventListener("keydown", onKeydown);
    return () => window.removeEventListener("keydown", onKeydown);
  }, [closeSearch]);

  function goToTransactions(params: string) {
    navigate(`/transactions?${params}`);
    closeSearch();
  }

  /**
   * The shell's search field + results panel, rendered ONCE as a function and
   * mounted in exactly one place per breakpoint: the TopBar's right cluster at
   * md+, the drawer below md. Same markup, same `search-panel` testid, same
   * z-order contract (docs/47 §1.1 — the search overlay keeps its contract).
   */
  function renderSearch(): ReactNode {
    return (
      <div ref={searchRef} className="relative">
        <label className="relative block">
          <span className="sr-only">Search</span>
          <Search
            className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted"
            aria-hidden
          />
          <input
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setMarket({ status: "idle" });
              setSearchOpen(true);
            }}
            onFocus={() => setSearchOpen(true)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && query.trim() !== "") {
                goToTransactions(`q=${encodeURIComponent(query.trim())}`);
              }
            }}
            placeholder="Search…"
            aria-label="Search transactions, categories, accounts"
            className="field-ring w-full rounded-lg border border-control bg-surface py-1.5 pl-8 pr-2 text-sm text-text outline-none transition-colors placeholder:text-muted hover:border-accent/70"
          />
        </label>
        {searchOpen && trimmedQuery.length >= 2 && (
          <div
            data-testid="search-panel"
            className="absolute left-0 right-0 top-full z-50 mt-1 max-h-[24rem] overflow-y-auto rounded-xl border border-border bg-surface-raised/95 p-2 shadow-glow-sm"
          >
            {search.isPending && (
              <p className="px-2 py-3 text-xs text-muted">Searching…</p>
            )}
            {search.isError && (
              <p className="px-2 py-3 text-xs text-danger">Search failed</p>
            )}
            {/* docs/33 rule 7f — a lock hint REPLACES an empty state, it
                never stacks with it, and rule 7e — the outage is scoped to
                what the lock withholds: encrypted PAYEES. */}
            {noSearchResults && lockedEncrypted && (
              <VaultLocked
                variant="blocked"
                capability="search"
                className="px-2 py-3"
              />
            )}
            {noSearchResults && !lockedEncrypted && (
              <p className="px-2 py-3 text-xs text-muted">No matches</p>
            )}
            {search.data !== undefined && mergedTransactions.length > 0 && (
              <>
                <p className="px-2 pb-1 pt-1.5 text-[11px] uppercase tracking-widest text-muted">
                  Transactions
                </p>
                {mergedTransactions.map((transaction) => (
                  <button
                    key={transaction.id}
                    type="button"
                    onClick={() =>
                      goToTransactions(
                        `q=${encodeURIComponent(transaction.payee)}`,
                      )
                    }
                    className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left outline-none transition-colors hover:bg-veil/[0.06] focus-visible:ring-2 focus-visible:ring-accent"
                  >
                    <ArrowLeftRight
                      className="size-3.5 shrink-0 text-muted"
                      aria-hidden
                    />
                    <span className="min-w-0 flex-1 truncate text-sm">
                      {transaction.payee}
                    </span>
                    <span className="shrink-0 text-[11px] text-muted">
                      {transaction.category_name ?? "Uncategorized"}
                    </span>
                  </button>
                ))}
              </>
            )}
            {search.data !== undefined && search.data.securities.length > 0 && (
              <>
                <p className="px-2 pb-1 pt-1.5 text-[11px] uppercase tracking-widest text-muted">
                  Securities
                </p>
                {search.data.securities.map((security) => (
                  <button
                    key={security.id}
                    type="button"
                    onClick={() => {
                      closeSearch();
                      navigate(`/securities/${security.id}`);
                    }}
                    className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left outline-none transition-colors hover:bg-veil/[0.06] focus-visible:ring-2 focus-visible:ring-accent"
                  >
                    <TrendingUp
                      className="size-3.5 shrink-0 text-muted"
                      aria-hidden
                    />
                    <span className="min-w-0 flex-1 truncate text-sm font-medium">
                      {security.symbol}
                    </span>
                    <span className="min-w-0 truncate text-sm text-muted">
                      {security.name}
                    </span>
                    <span className="text-[11px] text-muted">
                      {security.exchange}
                    </span>
                  </button>
                ))}
              </>
            )}
            {search.data !== undefined && search.data.categories.length > 0 && (
              <>
                <p className="px-2 pb-1 pt-1.5 text-[11px] uppercase tracking-widest text-muted">
                  Categories
                </p>
                {search.data.categories.map((category) => (
                  <button
                    key={category.id}
                    type="button"
                    onClick={() => goToTransactions(`category=${category.id}`)}
                    className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left outline-none transition-colors hover:bg-veil/[0.06] focus-visible:ring-2 focus-visible:ring-accent"
                  >
                    <ChartColumn
                      className="size-3.5 shrink-0 text-muted"
                      aria-hidden
                    />
                    <span className="truncate text-sm">{category.name}</span>
                  </button>
                ))}
              </>
            )}
            {search.data !== undefined && search.data.accounts.length > 0 && (
              <>
                <p className="px-2 pb-1 pt-1.5 text-[11px] uppercase tracking-widest text-muted">
                  Accounts
                </p>
                {search.data.accounts.map((account) => (
                  <button
                    key={account.id}
                    type="button"
                    onClick={() => goToTransactions(`account=${account.id}`)}
                    className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left outline-none transition-colors hover:bg-veil/[0.06] focus-visible:ring-2 focus-visible:ring-accent"
                  >
                    <SettingsIcon
                      className="size-3.5 shrink-0 text-muted"
                      aria-hidden
                    />
                    <span className="min-w-0 flex-1 truncate text-sm">
                      {account.name}
                    </span>
                    <span className="shrink-0 text-[11px] text-muted">
                      {account.type}
                    </span>
                  </button>
                ))}
              </>
            )}
            {(search.data?.tags ?? []).length > 0 && (
              <>
                <p className="px-2 pb-1 pt-1.5 text-[11px] uppercase tracking-widest text-muted">
                  Tags
                </p>
                {(search.data?.tags ?? []).map((tag) => (
                  <button
                    key={tag.id}
                    type="button"
                    onClick={() => {
                      closeSearch();
                      navigate("/tags");
                    }}
                    className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left outline-none transition-colors hover:bg-veil/[0.06] focus-visible:ring-2 focus-visible:ring-accent"
                  >
                    <Users
                      className="size-3.5 shrink-0 text-muted"
                      aria-hidden
                    />
                    <span className="truncate text-sm">{tag.name}</span>
                  </button>
                ))}
              </>
            )}
            {market.status === "idle" && (
              <button
                type="button"
                onClick={() => void lookupMarketData()}
                className="mt-1 flex w-full items-center gap-2 rounded-lg border-t border-veil/[0.06] px-2 pt-2 text-left text-xs text-accent outline-none transition-colors hover:text-text focus-visible:ring-2 focus-visible:ring-accent"
              >
                <Search className="size-3.5 shrink-0" aria-hidden />
                Search market data for “{trimmedQuery}”
              </button>
            )}
            {market.status === "pending" && (
              <p className="px-2 py-2 text-xs text-muted">
                Searching market data…
              </p>
            )}
            {market.status === "found" && marketListing !== null && (
              <button
                type="button"
                disabled={addFromMarket.isPending}
                onClick={() => {
                  if (marketListing) addFromMarket.mutate(marketListing);
                }}
                className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left outline-none transition-colors hover:bg-veil/[0.06] focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-40"
              >
                <TrendingUp
                  className="size-3.5 shrink-0 text-muted"
                  aria-hidden
                />
                <span className="min-w-0 flex-1 truncate text-sm font-medium">
                  {marketListing.symbol}
                </span>
                <span className="min-w-0 truncate text-sm text-muted">
                  {marketListing.name}
                </span>
                <span className="shrink-0 rounded-md bg-accent/15 px-1.5 py-0.5 text-[11px] text-accent">
                  Add
                </span>
              </button>
            )}
            {market.status === "ambiguous" &&
              (market.candidates ?? []).length > 0 && (
                <CandidatePicker
                  typed={trimmedQuery}
                  candidates={market.candidates ?? []}
                  pending={false}
                  onPick={(candidate) => void pickMarketCandidate(candidate)}
                  onDismiss={closeSearch}
                />
              )}
            {market.status === "miss" && (
              <p className="px-2 py-2 text-xs text-muted">
                No market-data match for “{trimmedQuery}”
              </p>
            )}
          </div>
        )}
      </div>
    );
  }

  // The former shell search surface is intentionally no longer mounted; all
  // record search now lives in the unified command palette.
  void renderSearch;

  /** The notifications bell + dropdown panel (M51), one markup, both homes. */
  function renderInbox(): ReactNode {
    return (
      <div ref={inboxRef} className="relative">
        <button
          type="button"
          onClick={() => setInboxOpen((open) => !open)}
          aria-expanded={inboxOpen}
          aria-controls="notifications-panel"
          aria-label={
            unread > 0 ? `Notifications, ${unread} unread` : "Notifications"
          }
          data-testid="notifications-toggle"
          className="relative flex size-11 items-center justify-center rounded-lg text-muted outline-none transition-colors hover:bg-veil/[0.06] hover:text-text focus-visible:ring-2 focus-visible:ring-accent"
        >
          <Bell className="size-5" aria-hidden />
          {badgeLabel !== "" && (
            <span className="absolute right-1 top-1 inline-flex h-4 min-w-4 items-center justify-center rounded-full border border-danger/40 bg-danger/15 px-1 text-[10px] font-semibold leading-none text-danger">
              {badgeLabel}
            </span>
          )}
        </button>
        {inboxOpen && (
          <div
            ref={inboxPanelRef}
            id="notifications-panel"
            data-testid="notifications-panel"
            tabIndex={-1}
            className="absolute right-0 top-full z-50 mt-2 max-h-[26rem] w-[19rem] overflow-y-auto rounded-xl border border-border bg-surface-raised/95 p-2 shadow-glow-sm"
          >
            <div className="flex items-center justify-between gap-2 px-2 pb-1.5 pt-1">
              <p className="text-[11px] font-semibold uppercase tracking-widest text-muted">
                Notifications
              </p>
              {unread > 0 && (
                <button
                  type="button"
                  onClick={() => markAllRead.mutate()}
                  disabled={markAllRead.isPending}
                  className="rounded text-[11px] text-accent outline-none hover:underline focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-40"
                >
                  Mark all read
                </button>
              )}
            </div>
            {inbox.isPending && (
              <p className="px-2 py-3 text-xs text-muted">Loading…</p>
            )}
            {inbox.isError && (
              <p className="px-2 py-3 text-xs text-danger">
                Couldn’t load notifications
              </p>
            )}
            {inbox.data !== undefined && inbox.data.items.length === 0 && (
              <p className="px-2 py-3 text-xs text-muted">
                Nothing needs your attention.
              </p>
            )}
            {(inbox.data?.items ?? []).map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => openInboxItem(item)}
                className="flex w-full gap-2 rounded-lg px-2 py-2 text-left outline-none transition-colors hover:bg-veil/[0.06] focus-visible:ring-2 focus-visible:ring-accent"
              >
                <span
                  className={`mt-1.5 size-2 shrink-0 rounded-full ${
                    item.severity === "critical"
                      ? "bg-danger"
                      : item.severity === "warning"
                        ? "bg-warn"
                        : "bg-accent"
                  }`}
                  aria-hidden
                />
                <span className="min-w-0 flex-1">
                  <span
                    className={`block truncate text-sm ${
                      item.read ? "text-muted" : "font-medium text-text"
                    }`}
                  >
                    {item.title}
                  </span>
                  {/* M139 (D4): the inbox BODY carries amounts inside prose,
                      so it runs through the text masker; titles and dates are
                      not amounts and stay readable. */}
                  <span className="block truncate text-xs text-muted">
                    {maskAmountsInText(item.body)}
                  </span>
                </span>
              </button>
            ))}
          </div>
        )}
      </div>
    );
  }

  /** The account menu (docs/47 §1.1): theme switch + the second mask row. */
  function renderAccountMenu(): ReactNode {
    return (
      <div ref={accountRef} className="relative">
        <button
          type="button"
          data-testid="account-menu-toggle"
          aria-expanded={accountOpen}
          aria-controls="account-menu-panel"
          aria-label={
            user !== null ? `Account menu: ${user.email}` : "Account menu"
          }
          onClick={() => setAccountOpen((open) => !open)}
          className="flex size-9 items-center justify-center rounded-full border border-control bg-veil/[0.04] text-xs font-semibold text-text outline-none transition-colors hover:border-accent/70 focus-visible:ring-2 focus-visible:ring-accent"
        >
          {(user?.email ?? "?").slice(0, 2).toUpperCase()}
        </button>
        {accountOpen && (
          // A DISCLOSURE, not a `role="menu"`: the panel is an email line, a
          // pressed toggle and two plain buttons, and a menu would promise
          // `menuitem` children plus roving arrow-key focus that this surface
          // never had (axe `aria-required-children`). Same contract as the
          // More panel: focus in on open, back to the avatar on close, Tab
          // walks out without trapping.
          <div
            ref={accountPanelRef}
            id="account-menu-panel"
            data-testid="account-menu-panel"
            tabIndex={-1}
            className="absolute right-0 top-full z-50 mt-2 w-[16rem] rounded-xl border border-border bg-surface-raised p-2 shadow-glow-sm"
          >
            {user !== null && (
              <p
                className="truncate px-3 py-1.5 text-xs text-muted"
                title={user.email}
              >
                {user.email}
              </p>
            )}
            <button
              type="button"
              onClick={toggleTheme}
              aria-label={
                theme === "dark"
                  ? "Switch to light theme"
                  : "Switch to dark theme"
              }
              className="flex min-h-11 w-full items-center gap-3 rounded-lg px-3 text-left text-sm text-muted outline-none transition-colors hover:bg-veil/[0.06] hover:text-text focus-visible:ring-2 focus-visible:ring-accent"
            >
              {theme === "dark" ? (
                <Sun className="size-[18px] shrink-0" aria-hidden />
              ) : (
                <Moon className="size-[18px] shrink-0" aria-hidden />
              )}
              <span>{theme === "dark" ? "Light mode" : "Dark mode"}</span>
            </button>
            <MaskToggleRow collapsed={false} />
            <button
              type="button"
              onClick={() => void logout()}
              aria-label="Log out"
              className="flex min-h-11 w-full items-center gap-3 rounded-lg px-3 text-left text-sm text-muted outline-none transition-colors hover:bg-danger/10 hover:text-danger focus-visible:ring-2 focus-visible:ring-danger"
            >
              <LogOut className="size-[18px] shrink-0" aria-hidden />
              <span>Log out</span>
            </button>
          </div>
        )}
      </div>
    );
  }

  /**
   * M185 §1.2 — `Add` is a REAL control: it navigates to /transactions with
   * the editor open (the reported deviation path docs/47 §1.2 allows; the
   * control is never a no-op). The Transactions page reads `?new=1`.
   */
  function openAddTransaction(): void {
    setDrawerOpen(false);
    navigate("/transactions?new=1");
  }

  const paletteTrigger = (className: string, id: string): ReactNode => (
    <button
      type="button"
      data-testid={id}
      aria-haspopup="dialog"
      aria-expanded={paletteOpen}
      aria-label={id === "command-palette-trigger" ? "Search" : "Open command palette"}
      onClick={() => {
        paletteOpenedByShortcut.current = false;
        setPaletteOpen(true);
      }}
      className={className}
    >
      <Search className="size-5" aria-hidden />
      {id === "command-palette-trigger" && (
        <span className="hidden items-center gap-1.5 lg:inline-flex">
          <span className="text-xs">Search</span>
          <kbd className="rounded border border-control px-1 py-0.5 text-[10px] font-medium">
            {isMacPlatform() ? "⌘K" : "Ctrl+K"}
          </kbd>
        </span>
      )}
    </button>
  );

  const vaultChip =
    vaultEnrollment.enrolled && !vaultUnlocked ? (
      <VaultLocked
        variant="status"
        capability="search"
        className="hidden md:flex"
      />
    ) : null;

  return (
    // M185 (docs/47 §1.3): ONE provider wraps the chrome AND the routed page,
    // so a page can publish the segments the truth bar prints (its own range,
    // its newest price_date) and the bar reads them — the segments are dead
    // without this mount, which the running-stack smoke caught.
    <PageMetaProvider>
      <div className="relative min-h-dvh">
        {/* M139 (D1 a11y): one polite live region announces every mask flip, so
          a screen-reader user hears the new state without hunting for the chip. */}
        <MaskLiveRegion />
        {/* Skip link (docs/09 Keyboard contract, WCAG 2.4.1): the first focus
          stop on every page, so the repeated chrome can be bypassed. `main`
          below carries the matching id + tabIndex={-1}. */}
        <a
          href="#main-content"
          data-testid="skip-link"
          className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-[200] focus:inline-flex focus:min-h-11 focus:items-center focus:rounded-lg focus:border focus:border-control focus:bg-surface-raised focus:px-4 focus:text-sm focus:font-medium focus:text-text focus:shadow-card focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          Skip to main content
        </a>
        {/* M56 (docs/11): the scheduled-backup runner ticks while the app is
          open — encryption is client-side, so a run needs this tab. */}
        <ScheduledBackupRunner />
        {/* M75: same ticking pattern — stores the ZIP locally when due. */}
        {user !== null && <ScheduledExportRunner userId={user.id} />}

        {isDesktop ? (
          /* Desktop chrome (docs/47 §1.1): TopBar + TruthBar as ONE sticky
           block at z-40, so page cards can never outrank it and the More
           panel's z-50 popover lives inside the bar's stacking context (the
           M68 S2b lesson). */
          <div className="sticky top-0 z-40">
            <TopBar
              moreOpen={moreOpen}
              onMoreOpenChange={setMoreOpen}
              accounts={accountItems}
              toolsEnabled={toolsEnabled}
              right={
                <>
                  {paletteTrigger(
                    "flex h-9 items-center gap-1.5 rounded-lg border border-control px-2.5 text-muted outline-none transition-colors hover:border-accent/70 hover:text-text focus-visible:ring-2 focus-visible:ring-accent",
                    "command-palette-trigger",
                  )}
                  <MaskChip className="w-auto" />
                  {vaultChip}
                  {renderInbox()}
                  {renderAccountMenu()}
                </>
              }
            />
            <TruthBar />
          </div>
        ) : (
          <>
            {drawerOpen && (
              <div
                data-testid="nav-scrim"
                aria-hidden
                onClick={() => setDrawerOpen(false)}
                className="fixed inset-0 z-40 bg-black/50 md:hidden"
              />
            )}
            {/* Mobile chrome (docs/47 §1.2): the fixed appbar carries the
              drawer toggle, brand, palette and mask switch, and the truth
              line rides directly under it — one fixed block whose height the
              `main` padding below clears. */}
            {/* `md:hidden` is belt-and-braces with the JS gate above — the
              scroll-perf contract (m101) pins it as the element that is
              opaque below md, with no alpha the removed blur used to hide. */}
            <header className="fixed inset-x-0 top-0 z-40 border-b border-rule bg-surface-raised pt-[env(safe-area-inset-top)] md:hidden">
              <div className="grid min-h-14 grid-cols-[2.75rem_minmax(0,1fr)_auto] items-center gap-2 px-2">
                <button
                  ref={toggleRef}
                  type="button"
                  data-testid="nav-drawer-toggle"
                  data-tour="nav-toggle"
                  aria-expanded={drawerOpen}
                  aria-controls="app-nav"
                  aria-label={
                    drawerOpen
                      ? "Close navigation menu"
                      : "Open navigation menu"
                  }
                  onClick={() => setDrawerOpen((open) => !open)}
                  className="flex size-11 shrink-0 items-center justify-center rounded-lg text-muted outline-none transition-colors hover:bg-veil/[0.06] hover:text-text focus-visible:ring-2 focus-visible:ring-accent"
                >
                  {drawerOpen ? (
                    <X className="size-5" aria-hidden />
                  ) : (
                    <Menu className="size-5" aria-hidden />
                  )}
                </button>
                <span className="flex min-w-0 items-center gap-2.5">
                  <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-accent to-accent-soft text-accent-ink">
                    <Zap className="size-4" aria-hidden />
                  </span>
                  <span className="truncate text-base font-semibold tracking-tight">
                    fin_tracker
                  </span>
                </span>
                <span className="flex shrink-0 items-center gap-1.5">
                  <MaskChip collapsed className="shrink-0" />
                  {paletteTrigger(
                    "flex size-11 shrink-0 items-center justify-center rounded-lg text-muted outline-none transition-colors hover:bg-veil/[0.06] hover:text-text focus-visible:ring-2 focus-visible:ring-accent",
                    "command-palette-trigger-mobile",
                  )}
                </span>
              </div>
              <TruthBar />
            </header>
          </>
        )}

        <main
          ref={mainRef}
          id="main-content"
          tabIndex={-1}
          className="min-w-0 flex-1 overflow-x-clip p-5 pt-[calc(5.5rem_+_env(safe-area-inset-top))] md:p-8 md:pt-8"
        >
          {/* M72 pull-to-refresh: the indicator floats under the header; only the
            route content below is translated, so the chrome never moves. */}
          <PullToRefreshIndicator distance={ptr.distance} phase={ptr.phase} />
          <div
            data-testid="pull-container"
            style={{ transform: `translateY(${Math.round(ptr.distance)}px)` }}
          >
            {/* Route-level entrance: subtle fade+rise on every path change. */}
            <motion.div
              key={location.pathname}
              {...routeFade}
              className="mx-auto w-full max-w-[1400px]"
            >
              <div className="pb-[calc(3.5rem_+_env(safe-area-inset-bottom))] md:pb-0">
                <Outlet />
              </div>
            </motion.div>
          </div>
        </main>

        {!isDesktop && (
          /* M72/M185 drawer: ONE <nav>, inert while parked, focus-in on open,
           Escape (via the shell router), scrim click and route change close.
           It carries the More panel's three groups plus the footer session
           controls — the artifact's `m-nav.html` hierarchy. */
          <motion.nav
            ref={navRef}
            id="app-nav"
            data-testid="app-nav"
            aria-label="Main navigation"
            tabIndex={-1}
            animate={{ x: drawerOpen ? 0 : -248 }}
            transition={{ duration: 0.3, ease: EASE }}
            className="fixed left-0 top-0 z-50 flex h-dvh w-[248px] shrink-0 flex-col border-r border-rule bg-surface-raised p-3"
          >
            <div className="mb-3 flex items-center gap-2.5 px-1.5">
              <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-accent to-accent-soft text-accent-ink">
                <Zap className="size-4" aria-hidden />
              </span>
              <span className="truncate text-base font-semibold tracking-tight">
                fin_tracker
              </span>
              <button
                type="button"
                data-testid="nav-drawer-close"
                aria-label="Close navigation menu"
                onClick={() => setDrawerOpen(false)}
                className="ml-auto flex size-11 shrink-0 items-center justify-center rounded-lg text-muted outline-none transition-colors hover:bg-veil/[0.06] hover:text-text focus-visible:ring-2 focus-visible:ring-accent"
              >
                <X className="size-5" aria-hidden />
              </button>
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto pb-2">
              <ul className="flex flex-col gap-1" data-tour="nav">
                {PRIMARY_TABS.map((item) => {
                  const Icon = item.icon;
                  return (
                    <li key={item.to}>
                      <NavLink
                        to={item.to}
                        data-tour={TOUR_TARGETS[item.to]}
                        className={({ isActive }) =>
                          `group relative flex min-h-11 items-center gap-3 rounded-lg px-3 text-sm outline-none transition-colors duration-200 focus-visible:ring-2 focus-visible:ring-accent ${
                            isActive
                              ? "text-accent"
                              : "text-muted hover:text-text"
                          }`
                        }
                      >
                        <Icon className="size-[18px] shrink-0" aria-hidden />
                        <span className="truncate">{item.label}</span>
                      </NavLink>
                    </li>
                  );
                })}
              </ul>

              <div className="mt-4 border-t border-rule pt-4">
                <p className="mb-2 px-3 text-[11px] font-semibold uppercase tracking-widest text-muted">
                  Primary
                </p>
                <ul className="flex flex-col gap-1">
                  {PRIMARY_GROUP.map((item) => {
                    const Icon = item.icon;
                    return (
                      <li key={item.to}>
                        <NavLink
                          to={item.to}
                          data-tour={TOUR_TARGETS[item.to]}
                          className={({ isActive }) =>
                            `group relative flex min-h-11 items-center gap-3 rounded-lg px-3 text-sm outline-none transition-colors duration-200 focus-visible:ring-2 focus-visible:ring-accent ${
                              isActive
                                ? "text-accent"
                                : "text-muted hover:text-text"
                            }`
                          }
                        >
                          <Icon className="size-[18px] shrink-0" aria-hidden />
                          <span className="truncate">{item.label}</span>
                        </NavLink>
                      </li>
                    );
                  })}
                </ul>
              </div>

              {toolsEnabled && (
                <div className="mt-4 border-t border-rule pt-4">
                  <p className="mb-2 px-3 text-[11px] font-semibold uppercase tracking-widest text-muted">
                    Tools
                  </p>
                  <ul className="flex flex-col gap-1">
                    {TOOLS_GROUP.map((item) => {
                      const Icon = item.icon;
                      return (
                        <li key={item.to}>
                          <NavLink
                            to={item.to}
                            className={({ isActive }) =>
                              `group relative flex min-h-11 items-center gap-3 rounded-lg px-3 text-sm outline-none transition-colors duration-200 focus-visible:ring-2 focus-visible:ring-accent ${
                                isActive
                                  ? "text-accent"
                                  : "text-muted hover:text-text"
                              }`
                            }
                          >
                            <Icon
                              className="size-[18px] shrink-0"
                              aria-hidden
                            />
                            <span className="truncate">{item.label}</span>
                          </NavLink>
                        </li>
                      );
                    })}
                  </ul>
                </div>
              )}

              <div className="mt-4 border-t border-rule pt-4">
                <h3 className="mb-2 px-3">
                  <NavLink
                    to="/accounts"
                    end
                    className={({ isActive }) =>
                      `relative flex min-h-11 items-center rounded-lg text-[11px] font-semibold uppercase tracking-widest outline-none transition-colors duration-200 focus-visible:ring-2 focus-visible:ring-accent ${
                        isActive ? "text-accent" : "text-muted hover:text-text"
                      }`
                    }
                  >
                    All accounts
                  </NavLink>
                </h3>
                {accountItems.length > 0 && (
                  <ul className="flex flex-col gap-0.5">
                    {TYPE_GROUPS.map((group) => {
                      const groupAccounts = accountItems.filter((a) =>
                        group.types.includes(a.type),
                      );
                      if (groupAccounts.length === 0) return null;
                      return (
                        <li key={group.label}>
                          <div className="px-3 py-1 text-[10px] font-medium uppercase tracking-wider text-muted">
                            {group.label}
                          </div>
                          <ul className="flex flex-col gap-0.5">
                            {groupAccounts.map((account) => (
                              <li key={account.id}>
                                <NavLink
                                  to={`/accounts/${account.id}`}
                                  className={({ isActive }) =>
                                    `relative flex min-h-11 items-center gap-2 rounded-lg px-3 text-sm outline-none transition-colors duration-200 focus-visible:ring-2 focus-visible:ring-accent ${
                                      isActive
                                        ? "text-accent"
                                        : "text-muted hover:text-text"
                                    }`
                                  }
                                >
                                  <span className="min-w-0 flex-1 truncate">
                                    {account.name}
                                  </span>
                                  <span className="shrink-0 text-[11px] text-muted">
                                    {formatMoney(
                                      account.total_value,
                                      account.currency,
                                    )}
                                  </span>
                                </NavLink>
                              </li>
                            ))}
                          </ul>
                        </li>
                      );
                    })}
                  </ul>
                )}
              </div>
            </div>

            {/* Footer session controls — the artifact's drawer hierarchy. */}
            <div className="mt-auto shrink-0 space-y-1 border-t border-rule pb-14 pt-3">
              {renderInbox()}
              {vaultChip}
              {user !== null && (
                <div
                  className="truncate px-3 pb-1 text-xs text-muted"
                  title={user.email}
                >
                  {user.email}
                </div>
              )}
              <button
                type="button"
                onClick={() => void logout()}
                aria-label="Log out"
                className="flex min-h-11 w-full items-center gap-3 rounded-lg px-3 text-left text-sm text-muted outline-none transition-colors hover:bg-danger/10 hover:text-danger focus-visible:ring-2 focus-visible:ring-danger"
              >
                <LogOut className="size-[18px] shrink-0" aria-hidden />
                <span>Log out</span>
              </button>
              <button
                type="button"
                onClick={toggleTheme}
                aria-label={
                  theme === "dark"
                    ? "Switch to light theme"
                    : "Switch to dark theme"
                }
                className="flex min-h-11 w-full items-center gap-3 rounded-lg px-3 text-left text-sm text-muted outline-none transition-colors hover:bg-veil/[0.06] hover:text-text focus-visible:ring-2 focus-visible:ring-accent"
              >
                {theme === "dark" ? (
                  <Sun className="size-[18px] shrink-0" aria-hidden />
                ) : (
                  <Moon className="size-[18px] shrink-0" aria-hidden />
                )}
                <span>{theme === "dark" ? "Light mode" : "Dark mode"}</span>
              </button>
              <MaskToggleRow collapsed={false} />
            </div>
          </motion.nav>
        )}

        {!isDesktop && (
          <MobileTabBar
            onAdd={openAddTransaction}
            onMore={() => setDrawerOpen(true)}
            moreOpen={drawerOpen}
          />
        )}
        <Tour user={user} />
        {/* M95 P1 — the ⌘K palette: one Suspense boundary, mounted only while
          open so its chunk costs nothing until the first invocation. */}
        {paletteOpen && (
          <Suspense fallback={null}>
            <CommandPalette
              onClose={closePalette}
              savedViews={user?.saved_report_views}
            />
          </Suspense>
        )}
      </div>
    </PageMetaProvider>
  );
}
