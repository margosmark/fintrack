// M68 — internal jargon must never reach rendered copy (docs/09 "Naming &
// semantics"). Two independent channels:
//
//  1. DOM: render the real pages and assert their actual textContent — this is
//     the acceptance criterion ("zero /docs/\d+ references in rendered
//     main-content text … DOM-asserted in a vitest test"), extended to the
//     other leaks in the same finding (`pending_review`, `yfinance`,
//     "bucket/category rows").
//  2. Source: an AST walk over every JSX text node and string literal in
//     `src/**`. Comments are not AST nodes, so a `(docs/NN)` reference in a
//     comment stays legal while one inside copy fails. This channel covers
//     branches the DOM cannot reach under jsdom (feature gates backed by
//     IndexedDB) and interaction-only toasts.
import type { ReactElement } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import ts from "typescript";

import { AuthProvider } from "../src/auth/AuthContext";
import { ToastProvider } from "../src/components/ui/Toast";
import { ThemeProvider } from "../src/theme/ThemeProvider";
import { Imports } from "../src/pages/Imports";
import { Investments } from "../src/pages/Investments";
import { Reports } from "../src/pages/Reports";
import { Settings } from "../src/pages/Settings";
import { Taxes } from "../src/pages/Taxes";

const h = vi.hoisted(() => {
  const user = {
    id: "user-1",
    email: "demo@fintracker.local",
    privacy_mode: false,
    currency_tracker_enabled: false,
    llm_provider: "openai",
    llm_base_url: null,
    llm_model: "gpt-4o-mini",
    encryption_required: false,
    dashboard_preset: "overview",
  };
  const brokerage = {
    id: 1,
    institution_id: null,
    name: "Margin account",
    type: "brokerage_non_registered",
    currency: "CAD",
    taxable: true,
    llm_autopilot: false,
    cash_balance: "0.00",
    investment_value: "0.00",
    total_value: "0.00",
    starting_balance: "0.00",
    name_cipher: null,
  };
  const page = <T,>(items: T[]) => ({ items, total: items.length, limit: 200, offset: 0 });
  return {
    restoreSession: vi.fn(),
    me: vi.fn(),
    listAccounts: vi.fn(),
    listCategories: vi.fn(),
    listInstitutions: vi.fn(),
    listImports: vi.fn(),
    getSpending: vi.fn(),
    getCapitalGains: vi.fn(),
    listAcbPools: vi.fn(),
    getPositions: vi.fn(),
    getPortfolioValueHistory: vi.fn(),
    getWatchlist: vi.fn(),
    listSecurities: vi.fn(),
    listTrades: vi.fn(),
    getDividendSuggestions: vi.fn(),
    user,
    brokerage,
    page,
  };
});

vi.mock("../src/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/client")>();
  return { ...actual, restoreSession: h.restoreSession, me: h.me };
});
vi.mock("../src/api/accounts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/accounts")>();
  const { useQuery } = await import("@tanstack/react-query");
  return {
    ...actual,
    listAccounts: h.listAccounts,
    // The real hook calls `listAccounts` through accounts.ts's OWN module
    // binding, which a vi.mock does not intercept — so it would fetch for real
    // and land in an error state. Rebuild the hook on top of the mock.
    useInvestmentAccounts: () => {
      const accounts = useQuery({
        queryKey: ["accounts", 200],
        queryFn: () => h.listAccounts(200),
      });
      const investmentAccounts = (accounts.data?.items ?? []).filter(
        (a) => actual.INVESTMENT_ACCOUNT_TYPES[a.type] === true,
      );
      return { accounts, investmentAccounts };
    },
  };
});
vi.mock("../src/api/categories", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/categories")>();
  return { ...actual, listCategories: h.listCategories };
});
vi.mock("../src/api/institutions", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/institutions")>();
  return { ...actual, listInstitutions: h.listInstitutions };
});
vi.mock("../src/api/imports", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/imports")>();
  return { ...actual, listImports: h.listImports };
});
vi.mock("../src/api/reports", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/reports")>();
  return { ...actual, getSpending: h.getSpending, getCapitalGains: h.getCapitalGains };
});
vi.mock("../src/api/acb", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/acb")>();
  return { ...actual, listAcbPools: h.listAcbPools };
});
vi.mock("../src/api/portfolio", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/portfolio")>();
  return { ...actual, getPositions: h.getPositions, getPortfolioValueHistory: h.getPortfolioValueHistory };
});
vi.mock("../src/api/watchlist", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/watchlist")>();
  return { ...actual, getWatchlist: h.getWatchlist };
});
vi.mock("../src/api/securities", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/securities")>();
  return { ...actual, listSecurities: h.listSecurities };
});
vi.mock("../src/api/trades", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/trades")>();
  return {
    ...actual,
    listTrades: h.listTrades,
    getDividendSuggestions: h.getDividendSuggestions,
  };
});

/** jsdom has no matchMedia (ThemeProvider reads prefers-color-scheme). */
Object.defineProperty(window, "matchMedia", {
  configurable: true,
  writable: true,
  value: (query: string) => ({
    matches: /min-width:\s*(\d+)px/.test(query) ? window.innerWidth >= 768 : false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }),
});

/** Render one page exactly as the shell does (guarded route → AppShell → page). */
function renderPage(ui: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <QueryClientProvider client={client}>
        <ThemeProvider>
          <ToastProvider>
            <AuthProvider>
              <Routes>
                <Route path="*" element={ui} />
              </Routes>
            </AuthProvider>
          </ToastProvider>
        </ThemeProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

const FORBIDDEN: RegExp[] = [/docs\/\d+/, /pending_review/, /\byfinance\b/i, /bucket\/category/];

/** The rendered text of a surface, minus nothing — the real DOM. */
function renderedText(container: HTMLElement): string {
  return container.textContent ?? "";
}

function expectNoJargon(container: HTMLElement, label: string): void {
  const text = renderedText(container);
  for (const pattern of FORBIDDEN) {
    expect(
      pattern.test(text),
      `${label} rendered jargon ${pattern}:\n${text.slice(0, 600)}`,
    ).toBe(false);
  }
}

beforeEach(() => {
  h.restoreSession.mockResolvedValue({
    access_token: "token",
    token_type: "bearer",
    user: h.user,
  });
  h.me.mockResolvedValue(h.user);
  h.listAccounts.mockResolvedValue(h.page([h.brokerage]));
  h.listCategories.mockResolvedValue(h.page([]));
  h.listInstitutions.mockResolvedValue(h.page([]));
  h.listImports.mockResolvedValue(
    h.page([
      {
        id: 7,
        account_id: 1,
        institution: "Test Bank",
        file_name: "statement.csv",
        status: "pending_review",
        imported: 12,
        duplicates_skipped: 1,
        unresolved: 0,
      },
    ]),
  );
  h.getSpending.mockResolvedValue({
    from: "2026-01-01",
    to: "2026-01-31",
    granularity: "month",
    items: [
      { bucket: "2026-01-15", category_id: 1, category_name: "Groceries", total_cad: "-80.00" },
    ],
    total_cad: "-80.00",
  });
  h.getCapitalGains.mockResolvedValue({
    year: 2026,
    items: [],
    totals: {
      proceeds_cad: "0.00",
      acb_cad: "0.00",
      gain_loss_cad: "0.00",
      outlays_commissions_cad: "0.00",
    },
    denials: [
      {
        security_id: 3,
        symbol: "BMO.TO",
        sale_date: "2026-03-04",
        denied_loss_cad: "-120.00",
        outcome: "deferral",
        acquiring_account_id: null,
      },
    ],
    disclaimer: "Computed under the average cost method.",
  });
  h.listAcbPools.mockResolvedValue(h.page([]));
  h.getPositions.mockResolvedValue({ items: [], total: 0 });
  h.getWatchlist.mockResolvedValue({ items: [], total: 0 });
  h.listSecurities.mockResolvedValue(h.page([]));
  h.listTrades.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  h.getDividendSuggestions.mockResolvedValue([
    {
      security_id: 3,
      account_id: 1,
      symbol: "BMO.TO",
      date: "2026-09-15",
      amount: "0.55",
      quantity: "100",
      gross_amount: "55.00",
      currency: "CAD",
    },
  ]);
});

describe("rendered copy carries no internal jargon", () => {
  it("Imports: no docs/03 reference, no pending_review token", async () => {
    const { container } = renderPage(<Imports />);

    // Static copy + the batch row are present, so the assertion below is not
    // vacuous.
    await waitFor(() =>
      expect(renderedText(container)).toContain(
        "Statements land in a batch marked Review",
      ),
    );
    expect(renderedText(container)).toContain("generic-CSV importer");
    // Data-dependent: the batch row lands when the list query resolves.
    await waitFor(() => expect(renderedText(container)).toContain("statement.csv"));
    // The batch enum renders as its display label in the row's status badge,
    // never the raw value ("Review" also appears as a wizard step label).
    const row = screen.getByText("statement.csv").closest("li");
    expect(row).not.toBeNull();
    expect((row as HTMLElement).textContent).toContain("Review");
    expect((row as HTMLElement).textContent).not.toContain("pending");

    expectNoJargon(container, "Imports");
  });

  it("Imports empty state: the first-batch hint is clean too", async () => {
    h.listImports.mockResolvedValue(h.page([]));
    const { container } = renderPage(<Imports />);

    await waitFor(() =>
      expect(renderedText(container)).toContain("marked Review until you commit it"),
    );

    expectNoJargon(container, "Imports (empty)");
  });

  it("Investments: no docs/02 reference, no yfinance", async () => {
    const { container } = renderPage(<Investments />);

    await waitFor(() =>
      expect(renderedText(container)).toContain("stored negative automatically"),
    );
    await waitFor(() =>
      expect(renderedText(container)).toContain("Pending dividends from market data"),
    );

    expectNoJargon(container, "Investments");
  });

  it("Reports: spending summary counts data points, not bucket/category rows", async () => {
    const { container } = renderPage(<Reports />);

    await waitFor(() => expect(renderedText(container)).toContain("data points"));
    expect(renderedText(container)).toContain("Range total:");

    expectNoJargon(container, "Reports");
  });

  it("Taxes: denial panel copy is clean", async () => {
    const { container } = renderPage(<Taxes />);

    await waitFor(() =>
      expect(renderedText(container)).toContain("Superficial-loss denials"),
    );

    expectNoJargon(container, "Taxes");
    // The rebuild tooltip is a DOM attribute — same rule, different channel.
    const rebuild = screen.getByRole("button", { name: "Rebuild pools" });
    expect(rebuild.getAttribute("title") ?? "").not.toMatch(/docs\/\d+/);
  });

  it("Settings: privacy, backup and accounts copy are clean", async () => {
    const { container } = renderPage(<Settings />);

    await waitFor(() =>
      expect(renderedText(container)).toContain("Blocks external calls"),
    );
    expect(renderedText(container)).toContain("cash balances are committed transactions");

    expectNoJargon(container, "Settings");
  });
});

describe("source-wide jargon scan (AST, comments excluded)", () => {
  const SRC = path.resolve("src");

  interface Literal {
    file: string;
    line: number;
    text: string;
    /** `copy` = it can be read on screen; `value` = enum/type/comparison use. */
    kind: "copy" | "value";
  }

  /**
   * Every literal-ish node in `src/**`. Comments are not AST nodes, so a
   * `(docs/NN)` reference in a comment stays legal while one inside copy
   * fails. `copy` marks the ones a user can actually read: JSX text, JSX
   * attribute strings (`title=`, `hint=`, `placeholder=`) and templates
   * interpolated into JSX.
   */
  function collectLiterals(): Literal[] {
    const out: Literal[] = [];
    const walk = (dir: string): void => {
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) {
          walk(full);
          continue;
        }
        if (!/\.tsx?$/.test(entry.name)) continue;
        const source = readFileSync(full, "utf8");
        const sf = ts.createSourceFile(
          full,
          source,
          ts.ScriptTarget.Latest,
          true,
          full.endsWith("x") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
        );
        // Readable only when the literal IS the JSX child expression or the
        // attribute value — `batch.status === "pending_review"` sits inside a
        // JSX expression but is a comparison, not copy.
        const inJsx = (node: ts.Node): boolean => {
          const parent = node.parent;
          if (parent === undefined) return false;
          if (ts.isJsxAttribute(parent)) return true;
          if (ts.isJsxExpression(parent)) {
            const grand = parent.parent;
            return (
              grand !== undefined &&
              (ts.isJsxElement(grand) ||
                ts.isJsxFragment(grand) ||
                ts.isJsxSelfClosingElement(grand) ||
                ts.isJsxAttribute(grand))
            );
          }
          return false;
        };
        const push = (node: ts.Node, text: string, kind: Literal["kind"]): void => {
          const { line } = sf.getLineAndCharacterOfPosition(node.getStart(sf));
          out.push({ file: path.relative(SRC, full), line: line + 1, text, kind });
        };
        const visit = (node: ts.Node): void => {
          if (ts.isJsxText(node)) push(node, node.text, "copy");
          else if (ts.isStringLiteral(node))
            push(node, node.text, ts.isJsxAttribute(node.parent) ? "copy" : inJsx(node) ? "copy" : "value");
          else if (ts.isNoSubstitutionTemplateLiteral(node))
            push(node, node.text, inJsx(node) ? "copy" : "value");
          else if (ts.isTemplateExpression(node)) {
            push(node.head, node.head.text, inJsx(node) ? "copy" : "value");
            for (const span of node.templateSpans)
              push(span.literal, span.literal.text, inJsx(node) ? "copy" : "value");
          }
          ts.forEachChild(node, visit);
        };
        visit(sf);
      }
    };
    walk(SRC);
    return out;
  }
  const where = (l: Literal): string => `${l.file}:${l.line} "${l.text.trim().slice(0, 80)}"`;

  it("has no docs/NN reference in any JSX text or string literal", () => {
    // `docs/NN` is a spec pointer, never product copy — but it does belong in
    // comments, which the AST walk skips by construction.
    expect(collectLiterals().filter((l) => /docs\/\d+/.test(l.text)).map(where)).toEqual([]);
  });

  it("has no user-readable pending_review / yfinance / bucket-category text", () => {
    const hits = collectLiterals().filter(
      (l) => l.kind === "copy" && /pending_review|\byfinance\b|bucket\/category/i.test(l.text),
    );
    expect(hits.map(where)).toEqual([]);
  });
});
