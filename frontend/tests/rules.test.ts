import { createElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, cleanup, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Rules } from "../src/pages/Rules";

vi.mock("../src/api/rules", () => ({
  listRules: vi.fn(),
  createRule: vi.fn(),
  updateRule: vi.fn(),
  deleteRule: vi.fn(),
  previewRule: vi.fn(),
  applyRule: vi.fn(),
}));
vi.mock("../src/api/categories", () => ({
  listCategories: vi.fn(),
}));
vi.mock("../src/api/tags", () => ({
  listTags: vi.fn(),
  createTag: vi.fn(),
}));
vi.mock("../src/components/ui/Toast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }),
}));

import { applyRule, createRule, listRules, previewRule } from "../src/api/rules";
import { listCategories } from "../src/api/categories";
import { createTag, listTags } from "../src/api/tags";

const rule = {
  id: 5,
  user_id: 1,
  match_type: "contains",
  pattern: "NETFLIX",
  priority: 10,
  category_id: 7,
  enabled: true,
  tag_action: "set",
  stop_on_match: true,
  tags: [],
};

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  render(
    createElement(QueryClientProvider, {
      client,
      children: createElement(MemoryRouter, null, createElement(Rules)),
    }),
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Rules dry-run", () => {
  it("previews matches and applies to all", async () => {
    vi.mocked(listRules).mockResolvedValue({ items: [rule], total: 1, limit: 200, offset: 0 });
    vi.mocked(listCategories).mockResolvedValue({
      items: [{ id: 7, user_id: 1, name: "entertainment", parent_id: null, is_system: true }],
      total: 1,
      limit: 200,
      offset: 0,
    });
    vi.mocked(listTags).mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
    vi.mocked(previewRule).mockResolvedValue({
      matched_count: 2,
      assigned_skipped: 0,
      items: [
        {
          id: 11, date: "2026-09-02", payee: "NETFLIX.COM", amount_native: "-16.99",
          category_id: null, category_name: null,
        },
      ],
    });
    vi.mocked(applyRule).mockResolvedValue({
      rule_id: 5, mode: "all", matched_count: 2, applied_count: 2, note: null,
    });
    renderPage();

    fireEvent.click(await screen.findByLabelText("Test run rule NETFLIX"));
    fireEvent.click(screen.getByText("Run preview"));

    await screen.findByText((_, el) => el?.textContent === "2 transactions would match");
    expect(screen.getByText("NETFLIX.COM")).toBeTruthy();
    expect(previewRule).toHaveBeenCalledWith(
      expect.objectContaining({ match_type: "contains", pattern: "NETFLIX" }),
    );

    fireEvent.click(screen.getByText("Apply to all"));
    await screen.findByText(/Applied 2 of 2 matched/);
  });

  it("explains zero applied when matches are already assigned", async () => {
    vi.mocked(listRules).mockResolvedValue({ items: [rule], total: 1, limit: 200, offset: 0 });
    vi.mocked(listCategories).mockResolvedValue({
      items: [{ id: 7, user_id: 1, name: "entertainment", parent_id: null, is_system: true }],
      total: 1,
      limit: 200,
      offset: 0,
    });
    vi.mocked(listTags).mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
    vi.mocked(previewRule).mockResolvedValue({
      matched_count: 0,
      assigned_skipped: 0,
      items: [],
    });
    vi.mocked(applyRule).mockResolvedValue({
      rule_id: 5, mode: "all", matched_count: 23, applied_count: 0, note: null,
    });
    renderPage();

    fireEvent.click(await screen.findByLabelText("Test run rule NETFLIX"));
    fireEvent.click(screen.getByText("Apply to all"));

    await screen.findByText(/Applied 0 of 23 matched/);
    expect(screen.getByText(/tick Overwrite on apply/)).toBeTruthy();
  });

  it("explains empty preview when assigned rows are hidden", async () => {
    vi.mocked(listRules).mockResolvedValue({ items: [rule], total: 1, limit: 200, offset: 0 });
    vi.mocked(listCategories).mockResolvedValue({
      items: [{ id: 7, user_id: 1, name: "entertainment", parent_id: null, is_system: true }],
      total: 1,
      limit: 200,
      offset: 0,
    });
    vi.mocked(listTags).mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
    vi.mocked(previewRule).mockResolvedValue({
      matched_count: 0,
      assigned_skipped: 11,
      items: [],
    });
    renderPage();

    fireEvent.click(await screen.findByLabelText("Test run rule NETFLIX"));
    fireEvent.click(screen.getByText("Run preview"));

    await screen.findByText("No matches in scope");
    expect(screen.getByText(/11 matching rows are already assigned/)).toBeTruthy();
  });

  it("creates a tag inline in the rule form", async () => {
    const tagStore: { id: number; user_id: number; name: string }[] = [];
    vi.mocked(listRules).mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
    vi.mocked(listCategories).mockResolvedValue({
      items: [{ id: 7, user_id: 1, name: "entertainment", parent_id: null, is_system: true }],
      total: 1,
      limit: 200,
      offset: 0,
    });
    vi.mocked(listTags).mockImplementation(() =>
      Promise.resolve({ items: [...tagStore], total: tagStore.length, limit: 200, offset: 0 }),
    );
    vi.mocked(createTag).mockImplementation(({ name }: { name: string }) => {
      const tag = { id: 3, user_id: 1, name };
      tagStore.push(tag);
      return Promise.resolve(tag);
    });
    renderPage();

    await screen.findByText("Add rule");
    fireEvent.click(screen.getByText("+ New…"));
    fireEvent.change(screen.getByLabelText("New rule tag name"), {
      target: { value: "binge" },
    });
    fireEvent.click(screen.getAllByText("Add")[0]);

    await screen.findByText("binge");
    expect(createTag).toHaveBeenCalledWith({ name: "binge" });
  });

  it("sends stop_on_match=false when the stop toggle is off", async () => {
    vi.mocked(listRules).mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
    vi.mocked(listCategories).mockResolvedValue({
      items: [{ id: 7, user_id: 1, name: "entertainment", parent_id: null, is_system: true }],
      total: 1,
      limit: 200,
      offset: 0,
    });
    vi.mocked(listTags).mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
    vi.mocked(createRule).mockResolvedValue({ ...rule, id: 6, stop_on_match: false });
    renderPage();

    await screen.findByText("Add rule");
    fireEvent.change(screen.getByPlaceholderText("e.g. amazon"), {
      target: { value: "NETFLIX" },
    });
    fireEvent.change(screen.getByLabelText("Category *"), {
      target: { value: "7" },
    });
    fireEvent.click(screen.getByLabelText("Stop at this rule on match"));
    // NOTE: click the button itself — jsdom won't submit a form from a
    // click on an element nested inside <button type="submit">.
    fireEvent.click(screen.getByText("Add rule").closest("button") as HTMLButtonElement);

    await waitFor(() => {
      expect(createRule).toHaveBeenCalledWith(
        expect.objectContaining({ pattern: "NETFLIX", stop_on_match: false }),
      );
    });
  });
});

// M82-F2: both rule-editor rows used a bare `grid-cols-12` with UNPREFIXED
// `col-span-*`, so below md a 2-column track was ~45px wide at a 412px
// viewport while the `Enabled` toggle alone needs ~98px. The row pushed the
// toggle and the Priority `?` past the right edge, where
// `main#main-content { overflow-x: clip }` swallowed them with no scrollbar to
// recover them (QA: 3 clipped nodes at every width from 412 down to 320).
// jsdom does no layout, so the pin is the class contract: full-width stacking
// below md, and spans that only exist from md up. Measured end-to-end by
// `frontend/scripts/local/rules_probe.py`.
describe("Rules form responsive contract (M82-F2)", () => {
  it("stacks both rule-editor rows below md and only spans columns from md up", async () => {
    vi.mocked(listRules).mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
    vi.mocked(listCategories).mockResolvedValue({
      items: [{ id: 7, user_id: 1, name: "entertainment", parent_id: null, is_system: true }],
      total: 1,
      limit: 200,
      offset: 0,
    });
    vi.mocked(listTags).mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
    renderPage();

    const form = (await screen.findByText("Add rule")).closest("form");
    expect(form).not.toBeNull();
    const rows = [...form!.querySelectorAll(":scope > .grid")];
    expect(rows).toHaveLength(2);

    for (const row of rows) {
      expect(row.classList.contains("grid-cols-1")).toBe(true);
      expect(row.classList.contains("md:grid-cols-12")).toBe(true);
      // An unprefixed `grid-cols-12` is the whole defect: it keeps the 12-col
      // row active at 320px.
      expect(row.classList.contains("grid-cols-12")).toBe(false);

      for (const child of [...row.children]) {
        const spans = [...child.classList].filter((c) => c.startsWith("col-span-"));
        expect(spans).toEqual([]);
        expect([...child.classList].filter((c) => c.startsWith("md:col-span-"))).toHaveLength(1);
      }
    }
  });
});

// M82-F2 residual: three parent-overflows that survive the F2 stacking fix and
// only appear under Android font scaling (130% root font = 18.4/20.8px).
// Measured end-to-end with `frontend/scripts/local/rules_probe.py`
// (parentOverflow == 0 at 320/344/360 x 18.4/20.8 and 768 x 20.8, clipped == 0
// across the whole 320-412 matrix). jsdom does no layout, so what these tests
// pin is the SOURCE of the two fixes that were still open — a class rename or
// a tidy-up that drops them re-opens the defect silently:
//   1. Row 3's prefill box was `flex flex-1` with no wrap, so its min-content
//      (label + gap + the nowrap "Use as pattern" button, ~245px at 130%)
//      WAS its width and it stuck 53.9px out of the row at 320.
//   2. `Enabled` sat in a 2-col track (55.4px at 768/130%) while the pill is
//      128px wide, and line 1 is exactly 12 tracks — no span split fits, so it
//      takes its own full-width row from md up, like `Stop here on match`.
//   3. CardTitle's `right` slot ("2 rules") no longer overflows at 320/130%:
//      the shared `mb-3 flex flex-wrap ...` title row (M82-F3) wraps it onto
//      its own line instead of pushing it past the card. That fix lives in
//      components/ui/Card.tsx and is asserted here only as the contract the
//      /rules page depends on.
describe("Rules form overflow residuals (M82-F2 residual)", () => {
  async function renderForm() {
    vi.mocked(listRules).mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
    vi.mocked(listCategories).mockResolvedValue({
      items: [{ id: 7, user_id: 1, name: "entertainment", parent_id: null, is_system: true }],
      total: 1,
      limit: 200,
      offset: 0,
    });
    vi.mocked(listTags).mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
    renderPage();
    const form = (await screen.findByText("Add rule")).closest("form");
    expect(form).not.toBeNull();
    return form as HTMLFormElement;
  }

  it("lets the prefill block wrap so it cannot outgrow row 3", async () => {
    const form = await renderForm();

    const prefill = [...form.querySelectorAll("div")].find(
      (d) => d.classList.contains("flex-1") && d.classList.contains("items-end"),
    );
    expect(prefill).toBeDefined();
    // Without `flex-wrap` the box's min-content is the SUM of label + button,
    // which is wider than the row at 320/130% — the box itself is the overflow.
    expect(prefill!.classList.contains("flex-wrap")).toBe(true);

    // Row 3 keeps its own wrap so the submit group can move to its own line.
    const row3 = prefill!.parentElement as HTMLElement;
    expect(row3.classList.contains("flex-wrap")).toBe(true);
    expect(row3.classList.contains("flex-1")).toBe(false);
  });

  it("gives the Enabled toggle a full row from md up", async () => {
    const form = await renderForm();

    const enabled = [...form.querySelectorAll("button")].find(
      (b) => b.getAttribute("aria-label") === "Rule enabled",
    );
    expect(enabled).not.toBeNull();
    const cell = enabled!.parentElement as HTMLElement;
    expect(cell.classList.contains("md:col-span-12")).toBe(true);
    // 2 tracks = 55.4px at 768/130% while the pill measures 128px.
    expect(cell.classList.contains("md:col-span-2")).toBe(false);

    // The rest of row 1 keeps its F2 spans: only the toggle moved.
    const row1 = form.querySelector(":scope > .grid");
    expect(row1).not.toBeNull();
    const spans = [...row1!.children].map((child) =>
      [...child.classList].filter((c) => c.startsWith("md:col-span-")),
    );
    expect(spans).toEqual([
      ["md:col-span-3"],
      ["md:col-span-5"],
      ["md:col-span-2"],
      ["md:col-span-12"],
      ["md:col-span-12"],
    ]);
  });

  it("keeps the card title row wrapping so the rules counter stays inside", async () => {
    const form = await renderForm();
    const title = form.parentElement!.querySelector<HTMLDivElement>(
      "div.mb-3.flex.items-center.justify-between",
    );
    expect(title).not.toBeNull();
    expect(title!.classList.contains("flex-wrap")).toBe(true);
  });
});

// M82-F2 200% residual: Android's MAXIMUM font scale makes the root font 32px
// while the viewport stays 320, so every rem-based floor grows with it — card
// padding 16→32, form padding 12→24, the 12-col gutter 8→16, the `Enabled`
// pill 128→197px, and the word "Categorization" 168→241.7px. Five things then
// sat outside their parent (the title 42.7px outside the viewport, where
// `main { overflow-x: clip }` cut it off with no scrollbar to recover it):
//
//   1. `CardTitle`'s h2 is a flex item, so an unbreakable word is a floor the
//      row cannot shrink past: 241.7px of min-content against a 174px row at
//      320. `[overflow-wrap:anywhere]` drops the floor to a single character
//      (normal word breaks still win wherever a line fits) so the row shrinks
//      the h2 instead of pushing it out of the viewport.
//   2. rem padding scales too — card `p-4` + form `p-3` left a 124px form
//      column at 320, narrower than the prefill label's own longest word
//      ("transaction", 142.7px) AND narrower than the submit row (140.6px).
//      One step down below `sm` gives 156px; `sm:` keeps >=640 unchanged.
//   3. the prefill button was `whitespace-nowrap`, so its min-content was
//      label + padding = 217.5px and it pushed the whole prefill box 93.5px
//      out of row 3. `wrap` swaps in `whitespace-normal` AND `min-h-7`: a
//      wrapped label is two lines, and a fixed `h-7` would print it outside
//      the button. It is opt-in — the submit button keeps `whitespace-nowrap`.
//   4. the toggle pill's min-content is switch + gap + label (197px for
//      `Enabled`) against a 156px cell. `flex-wrap` lets the label drop under
//      the switch, and a wrapping flex line sizes exactly like a nowrap one
//      while everything fits, so >=768 keeps every rect it had.
//   5. the Priority `?` lives in a 2-track cell = 36.7px at 768/200% (the
//      gutter alone is 16px per column), and the default `ml-1` (8px) + its
//      own 32px box stood 3.3px outside the label span. `ml-0.5 lg:ml-1`
//      keeps a visible gap at 100/115/130% and leaves 1024/1280 untouched.
//
// jsdom does no layout, so these tests pin the SOURCE of the five fixes; the
// measurement is `frontend/scripts/local/rules_probe.py` — `clipped=0` and
// `parentOver=0` at 320/360/412 x root 32 (dark + light), 768/1024/1280 at
// `parentOver=0`, and the inherited 16/18.4/20.8 matrix still 0 across the
// board (byte-identical 1024/1280 rects vs `m82res/out/font32_probe.json`).
describe("Rules 200% font-scale contract (M82-F2 residual, root 32)", () => {
  async function renderForm() {
    vi.mocked(listRules).mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
    vi.mocked(listCategories).mockResolvedValue({
      items: [{ id: 7, user_id: 1, name: "entertainment", parent_id: null, is_system: true }],
      total: 1,
      limit: 200,
      offset: 0,
    });
    vi.mocked(listTags).mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
    renderPage();
    const form = (await screen.findByText("Add rule")).closest("form");
    expect(form).not.toBeNull();
    return form as HTMLFormElement;
  }

  it("lets the card title shrink below a word that cannot fit", async () => {
    const form = await renderForm();
    const title = form.parentElement!.querySelector<HTMLDivElement>(
      "div.mb-3.flex.items-center.justify-between",
    );
    const h2 = title!.querySelector("h2");
    // Without it the h2's auto min-size is the word itself (241.7px at root
    // 32) and the flex row cannot shrink it — it leaves the viewport instead.
    expect(h2!.classList.contains("[overflow-wrap:anywhere]")).toBe(true);
    // `break-words` would NOT do: it wraps long words only when a line is
    // already overflowing and leaves min-content untouched.
    expect(h2!.classList.contains("break-words")).toBe(false);
  });

  it("gives the form one padding step below sm only", async () => {
    const form = await renderForm();
    const card = form.parentElement as HTMLElement;
    expect(card.classList.contains("p-3")).toBe(true);
    expect(card.classList.contains("sm:p-4")).toBe(true);
    expect(card.classList.contains("p-4")).toBe(false);

    expect(form.classList.contains("p-2")).toBe(true);
    expect(form.classList.contains("sm:p-3")).toBe(true);
    expect(form.classList.contains("p-3")).toBe(false);
  });

  it("lets the prefill button wrap while the submit button stays one line", async () => {
    const form = await renderForm();
    const prefill = [...form.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("Use as pattern"),
    );
    expect(prefill).toBeDefined();
    expect(prefill!.classList.contains("whitespace-normal")).toBe(true);
    expect(prefill!.classList.contains("whitespace-nowrap")).toBe(false);
    // A wrapped label is two lines tall: a fixed `h-7` would spill the second
    // line out of the button, so the height becomes a floor.
    expect(prefill!.classList.contains("min-h-7")).toBe(true);
    expect(prefill!.classList.contains("h-7")).toBe(false);

    // `wrap` is opt-in: every other button keeps the contract it had.
    const submit = screen.getByText("Add rule").closest("button") as HTMLButtonElement;
    expect(submit.classList.contains("whitespace-nowrap")).toBe(true);
    expect(submit.classList.contains("h-7")).toBe(true);
    expect(submit.classList.contains("whitespace-normal")).toBe(false);
  });

  it("lets the toggle pill drop its label onto its own line", async () => {
    const form = await renderForm();
    const enabled = [...form.querySelectorAll("button")].find(
      (b) => b.getAttribute("aria-label") === "Rule enabled",
    );
    expect(enabled).not.toBeNull();
    // min-content = switch + gap + label (197px for `Enabled` at root 32)
    // against a 156px cell; wrapping makes it the WIDER of switch and label.
    expect(enabled!.classList.contains("flex-wrap")).toBe(true);
  });

  it("shrinks the Priority hint's gap below lg", async () => {
    const form = await renderForm();
    // M97: the copy moved from the trigger's aria-label to the portalled
    // tooltip behind aria-describedby (docs/09 §Tooltips), so follow that link.
    const hint = [...form.querySelectorAll("button")].find((b) => {
      const id = b.getAttribute("aria-describedby");
      const tip = id === null ? null : document.getElementById(id);
      return (tip?.textContent ?? "").startsWith("Rules run in priority order");
    });
    expect(hint).toBeDefined();
    expect(hint!.classList.contains("ml-0.5")).toBe(true);
    expect(hint!.classList.contains("lg:ml-1")).toBe(true);
    // className REPLACES the default gap: a leftover `ml-1` would fight
    // `ml-0.5` for the same property and win by stylesheet order.
    expect(hint!.classList.contains("ml-1")).toBe(false);
  });
});
