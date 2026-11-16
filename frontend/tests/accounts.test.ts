// @vitest-environment node
import { describe, expect, it } from "vitest";
import {
  filterInvestmentAccounts,
  INVESTMENT_ACCOUNT_TYPES,
  type AccountType,
} from "../src/api/accounts";

type MinimalAccount = { id: number; type: AccountType };

const make = (type: AccountType, id: number): MinimalAccount => ({ id, type });

const ALL_TYPES: AccountType[] = [
  "chequing",
  "savings",
  "credit_card",
  "line_of_credit",
  "brokerage_non_registered",
  "rrsp",
  "tfsa",
  "fhsa",
  "resp",
  "lira",
  "rdsp",
];

describe("filterInvestmentAccounts", () => {
  it("keeps only investment-capable account types", () => {
    const accounts = ALL_TYPES.map((type, i) => make(type, i));
    const result = filterInvestmentAccounts(accounts);
    const ids = result.map((a) => a.id).sort((a, b) => a - b);
    // Investment types: brokerage(4), rrsp(5), tfsa(6), fhsa(7), resp(8), lira(9), rdsp(10)
    expect(ids).toEqual([4, 5, 6, 7, 8, 9, 10]);
  });

  it("returns an empty list when there are no investment accounts", () => {
    const accounts = [
      make("chequing", 1),
      make("savings", 2),
      make("credit_card", 3),
      make("line_of_credit", 4),
    ];
    expect(filterInvestmentAccounts(accounts)).toEqual([]);
  });

  it("returns an empty list for an empty input", () => {
    expect(filterInvestmentAccounts([])).toEqual([]);
  });

  it("matches the INVESTMENT_ACCOUNT_TYPES membership set exactly", () => {
    const expected = ALL_TYPES.filter((t) => INVESTMENT_ACCOUNT_TYPES[t] === true);
    const result = filterInvestmentAccounts(ALL_TYPES.map((type, i) => make(type, i)));
    expect(result.map((a) => a.type)).toEqual(expected);
  });
});
