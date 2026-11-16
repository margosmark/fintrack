import { beforeEach, describe, expect, it } from "vitest";
import type { User } from "../src/types";
import {
  candidatesOwner,
  readCandidates,
  scopeCandidatesTo,
  writeCandidates,
  type CacheOwner,
} from "../src/lib/recurringSession";

const RECURRING_CACHE_KEY = "fintrack.recurring-candidates.v1";

describe("numeric user identity at the recurring-session storage boundary", () => {
  beforeEach(() => {
    sessionStorage.clear();
    scopeCandidatesTo(null);
  });

  it("keeps the real User id strict-equal across an API JSON round-trip", () => {
    // This annotation deliberately uses the production User contract: changing
    // User.id back to a string must fail the test's TypeScript gate.
    const userId: User["id"] = 1;
    const apiUser: Pick<User, "id"> & { name: string } = {
      id: userId,
      name: "Mark",
    };

    const restoredUser = JSON.parse(JSON.stringify(apiUser)) as typeof apiUser;

    expect(restoredUser.id).toBe(apiUser.id);
    expect(typeof restoredUser.id).toBe("number");
  });

  it("recovers its own cache after the numeric id is stamped and reloaded", () => {
    const userId: User["id"] = 1;
    const cacheOwner: CacheOwner = userId;

    scopeCandidatesTo(cacheOwner);
    writeCandidates([]);

    // Browser storage is string-backed. The owner stamp must be normalized at
    // this boundary, rather than preserving the numeric wire representation.
    expect(candidatesOwner()).toBe("1");
    expect(sessionStorage.getItem(RECURRING_CACHE_KEY)).toBe(
      JSON.stringify({ owner: "1", groups: [] }),
    );

    // A reload reconstructs the same numeric User id from JSON and binds it
    // again. [] proves the entry was found; a cache miss would be null.
    const reloadedUser = JSON.parse(JSON.stringify({ id: userId })) as Pick<User, "id">;
    scopeCandidatesTo(reloadedUser.id);
    expect(readCandidates()).toEqual([]);
  });
});
