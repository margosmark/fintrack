// docs/57 §3 — the TaxEngine seam for client-side simulations.
//
// The retirement Monte Carlo (lib/retirementEngine.ts) must not model Canadian
// tax: docs/06 owns that math, computed server-side. This interface is the one
// place the sim asks "how much do I have to pull out to actually spend X?" and
// v1 answers it with a flat-rate stub. The Canadian tax projection card
// (t_52ed8d01) fills it with the real engine — swap the constructor call in
// pages/Retirement.tsx, keep every call site.

export interface TaxContext {
  /** The age at the withdrawal step (plan years, not a date). */
  age: number;
  /** Calendar year of this withdrawal step: `baseYear + t` (docs/57 §2). */
  year: number;
}

export interface TaxEngine {
  /** Machine id: `"stub-flat"` today, `"ca-engine"` for the real one. */
  readonly id: string;
  /** Human label rendered next to the tax control (docs/57 §4). */
  readonly label: string;
  /**
   * Gross withdrawal required so the retiree NETS `netSpend` after tax. The
   * portfolio is debited the GROSS amount — the `gross - netSpend` part is the
   * tax. A real engine solves its own brackets inside this call.
   */
  grossWithdrawalFor(netSpend: number, ctx: TaxContext): number;
}

/**
 * The v1 stub: one flat rate on the withdrawal. `rate = 0` (the default) makes
 * gross === net, so the simulation runs tax-free exactly as the panel says.
 * Rate is clamped to 0..95 — at 100 the gross would be undefined, and no
 * honest engine ever takes every dollar.
 */
export function flatRateTaxEngine(ratePct: number): TaxEngine {
  const rate = Math.min(95, Math.max(0, ratePct)) / 100;
  return {
    id: "stub-flat",
    label: "Flat-rate stub",
    grossWithdrawalFor(netSpend: number): number {
      return netSpend / (1 - rate);
    },
  };
}
