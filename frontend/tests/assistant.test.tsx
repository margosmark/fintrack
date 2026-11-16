// assistant.test.tsx — docs/57 §Verification (the frontend pins):
//   * the card renders Now vs With change deltas from before/after;
//   * Apply POSTs /assistant/proposals/{id}/apply and invalidates history;
//     Dismiss POSTs dismiss;
//   * a history row with status=applied shows Undo; after undo the row shows
//     Undone and no Undo button;
//   * a refusal envelope (code=assistant_proposal_invalid) renders the honest
//     "nothing was applied" copy and NO card;
//   * Enter in the question box asks (docs/57 §6).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";
import { ApiError } from "../src/api/client";
import type { Proposal } from "../src/api/assistant";

const m = vi.hoisted(() => ({
  ask: vi.fn(),
  apply: vi.fn(),
  dismiss: vi.fn(),
  undo: vi.fn(),
  history: vi.fn(),
  listCategories: vi.fn(),
}));

vi.mock("../src/api/assistant", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/assistant")>()),
  askAssistant: m.ask,
  applyProposal: m.apply,
  dismissProposal: m.dismiss,
  undoProposal: m.undo,
  listHistory: m.history,
}));
vi.mock("../src/api/categories", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/categories")>()),
  listCategories: m.listCategories,
}));

import { Assistant } from "../src/pages/Assistant";
import { ToastProvider } from "../src/components/ui/Toast";

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const ui: ReactElement = (
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter>
          <Assistant />
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>
  );
  return render(ui);
}

const PENDING: Proposal = {
  id: 7,
  question: "rename my chequing",
  answer: "Your chequing account is named Chequing.",
  kind: "account.update",
  target: "account:3",
  target_label: "Chequing",
  summary: "Rename the account",
  before: { name: "Chequing" },
  after: { name: "Everyday Chequing" },
  status: "pending",
  created_at: "2026-11-10T06:00:00Z",
  applied_at: null,
  undone_at: null,
  apply_audit_id: null,
  undo_audit_id: null,
};

const APPLIED: Proposal = {
  ...PENDING,
  id: 8,
  status: "applied",
  applied_at: "2026-11-10T06:05:00Z",
  apply_audit_id: 21,
};

const UNDONE: Proposal = {
  ...APPLIED,
  id: 9,
  status: "undone",
  undone_at: "2026-11-10T06:10:00Z",
  undo_audit_id: 22,
};

function typeAndAsk(question: string) {
  const box = screen.getByTestId("assistant-question");
  fireEvent.change(box, { target: { value: question } });
  fireEvent.click(screen.getByTestId("assistant-ask"));
}

beforeEach(() => {
  m.history.mockResolvedValue({ items: [] });
  m.listCategories.mockResolvedValue({
    items: [{ id: 4, user_id: 1, name: "Groceries", parent_id: null, is_system: false }],
    total: 1,
    limit: 200,
    offset: 0,
  });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("ask → proposed-change card", () => {
  it("renders the answer and a Now vs With change delta table", async () => {
    m.ask.mockResolvedValue({ answer: PENDING.answer, proposal: PENDING });
    renderPage();

    typeAndAsk("rename my chequing");

    const answerCard = await screen.findByTestId("assistant-answer");
    expect(answerCard.textContent ?? "").toContain("Your chequing account is named Chequing.");
    const card = await screen.findByTestId("assistant-proposal");
    expect(card.textContent ?? "").toContain("Rename the account");
    expect(screen.getAllByTestId("assistant-delta-row")).toHaveLength(1);
    expect(screen.getByTestId("assistant-delta-now").textContent ?? "").toContain("Chequing");
    expect(screen.getByTestId("assistant-delta-with").textContent ?? "").toContain(
      "Everyday Chequing",
    );
    expect(screen.getByTestId("assistant-apply")).toBeTruthy();
    expect(screen.getByTestId("assistant-dismiss")).toBeTruthy();
    expect(m.ask).toHaveBeenCalledWith("rename my chequing");
  });

  it("Enter in the question box asks", async () => {
    m.ask.mockResolvedValue({ answer: "One account.", proposal: null });
    renderPage();

    const box = screen.getByTestId("assistant-question");
    fireEvent.change(box, { target: { value: "how many accounts?" } });
    fireEvent.keyDown(box, { key: "Enter" });

    await waitFor(() => expect(m.ask).toHaveBeenCalledWith("how many accounts?"));
  });

  it("renders the refusal envelope with 'nothing was applied' and NO card", async () => {
    m.ask.mockRejectedValue(
      new ApiError(
        422,
        "unsupported field(s) for account.update: bogus",
        "assistant_proposal_invalid",
      ),
    );
    renderPage();

    typeAndAsk("change something");

    const refusal = await screen.findByTestId("assistant-refusal");
    expect(refusal.textContent ?? "").toContain("Nothing was applied.");
    expect(refusal.textContent ?? "").toContain("unsupported field(s) for account.update: bogus");
    expect(screen.queryByTestId("assistant-proposal")).toBeNull();
    expect(screen.queryByTestId("assistant-answer")).toBeNull();
  });
});

describe("apply / dismiss", () => {
  it("Apply POSTs the proposal, clears the card and invalidates history", async () => {
    m.ask.mockResolvedValue({ answer: PENDING.answer, proposal: PENDING });
    m.apply.mockResolvedValue({ ...PENDING, status: "applied" });
    renderPage();

    typeAndAsk("rename my chequing");
    await screen.findByTestId("assistant-proposal");

    m.history.mockClear();
    fireEvent.click(screen.getByTestId("assistant-apply"));

    await waitFor(() => expect(m.apply).toHaveBeenCalledWith(7));
    await waitFor(() => expect(screen.queryByTestId("assistant-proposal")).toBeNull());
    await waitFor(() => expect(m.history).toHaveBeenCalled());
  });

  it("Dismiss POSTs the proposal and clears the card without applying", async () => {
    m.ask.mockResolvedValue({ answer: PENDING.answer, proposal: PENDING });
    m.dismiss.mockResolvedValue({ ...PENDING, status: "dismissed" });
    renderPage();

    typeAndAsk("rename my chequing");
    await screen.findByTestId("assistant-proposal");

    fireEvent.click(screen.getByTestId("assistant-dismiss"));

    await waitFor(() => expect(m.dismiss).toHaveBeenCalledWith(7));
    await waitFor(() => expect(screen.queryByTestId("assistant-proposal")).toBeNull());
    expect(m.apply).not.toHaveBeenCalled();
  });
});

describe("history undo", () => {
  it("an applied row shows Undo; the undone row shows Undone and no button", async () => {
    m.history.mockResolvedValueOnce({ items: [APPLIED] }).mockResolvedValue({ items: [UNDONE] });
    m.undo.mockResolvedValue({ ...APPLIED, status: "undone" });
    renderPage();

    expect(await screen.findByTestId("assistant-undo")).toBeTruthy();
    expect(screen.getByTestId("assistant-history-row").textContent ?? "").toContain("Applied");

    fireEvent.click(screen.getByTestId("assistant-undo"));

    await waitFor(() => expect(m.undo).toHaveBeenCalledWith(8));
    // invalidation refetches history -> the row comes back undone
    expect(await screen.findByTestId("assistant-undone")).toBeTruthy();
    expect(screen.queryByTestId("assistant-undo")).toBeNull();
    expect(screen.getByTestId("assistant-history-row").textContent ?? "").toContain("Undone");
  });
});
