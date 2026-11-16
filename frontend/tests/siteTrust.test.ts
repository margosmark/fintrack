// Trust & privacy page (site/trust.html) — copy-drift pins (t_09ed864f).
//
// The page makes factual claims about the shipped code: exact Settings paths,
// the deletion endpoint, and the AI payload caps. Marketing copy that can't
// fail isn't honest, so each claim is pinned against the source it describes —
// if the app drifts (a card is renamed, a constant changes) the build goes red
// instead of the page quietly becoming false.
//
// Fail closed (privacy review F1): a pinned source that is absent from this
// checkout must surface as a red assertion naming the path — never an ENOENT
// thrown at module load, which would crash the whole suite before it runs.
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const ROOT = path.resolve(__dirname, "..", "..");
const missing: string[] = [];
const read = (rel: string): string => {
  const file = path.join(ROOT, rel);
  if (!existsSync(file)) {
    missing.push(rel);
    return "";
  }
  return readFileSync(file, "utf8");
};

const trust = read("site/trust.html");
const index = read("site/index.html");
const dockerfile = read("site/Dockerfile");
const settings = read("frontend/src/pages/Settings.tsx");
const users = read("backend/app/api/users.py");
const categorization = read("backend/app/services/categorization.py");
const columnMapping = read("backend/app/services/column_mapping.py");
const egress = read("backend/app/llm/egress.py");

describe("pin sources exist on this checkout (fail closed)", () => {
  it("every source file this suite pins is present", () => {
    expect(missing, `missing pin source(s): ${missing.join(", ")}`).toEqual([]);
  });
});

describe("trust page ships and is reachable", () => {
  it("carries the three promise headings", () => {
    expect(trust).toContain("Never sold");
    expect(trust).toContain("Never used to train AI");
    expect(trust).toContain("Yours to take or delete");
  });

  it("is linked from the landing page (privacy section + footer)", () => {
    const links = index.match(/href="trust\.html"/g) ?? [];
    expect(links.length).toBeGreaterThanOrEqual(2);
  });

  it("is COPYied into the site image and served at /trust", () => {
    expect(dockerfile).toContain("trust.html");
    expect(dockerfile).toContain("location = /trust");
  });
});

describe("promise: yours to take or delete — paths exist in the app", () => {
  it("names Settings cards/headings verbatim", () => {
    // The page must quote the UI exactly as the UI spells it.
    expect(settings).toContain("Privacy &amp; data");
    expect(settings).toContain("Export my data");
    expect(settings).toContain("Download ZIP");
    expect(settings).toContain("Download JSON");
    expect(settings).toContain("Delete account");
    expect(settings).toContain("LLM provider");
    for (const claim of [
      "Privacy &amp; data",
      "Export my data",
      "Download ZIP",
      "Download JSON",
      "Delete account",
      "LLM provider",
    ]) {
      expect(trust, `trust page must name "${claim}"`).toContain(claim);
    }
  });

  it("names the real account-deletion endpoint", () => {
    expect(users).toContain('@router.delete("/me"');
    expect(trust).toContain("DELETE /api/v1/users/me");
  });
});

describe("promise: never used to train AI — payload caps match the code", () => {
  it("categorization: 10 few-shot examples, batches of 25", () => {
    expect(categorization).toContain("FEW_SHOT_LIMIT = 10");
    expect(categorization).toContain("BATCH_SIZE = 25");
    expect(trust).toContain("up to 10");
    expect(trust).toContain("up to 25");
  });

  it("CSV column mapping: headers + up to 5 sample rows", () => {
    expect(columnMapping).toContain("max_rows: int = 5");
    expect(trust).toContain("up to 5 sample");
  });

  it("privacy mode refusal code exists and the page states it", () => {
    expect(egress).toContain("privacy_mode_blocks_external_api");
    expect(trust).toContain("privacy_mode_blocks_external_api");
  });
});

describe("honesty guardrails on the page", () => {
  it("states the docs/33 encryption status, not a zero-knowledge claim", () => {
    expect(trust).toMatch(/server can still read your data/i);
    expect(trust).toContain("not zero-knowledge");
    expect(trust).not.toMatch(/\bis zero-knowledge\b/i);
  });

  it("carries no marketing-fog security clichés", () => {
    expect(trust).not.toMatch(/military-grade|bank-level encryption|your data is (?:completely )?safe with us/i);
  });

  it("pins a verification date instead of an open-ended promise", () => {
    expect(trust).toMatch(/Last verified against the code: \d{4}/);
  });
});
