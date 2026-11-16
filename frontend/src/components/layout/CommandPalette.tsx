// M95 P1 — the global command palette, ⌘K / Ctrl+K (docs/09 "Command
// palette").
//
// Design rules the card pins, all enforced here or in AppShell:
//   * EXISTING routes only — the route table is the data source, no new
//     backend endpoint and no fetch of any kind;
//   * one plain case-insensitive prefix/substring matcher, zero fuzzy deps;
//   * keyboard-first: arrows move, Enter goes, Escape closes — Escape is NOT
//     handled here, the shell's single keydown router owns it (AppShell
//     `escapeState`; the palette registers as one more consumer);
//   * dialog contract from `dialogA11y` (focus in on open, focus back to the
//     invoking element on close, no keyboard trap — `aria-modal` marks the
//     rest inert, per docs/09 §Keyboard contract);
//   * the component ships in its OWN chunk (React.lazy from AppShell): the
//     guide registry below is page copy, and the entry chunk has a 700,000 B
//     ceiling (docs/09 §Bundle facts).

import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { motion, useReducedMotion } from "framer-motion";
import {
  ArrowLeftRight,
  Bookmark,
  ChartColumn,
  CircleHelp,
  FileUp,
  KeyRound,
  PlayCircle,
  Plus,
  Search,
  Settings as SettingsIcon,
  Target,
  Wallet,
  TrendingUp,
  Users,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { GUIDES } from "../../pages/helpGuides";
import { REPORT_TABS } from "../../lib/reportTabs";
import { criteriaToParams, requestCreateFromSearch } from "../../lib/createFromSearch";
import type { SavedReportView } from "../../types";
import { requestTour } from "../tour/tourState";
import { useDialogFocus } from "../ui/dialogA11y";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchGlobalSearch } from "../../lib/globalSearchQuery";
import { useVaultUnlocked } from "../e2ee/useUnlock";
import { VaultLocked } from "../e2ee/VaultLocked";
import { isUnlocked } from "../../lib/vault";

export type PaletteGroup = "Page" | "Report" | "Guide" | "Action" | "View" | "Transaction" | "Category" | "Account" | "Security" | "Tag";

export interface PaletteEntry {
  /** Stable, unique — also the id of its listbox option in the DOM. */
  id: string;
  label: string;
  group: PaletteGroup;
  icon: LucideIcon;
  /** Extra haystack for the match (route, slug, synonyms). */
  keywords?: string;
  /** Navigate here… */
  to?: string;
  /** …or run this instead (actions with no route of their own). */
  run?: () => void;
}

/** What the palette knows about WHERE it was opened (M123 additions only). */
export interface PaletteContext {
  /** Current pathname — gates entries that only make sense on one surface. */
  pathname?: string;
  /** M123: saved transaction views, offered as restore entries. */
  savedViews?: SavedReportView[];
}

/**
 * The registry, in priority order: an empty query shows the FIRST 8 entries
 * verbatim — the seven navigate-to pages (M100 added Accounts, which is why
 * the eighth slot is now `Import a statement` and the tour entry sits at #9).
 * Every entry points at a route the app already serves.
 *
 * The M123 entries come LAST so the empty-query default is untouched: they are
 * contextual (one path) or per-user (N rows), not part of the fixed registry.
 */
export function paletteEntries(context: PaletteContext = {}): PaletteEntry[] {
  const pages: PaletteEntry[] = [
    { id: "page-transactions", label: "Transactions", group: "Page", icon: ArrowLeftRight, to: "/transactions", keywords: "ledger rows" },
    { id: "page-budgets", label: "Budgets", group: "Page", icon: Wallet, to: "/budgets", keywords: "spending limits" },
    { id: "page-reports", label: "Reports", group: "Page", icon: ChartColumn, to: "/reports", keywords: "charts graphs" },
    { id: "page-goals", label: "Goals", group: "Page", icon: Target, to: "/goals", keywords: "savings targets" },
    { id: "page-settings", label: "Settings", group: "Page", icon: SettingsIcon, to: "/settings", keywords: "account preferences" },
    { id: "page-help", label: "Help", group: "Page", icon: CircleHelp, to: "/help", keywords: "guides docs support" },
    // M100: the accounts index joins the registry so ⌘K reaches the page the
    // rail's Accounts entries open (cmd-K registers routes — a page nobody
    // can reach is an orphan). Appended last among pages so every existing
    // entry keeps its rank in the empty-query default list.
    { id: "page-accounts", label: "Accounts", group: "Page", icon: Wallet, to: "/accounts", keywords: "balances chequing savings registered institutions" },
  ];
  const actions: PaletteEntry[] = [
    {
      id: "action-import",
      label: "Import a statement",
      group: "Action",
      icon: FileUp,
      to: "/imports",
      keywords: "upload csv ofx qif bank file",
    },
    {
      id: "action-tour",
      label: "Take the tour",
      group: "Action",
      icon: PlayCircle,
      keywords: "walkthrough coach marks",
      run: requestTour,
    },
    {
      id: "action-mcp-token",
      label: "Mint an MCP token",
      group: "Action",
      icon: KeyRound,
      // The card's "settings section anchor" does not exist: minting is an
      // API action (docs/40) and Settings has no token surface. The guide IS
      // the entry point the Help hub's third track already links to.
      to: "/help/agent-integration",
      keywords: "agent token api key",
    },
  ];
  const reports: PaletteEntry[] = REPORT_TABS.map(({ id, label }) => ({
    id: `report-${id}`,
    label,
    group: "Report",
    icon: ChartColumn,
    to: `/reports?tab=${id}`,
    keywords: `report ${id}`,
  }));
  const guides: PaletteEntry[] = GUIDES.map((guide) => ({
    id: `guide-${guide.slug}`,
    label: guide.title,
    group: "Guide",
    icon: CircleHelp,
    to: `/help/${guide.slug}`,
    keywords: `${guide.slug} ${guide.blurb}`,
  }));
  // M123 C2 (docs/09): offered ONLY on the one surface that OWNS a search, so
  // the action can never be a dead click. It hands the open menu back to the
  // Transactions page rather than navigating — the live `applied` state may be
  // newer than the URL, and the page is the thing that holds it.
  const createAction: PaletteEntry[] =
    context.pathname === "/transactions"
      ? [
          {
            id: "action-create-from-search",
            label: "Create from this search",
            group: "Action",
            icon: Plus,
            keywords: "rule bill budget saved view categorize filter",
            run: requestCreateFromSearch,
          },
        ]
      : [];
  // M123 C1: the saved transaction views, each restoring its own bookmark URL.
  const views: PaletteEntry[] = (context.savedViews ?? [])
    .filter((view) => view.tab === "transactions")
    .map((view) => {
      const query = criteriaToParams(view.state).toString();
      return {
        id: `view-${view.id}`,
        label: view.title,
        group: "View" as const,
        icon: Bookmark,
        to: query === "" ? "/transactions" : `/transactions?${query}`,
        keywords: `saved view transactions ${view.state.q ?? ""}`.trim(),
      };
    });
  return [...pages, ...actions, ...createAction, ...views, ...reports, ...guides];
}

/** 0 label-prefix · 1 word-prefix · 2 label substring · 3 keyword/group · -1 no. */
function matchRank(entry: PaletteEntry, needle: string): number {
  const label = entry.label.toLowerCase();
  if (label.startsWith(needle)) return 0;
  if (label.split(/\s+/).some((word) => word.startsWith(needle))) return 1;
  if (label.includes(needle)) return 2;
  const haystack = `${entry.group} ${entry.keywords ?? ""}`.toLowerCase();
  if (haystack.includes(needle)) return 3;
  return -1;
}

/**
 * Plain case-insensitive prefix-then-substring match — deliberately NOT fuzzy
 * (the card keeps deps at zero; typo tolerance is out of scope).
 * An empty/blank query returns the top `defaultCount` entries in registry order.
 */
export function matchPaletteEntries(
  entries: PaletteEntry[],
  query: string,
  defaultCount = 8,
): PaletteEntry[] {
  const needle = query.trim().toLowerCase();
  if (needle === "") return entries.slice(0, defaultCount);
  const ranked = entries
    .map((entry, index) => ({ entry, index, rank: matchRank(entry, needle) }))
    .filter((hit) => hit.rank >= 0)
    .sort((a, b) => a.rank - b.rank || a.index - b.index);
  return ranked.map((hit) => hit.entry);
}

/** Visible label for the group word shown at the right of every row. */
function groupHint(group: PaletteGroup): string {
  return group;
}

export function CommandPalette({
  onClose,
  savedViews,
}: {
  onClose: () => void;
  /** The signed-in user's saved views (docs/09 §Create from search). */
  savedViews?: SavedReportView[];
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const panelRef = useRef<HTMLDivElement>(null);
  const optionRefs = useRef(new Map<string, HTMLLIElement>());
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [active, setActive] = useState(0);
  const reduced = useReducedMotion();
  const vaultUnlocked = useVaultUnlocked();
  const queryClient = useQueryClient();
  const liveVaultUnlocked = useRef(vaultUnlocked);
  liveVaultUnlocked.current = vaultUnlocked;

  useEffect(() => {
    const clearSearchOnLock = (): void => {
      if (isUnlocked()) return;
      liveVaultUnlocked.current = false;
      void queryClient.cancelQueries({ queryKey: ["search"] });
      queryClient.removeQueries({ queryKey: ["search"] });
    };
    if (!vaultUnlocked) clearSearchOnLock();
    window.addEventListener("vaultchange", clearSearchOnLock);
    return () => window.removeEventListener("vaultchange", clearSearchOnLock);
  }, [queryClient, vaultUnlocked]);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedQuery(query), 200);
    return () => window.clearTimeout(timer);
  }, [query]);

  const trimmedQuery = debouncedQuery.trim();
  const search = useQuery({
    // docs/09 "Query keys": the shared global-search pair. AppShell's app-bar
    // panel declares the SAME key and delegates to the same fetch function —
    // one cached search for both surfaces, pinned by queryKeyShapes.
    queryKey: ["search", trimmedQuery, vaultUnlocked],
    // Search is an interactive, debounced palette request. Surface a failed
    // request immediately rather than inheriting React Query's global retry
    // policy, which can leave the palette showing "Searching…" through a
    // long backoff after a server rejection.
    retry: false,
    queryFn: ({ signal }) =>
      fetchGlobalSearch(trimmedQuery, vaultUnlocked, liveVaultUnlocked, signal),
    enabled: trimmedQuery.length >= 2,
  });

  // Focus moves into the panel on mount and returns to the invoking element
  // when the palette unmounts (dialogA11y — one implementation, every surface).
  useDialogFocus(panelRef, true);
  const inputRef = useRef<HTMLInputElement>(null);
  // The dialog contract focuses the CONTAINER so Tab walks the surface in DOM
  // order; a palette is a typing surface, so the input takes focus on the pass
  // right after (docs/09 allows the focus move inside a dialog — the container
  // keeps the opener bookkeeping, the input keeps the keystrokes).
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const entries = useMemo(
    () => paletteEntries({ pathname: location.pathname, savedViews }),
    [location.pathname, savedViews],
  );
  const localResults = useMemo(() => matchPaletteEntries(entries, query), [entries, query]);
  const recordResults: PaletteEntry[] = useMemo(() => {
    const data = search.data;
    if (data === undefined) return [];
    return [
      ...data.transactions
        .filter((row) => vaultUnlocked || row.encrypted === false)
        .map((row) => ({ id: `record-transaction-${row.id}`, label: row.normalized_payee, group: "Transaction" as const, icon: ArrowLeftRight, keywords: row.category_name ?? "", to: `/transactions?q=${encodeURIComponent(row.normalized_payee)}` })),
      ...data.categories.map((row) => ({ id: `record-category-${row.id}`, label: row.name, group: "Category" as const, icon: ChartColumn, to: `/transactions?category=${row.id}` })),
      ...data.accounts.map((row) => ({ id: `record-account-${row.id}`, label: row.name, group: "Account" as const, icon: Wallet, keywords: row.type, to: `/transactions?account=${row.id}` })),
      ...data.securities.map((row) => ({ id: `record-security-${row.id}`, label: row.symbol, group: "Security" as const, icon: TrendingUp, keywords: `${row.name} ${row.exchange}`, to: `/securities/${row.id}` })),
      ...(data.tags ?? []).map((row) => ({ id: `record-tag-${row.id}`, label: row.name, group: "Tag" as const, icon: Users, to: "/tags" })),
    ];
  }, [search.data, vaultUnlocked]);
  const visibleLocalResults =
    localResults.length === 0 && recordResults.length > 0
      ? matchPaletteEntries(entries, "").slice(0, 1)
      : localResults;
  const results = [...visibleLocalResults, ...recordResults];
  const hasRecordResults = recordResults.length > 0;
  const noRecordResults = search.data !== undefined && !hasRecordResults;
  const withheld = (search.data?.withheld ?? 0) > 0;

  // A shorter result list must never leave the cursor past its end.
  useEffect(() => {
    setActive((current) => (current >= results.length ? 0 : current));
  }, [results.length]);

  // Keep the active option inside the scroll box (nearest, never forced).
  useEffect(() => {
    const entry = results[active];
    if (entry === undefined) return;
    optionRefs.current.get(entry.id)?.scrollIntoView?.({ block: "nearest" });
  }, [active, results]);

  function run(entry: PaletteEntry): void {
    onClose();
    if (entry.to !== undefined) navigate(entry.to);
    else entry.run?.();
  }

  function onKeyDown(event: React.KeyboardEvent): void {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((current) => (results.length === 0 ? 0 : (current + 1) % results.length));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((current) => (results.length === 0 ? 0 : (current - 1 + results.length) % results.length));
    } else if (event.key === "Enter") {
      const entry = results[active];
      if (entry !== undefined) {
        event.preventDefault();
        run(entry);
      }
    }
    // Escape is deliberately NOT handled: the shell's single keydown router
    // (AppShell `escapeState`) closes the palette first, then the inbox,
    // search overlay and drawer, one layer per press.
  }

  const activeEntry = results[active];

  return (
    <div
      // Scrim: pointer-down outside the panel closes it (the Escape half
      // belongs to the shell router).
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      className="fixed inset-0 z-[120] flex items-start justify-center bg-scrim p-4 pt-[10vh] backdrop-blur-sm"
    >
      <div
        ref={panelRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby="command-palette-label"
        data-testid="command-palette"
        onKeyDown={onKeyDown}
        className="glass-strong w-full max-w-lg overflow-hidden rounded-xl border border-border shadow-card outline-none focus-visible:ring-2 focus-visible:ring-accent"
      >
        {/* Opacity only — no slide (docs/09 reduced-motion contract). */}
        <motion.div
          initial={reduced ? { opacity: 1 } : { opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ duration: 0.12, ease: "easeOut" }}
        >
          <div className="flex items-center gap-2 border-b border-veil/[0.06] px-3">
            <Search className="size-4 shrink-0 text-muted" aria-hidden />
            <label className="sr-only" htmlFor="command-palette-input" id="command-palette-label">
              Search pages, records, reports, guides and actions
            </label>
            <input
              ref={inputRef}
              id="command-palette-input"
              data-testid="command-palette-input"
              role="combobox"
              aria-expanded="true"
              aria-controls="command-palette-listbox"
              aria-activedescendant={activeEntry?.id !== undefined ? `palette-option-${activeEntry.id}` : undefined}
              aria-autocomplete="list"
              autoComplete="off"
              spellCheck={false}
              value={query}
              onChange={(event) => {
                setQuery(event.target.value);
                setActive(0);
              }}
              placeholder="Search pages, transactions, accounts…"
              className="w-full bg-transparent py-3 text-sm text-text placeholder:text-muted"
            />
          </div>

          <ul
            id="command-palette-listbox"
            role="listbox"
            aria-label="Command palette results"
            data-testid="command-palette-listbox"
            className="max-h-[50vh] overflow-y-auto p-2"
          >
            {results.map((entry, index) => {
              const Icon = entry.icon;
              const selected = index === active;
              return (
                /* NO nested <button> here (M95 axe fix): `role="option"` may not
                   contain an interactive descendant, and the inner button made
                   axe's `nested-interactive` fire on all 8 default rows — a
                   SERIOUS violation of the card's "axe clean" contract. The li
                   itself carries the pointer handlers; the listbox semantics
                   (combobox + aria-activedescendant) carry keyboard and
                   screen-reader, so the button had no job the li cannot do. */
                <li
                  key={entry.id}
                  id={`palette-option-${entry.id}`}
                  ref={(node) => {
                    if (node) optionRefs.current.set(entry.id, node);
                    else optionRefs.current.delete(entry.id);
                  }}
                  role="option"
                  aria-selected={selected}
                  onMouseMove={() => setActive(index)}
                  onClick={() => run(entry)}
                  className={`flex min-h-11 cursor-pointer items-center gap-2.5 rounded-lg px-2.5 py-2 text-left transition-colors ${
                    selected ? "bg-accent/10 text-text" : "text-text hover:bg-veil/[0.06]"
                  }`}
                >
                  <Icon className="size-4 shrink-0 text-muted" aria-hidden />
                  <span className="min-w-0 flex-1 truncate text-sm">{entry.label}</span>
                  <span className="shrink-0 text-[11px] text-muted">{groupHint(entry.group)}</span>
                </li>
              );
            })}
            {trimmedQuery.length >= 2 && search.isPending && (
              <li role="presentation" className="px-2.5 py-2 text-xs text-muted">
                <span role="status" aria-live="polite" data-testid="global-search-loading">Searching…</span>
              </li>
            )}
            {trimmedQuery.length >= 2 && search.isError && (
              <li role="presentation" className="px-2.5 py-2 text-xs text-danger">
                <span role="status" aria-live="polite" data-testid="global-search-error">Search failed</span>
              </li>
            )}
            {noRecordResults && withheld && (
              <li role="presentation" data-testid="global-search-withheld"><VaultLocked variant="blocked" capability="search" className="px-2.5 py-2" /></li>
            )}
            {trimmedQuery.length >= 2 && visibleLocalResults.length > 0 && noRecordResults && !withheld && !search.isError && !search.isPending && (
              <li role="presentation" className="px-2.5 py-2 text-xs text-muted" data-testid="global-search-empty">No matching records</li>
            )}
            {results.length === 0 && trimmedQuery.length < 2 && (
              <li role="presentation" className="px-2.5 py-3 text-sm text-muted" data-testid="command-palette-empty">
                No match — Try Import or Help
              </li>
            )}
            {results.length === 0 && trimmedQuery.length >= 2 && noRecordResults && !withheld && !search.isPending && !search.isError && (
              <li role="presentation" className="px-2.5 py-3 text-sm text-muted" data-testid="command-palette-empty">No matches</li>
            )}
          </ul>

          <div className="flex items-center justify-between gap-2 border-t border-veil/[0.06] px-3 py-2 text-[11px] text-muted">
            <span>↑↓ move · Enter go · Esc close</span>
            <span>{results.length === 1 ? "1 match" : `${results.length} matches`}</span>
          </div>
        </motion.div>
      </div>
    </div>
  );
}

export default CommandPalette;
