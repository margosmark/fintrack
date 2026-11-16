// CoveredByLine — the "this is already a bill" statement both suggestion
// surfaces print INSTEAD of a Create-bill button (M165, docs/09 §Recurring
// prompt + §Bills you probably have).
//
// One component because the copy is a contract: docs/09 pins the sentence
// `covered by '<bill name>' → /bills`, and two hand-written copies of a
// pinned sentence are two chances to drift. It is a muted LINE with a link to
// the existing record — deliberately not a button, because a button is an
// action and there is no action left to take: the bill exists.
import { Link } from "react-router-dom";
import type { Bill } from "../../api/bills";

export function CoveredByLine({
  bill,
  testId = "covered-by-bill",
}: {
  bill: Bill;
  testId?: string;
}) {
  return (
    <p className="text-xs text-muted" data-testid={testId}>
      covered by <span className="font-medium text-text">{`'${bill.name}'`}</span>{" "}
      <Link
        to="/bills"
        className="underline decoration-dotted underline-offset-2"
        aria-label={`Open the '${bill.name}' bill on the Bills page`}
      >
        → /bills
      </Link>
    </p>
  );
}
