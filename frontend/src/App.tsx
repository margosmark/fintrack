import { lazy, Suspense, type ComponentType } from "react";
import { Navigate, Outlet, RouterProvider, createBrowserRouter, useLocation } from "react-router-dom";
import { MotionConfig, motion } from "framer-motion";
import { Zap } from "lucide-react";
import { AuthProvider, useAuth } from "./auth/AuthContext";
import { ToastProvider } from "./components/ui/Toast";
import { splashPulse } from "./components/ui/motion";
import { AppShell } from "./components/layout/AppShell";
import { RouteFallback, RouteLoadError } from "./components/ui/RouteFallback";

/**
 * One route = one lazily-loaded page module (docs/09 "Route loading &
 * code-splitting"): `React.lazy` inside a per-route `Suspense`, with the shimmer
 * `RouteFallback` while the chunk loads and `RouteLoadError` if it fails (a
 * chunk 404 after a deploy must not blank the page). The shell and auth stay
 * eager — only page modules are split. `tests/routeCodeSplit.test.tsx` fails if
 * a file in `src/pages/` is wired eagerly or not wired at all.
 */
export function route(load: () => Promise<{ default: ComponentType }>) {
  const Page = lazy(load);
  return (
    <RouteLoadError>
      <Suspense fallback={<RouteFallback />}>
        <Page />
      </Suspense>
    </RouteLoadError>
  );
}

/** Every entry is a dynamic import of exactly one `src/pages/*.tsx` module. */
const pages = {
  login: () => import("./pages/Login").then((m) => ({ default: m.Login })),
  oidcLogin: () => import("./pages/OidcLogin").then((m) => ({ default: m.OidcLogin })),
  register: () => import("./pages/Register").then((m) => ({ default: m.Register })),
  dashboard: () => import("./pages/Dashboard").then((m) => ({ default: m.Dashboard })),
  // M90 (docs/43 §13): the partner's page + the invitee's landing.
  shared: () => import("./pages/SharedHousehold").then((m) => ({ default: m.SharedHousehold })),
  sharingAccept: () => import("./pages/SharingAccept").then((m) => ({ default: m.SharingAccept })),
  transactions: () => import("./pages/Transactions").then((m) => ({ default: m.Transactions })),
  imports: () => import("./pages/Imports").then((m) => ({ default: m.Imports })),
  investments: () => import("./pages/Investments").then((m) => ({ default: m.Investments })),
  securityDetail: () => import("./pages/SecurityDetail").then((m) => ({ default: m.SecurityDetail })),
  // M100 (docs/14 C10): the accounts INDEX — what the rail's Accounts entries
  // (collapsed wallet, expanded section heading) point at.
  accounts: () => import("./pages/Accounts").then((m) => ({ default: m.Accounts })),
  accountDetail: () => import("./pages/AccountDetail").then((m) => ({ default: m.AccountDetail })),
  reports: () => import("./pages/Reports").then((m) => ({ default: m.Reports })),
  // M109 (docs/45): the what-if decision surface that grows out of Reports.
  forecast: () => import("./pages/Forecast").then((m) => ({ default: m.Forecast })),
  // M188 (docs/57): the plain-English assistant.
  assistant: () => import("./pages/Assistant").then((m) => ({ default: m.Assistant })),
  // M188 (docs/57): the Monte Carlo retirement simulator, beside the forecast.
  retirement: () => import("./pages/Retirement").then((m) => ({ default: m.Retirement })),
  calendar: () => import("./pages/Calendar").then((m) => ({ default: m.Calendar })),
  taxes: () => import("./pages/Taxes").then((m) => ({ default: m.Taxes })),
  settings: () => import("./pages/Settings").then((m) => ({ default: m.Settings })),
  tags: () => import("./pages/Tags").then((m) => ({ default: m.Tags })),
  rules: () => import("./pages/Rules").then((m) => ({ default: m.Rules })),
  currencies: () => import("./pages/Currencies").then((m) => ({ default: m.Currencies })),
  goals: () => import("./pages/Goals").then((m) => ({ default: m.Goals })),
  budgets: () => import("./pages/Budgets").then((m) => ({ default: m.Budgets })),
  reconciliation: () => import("./pages/Reconciliation").then((m) => ({ default: m.Reconciliation })),
  bills: () => import("./pages/Bills").then((m) => ({ default: m.Bills })),
  // M116 (docs/25 §M116 B2): the drill-down a bill row opens.
  billDetail: () => import("./pages/BillDetail").then((m) => ({ default: m.BillDetail })),
  debts: () => import("./pages/Debts").then((m) => ({ default: m.Debts })),
  taxProjection: () => import("./pages/TaxProjection").then((m) => ({ default: m.TaxProjection })),
  help: () => import("./pages/Help").then((m) => ({ default: m.Help })),
  helpGuide: () => import("./pages/HelpGuide").then((m) => ({ default: m.HelpGuide })),
  notFound: () => import("./pages/NotFound").then((m) => ({ default: m.NotFound })),
};

/**
 * Route table (docs/09): `/login` and `/register` public;
 * `/dashboard`, `/transactions`, `/imports`, `/investments`, `/reports`,
 * `/taxes`, `/settings` behind an auth guard redirecting to `/login`.
 * The `*` catch-all is the LAST child so every real path outranks it, and it
 * sits inside `AppShell` so a bad URL still gets the branded chrome instead of
 * React Router's default "Unexpected Application Error!" (M68).
 */
// Exported so tests can drive the SAME router the app runs (they navigate it
// to a fresh location before each render instead of patching window.history).
export const router = createBrowserRouter([
  { path: "/login", element: route(pages.login) },
  // OIDC redirect target (docs/38): the backend 302s the browser here with the
  // `oidc_pending` cookie — PUBLIC, because no session exists yet.
  { path: "/login/oidc", element: route(pages.oidcLogin) },
  { path: "/register", element: route(pages.register) },
  {
    element: <RequireAuth />,
    children: [
      {
        path: "/",
        element: <AppShell />,
        children: [
          { index: true, element: <Navigate to="/dashboard" replace /> },
          { path: "dashboard", element: route(pages.dashboard) },
          // `/sharing/accept` sits behind RequireAuth ON PURPOSE (docs/43 §13):
          // a signed-out visit bounces to /login with state.from carrying the
          // full path INCLUDING ?token=…, so sign-in/register resume here.
          { path: "sharing/accept", element: route(pages.sharingAccept) },
          { path: "shared", element: route(pages.shared) },
          { path: "transactions", element: route(pages.transactions) },
          { path: "imports", element: route(pages.imports) },
          { path: "investments", element: route(pages.investments) },
          { path: "securities/:securityId", element: route(pages.securityDetail) },
          // M100: the accounts index. Static segments outrank the dynamic
          // `/accounts/:accountId` below, so both can live in either order —
          // listed first because it is the section entry the rail points at.
          { path: "accounts", element: route(pages.accounts) },
          { path: "accounts/:accountId", element: route(pages.accountDetail) },
          { path: "reports", element: route(pages.reports) },
          // M109 (docs/45): a decision surface that grows out of Reports, so
          // it sits beside it in both the table and the rail.
          { path: "forecast", element: route(pages.forecast) },
          // M188 (docs/57): ask in plain English — beside the what-if surface.
          { path: "assistant", element: route(pages.assistant) },
          { path: "retirement", element: route(pages.retirement) },
          { path: "calendar", element: route(pages.calendar) },
          { path: "taxes", element: route(pages.taxes) },
          { path: "settings", element: route(pages.settings) },
          { path: "tags", element: route(pages.tags) },
          { path: "rules", element: route(pages.rules) },
          { path: "currencies", element: route(pages.currencies) },
          { path: "goals", element: route(pages.goals) },
          { path: "budgets", element: route(pages.budgets) },
          { path: "reconciliation/:accountId", element: route(pages.reconciliation) },
          { path: "bills", element: route(pages.bills) },
          { path: "bills/:billId", element: route(pages.billDetail) },
          { path: "debts", element: route(pages.debts) },
          { path: "tax-projection", element: route(pages.taxProjection) },
          { path: "help", element: route(pages.help) },
          { path: "help/:slug", element: route(pages.helpGuide) },
          { path: "*", element: route(pages.notFound) },
        ],
      },
    ],
  },
]);

/** Redirects unauthenticated visitors to /login once the session probe lands. */
function RequireAuth() {
  const { ready, user } = useAuth();
  const location = useLocation();
  if (!ready) {
    // Branded splash while the session probe resolves.
    return (
      <div className="grid min-h-dvh place-items-center bg-surface">
        <motion.span
          {...splashPulse}
          className="flex size-12 items-center justify-center rounded-xl bg-gradient-to-br from-accent to-accent-soft text-accent-ink shadow-glow"
        >
          <Zap className="size-5" aria-hidden />
        </motion.span>
      </div>
    );
  }
  if (user === null) {
    // Carry the requested path through /login so BOTH sign-in buttons can
    // resume it (postAuthRedirect) — deep link `/rules` while logged out →
    // login/demo → `/rules` (M68).
    return (
      <Navigate
        to="/login"
        replace
        state={{ from: `${location.pathname}${location.search}` }}
      />
    );
  }
  return <Outlet />;
}

export default function App() {
  return (
    // reducedMotion="user" (docs/09 repo rule): when the OS asks for reduced
    // motion, motion components skip transform/opacity animation entirely —
    // presence swaps resolve instantly instead of animating.
    <MotionConfig reducedMotion="user">
      <AuthProvider>
        <ToastProvider>
          <RouterProvider router={router} />
        </ToastProvider>
      </AuthProvider>
    </MotionConfig>
  );
}
