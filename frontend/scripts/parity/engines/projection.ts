// M157 engine adapter — the projection parity slice (docs/12 §7, docs/33
// §Server-first rule 5; M135 §4.3 "`POST /projections/run` (or its read twin)
// → projection-parity.json 7/7 cases").
//
//   POST /projections/run   ->   the server, over HTTP, as the DEMO tenant
//
// Every other slice's engine ports the CLIENT mirror and diffs it against a
// captured server golden. This one is the mirror image, deliberately: the
// restore this slice gates is the SERVER route, so the thing under test is
// the route itself — and it is exercised under the demo tenant on purpose,
// because "the demo tenant can run projections" is the exact invariant M135
// §2 K-12 restores (the read-only demo used to 403 this POST, which is why
// the calculator ran in the browser at all). A 403 therefore surfaces as a
// `$.status` diff at exit 1 — the slice cannot pass while the demo tenant is
// refused a run.
//
// `EngineCall` never carries `expected`, so echoing the golden is unreachable.

import { demoToken, request, type RawResponse } from "../http.ts";
import type { Engine, EngineCall } from "../types.ts";

/** Flip to true only once runEngine really computes the response. */
export const wired = true;

/** `POST /auth/demo` is capped at 5/min — one token for the whole slice. */
let cachedToken: string | null = null;

async function tokenFor(baseUrl: string): Promise<string> {
  if (cachedToken === null) cachedToken = await demoToken(baseUrl);
  return cachedToken;
}

export const runEngine: Engine = async (call: EngineCall): Promise<RawResponse> => {
  const path = call.request.path.split("?")[0] as string;
  if (path !== "/projections/run") {
    throw new Error(`projection engine: no implementation for ${path}`);
  }
  if (call.request.method !== "POST") {
    throw new Error(`projection engine: expected POST ${path}, got ${call.request.method}`);
  }
  const baseUrl = call.meta.base_url.replace(/\/$/, "");
  return request("POST", `${baseUrl}/projections/run`, {
    token: await tokenFor(baseUrl),
    body: call.request.body,
  });
};
