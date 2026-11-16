// The sidebar's account grouping + the shared display names for account types,
// in one place (M100).
//
// `AppShell`'s rail and the `/accounts` index page both present "accounts
// grouped by type", and they must agree on the group labels and membership or
// the rail's promise ("Accounts") and the page it opens drift apart. This
// module is the single source: `AppShell` renders `TYPE_GROUPS` in the rail,
// `pages/Accounts.tsx` renders it as the index's section list, and
// `TYPE_LABELS` is the one table of human names for `AccountType` (the
// Settings manager's select and every row chip read it).
//
// NOTE the Settings → accounts MANAGER keeps its own `TYPE_GROUPS` on purpose:
// its group labels are manager vocabulary ("Registered non-taxable") and
// docs/14 C2 pins them — that is a different surface, not a copy of this one.
import type { AccountType } from "../api/accounts";

export const TYPE_GROUPS: ReadonlyArray<{
  label: string;
  types: readonly AccountType[];
}> = [
  { label: "Cash", types: ["chequing", "savings"] },
  { label: "Credit", types: ["credit_card", "line_of_credit"] },
  {
    label: "Registered",
    types: ["rrsp", "tfsa", "fhsa", "resp", "lira", "rdsp"],
  },
  { label: "Non-registered", types: ["brokerage_non_registered"] },
];

/** Display labels of the widened AccountType enum (docs/14 C1). */
export const TYPE_LABELS: Record<AccountType, string> = {
  chequing: "Chequing",
  savings: "Savings",
  credit_card: "Credit card",
  line_of_credit: "Line of credit",
  brokerage_non_registered: "Brokerage (non-registered)",
  rrsp: "RRSP",
  tfsa: "TFSA",
  fhsa: "FHSA",
  resp: "RESP",
  lira: "LIRA",
  rdsp: "RDSP",
};
