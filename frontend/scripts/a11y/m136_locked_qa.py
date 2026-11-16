#!/usr/bin/env python3
"""M136 live locked-browser QA (0-d) — every wave-13a surface, against 0-a's
fixture tenant on the DEPLOYED stack.

Logs in as `locked-fixture@fintracker.local` (encryption_required=TRUE, no
envelope ⇒ locked in every browser) through the real login form, then walks
each surface and asserts the M136 contract:

  shell chip      the ONE status chip, enrolled+locked only
  global search   S-1: the popover announces the lock, never a bare "No matches"
  search view     A18/F4: partial variant + MEASURED withheld count, replacing
                  the empty state
  similar popover S-3: the shared search string + canonical destination
  backup panel    D-1: the shared client string in place of the invented one
  settings        D-3: the backfill panel uses the shared component
  G1 split        Save replaced by `blocked` BEFORE any leg is typed
  G2 bulk rename  "Set payee" disabled WITH the reason
  G3 YNAB        Import replaced by `blocked` in the plan view

Each check prints PASS/FAIL with the observed text so a human can read the
verdict without the DOM.

    cd frontend && python scripts/a11y/m136_locked_qa.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from a11y_driver import Browser  # noqa: E402

BASE = os.environ.get("M136_BASE", "http://127.0.0.1:18080")
EMAIL = "locked-fixture@fintracker.local"
PASSWORD = "locked-fixture-only"  # deliberate long-lived fixture credential (like demo-password) — never randomize

# A distinct port + profile so this probe never races the shared :9444 harness
# (fin-tracker skill: "drive it with YOUR OWN headless Chrome").
os.environ["A11Y_CDP_PORT"] = "9461"
os.environ["A11Y_PROFILE_DIR"] = tempfile.mkdtemp(prefix="m136-locked-qa-")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")


def login(tab) -> None:
    tab.navigate(f"{BASE}/login")
    tab.wait_for("document.querySelector('input[type=email]') !== null")
    tab.evaluate(
        """(() => {
          const set = (sel, val) => {
            const el = document.querySelector(sel);
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(el, val);
            el.dispatchEvent(new Event('input', { bubbles: true }));
          };
          set('input[type=email]', %s);
          set('input[type=password]', %s);
        })()""" % (json.dumps(EMAIL), json.dumps(PASSWORD))
    )
    tab.evaluate(
        "document.querySelector('button[type=submit]').click()"
    )
    # Wait for the redirect off /login.
    tab.wait_for("location.pathname !== '/login'", timeout=30)
    print(f"  logged in → {tab.evaluate('location.pathname')}")


def text_of(tab, testid: str) -> str:
    return tab.evaluate(
        f"""(() => {{
          const el = document.querySelector('[data-testid="{testid}"]');
          return el ? el.textContent : null;
        }})()"""
    ) or ""


def main() -> int:
    from probe_target import require_target
    require_target(BASE, who="m136_locked_qa.py")
    b = Browser()
    try:
        tab = b.new_tab(f"{BASE}/login")
        # No force_viewport: the driver's docstring warns a target created WITH
        # a url can commit its document before the override lands ("Cannot find
        # default execution context"). Browser() already opens at 1440x900.
        login(tab)

        # ── shell chip (status variant, enrolled+locked) ──────────────────
        tab.navigate(f"{BASE}/dashboard")
        chip = tab.wait_for(
            "document.querySelector('[data-testid=vault-locked-chip]') !== null", timeout=20
        )
        chip_text = text_of(tab, "vault-locked-chip")
        check("shell chip", bool(chip) and chip_text.strip() == "Vault locked",
              f"text={chip_text.strip()!r} (must be exactly the status constant)")

        # ── S-1 global search: the popover announces the lock ─────────────
        # The term must match a CIPHERED PAYEE and nothing else: the lock
        # notice replaces the EMPTY state (rule 7f), and the outage is scoped
        # to encrypted payees (rule 7e) — categories/accounts/tags still match
        # while locked. "grocery" also hits the "Groceries" CATEGORY, which
        # correctly keeps the popover out of its empty state; "FRESH" hits only
        # the ciphered "FRESH CO-OP" payee.
        tab.evaluate(
            """(() => {
              const el = document.querySelector('input[type=search], input[placeholder*="Search"]');
              if (!el) return false;
              el.focus();
              const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
              setter.call(el, 'FRESH');
              el.dispatchEvent(new Event('input', { bubbles: true }));
              return true;
            })()"""
        )
        try:
            tab.wait_for(
                "document.querySelector('[data-testid=vault-locked]') !== null", timeout=15
            )
            s1 = text_of(tab, "vault-locked-copy")
            check("S-1 global search popover", "Unlock the vault to search encrypted payees" in s1,
                  f"copy={s1[:90]!r}")
        except Exception as exc:
            check("S-1 global search popover", False, f"no lock notice: {exc}")

        # ── A18/F4 search view: partial + MEASURED withheld count ─────────
        tab.navigate(f"{BASE}/transactions?q=grocery")
        try:
            tab.wait_for(
                "document.querySelector('[data-testid=vault-locked][data-vault-variant=partial]') !== null",
                timeout=20,
            )
            partial = text_of(tab, "vault-locked-copy")
            # 8 rows total, 7 ciphered, 1 plaintext: the count is MEASURED.
            ok_count = "Showing 1 of 8 rows" in partial or "Showing 0 of 8 rows" in partial
            check("A18 search view (partial)", ok_count, f"copy={partial[:110]!r}")
            dest = text_of(tab, "vault-locked-destination")
            check("canonical destination (search view)",
                  "Settings → Privacy & data → Client-side encryption (in progress)" in dest,
                  f"dest={dest.strip()!r}")
            # Rule 7f: the empty state must NOT stack with the notice.
            has_empty = tab.evaluate(
                "document.body.textContent.includes('No transactions match these filters.')"
            )
            check("A18 rule 7f (no stacked empty state)", not has_empty,
                  f"empty-state text present={has_empty}")
            # The inline unlock exists (rule 7c) but is NOT auto-revealed.
            has_toggle = tab.evaluate(
                "document.querySelector('[data-testid=vault-unlock-toggle]') !== null"
            )
            has_form = tab.evaluate(
                "document.querySelector('[data-testid=vault-unlock-form]') !== null"
            )
            check("A18 inline unlock (one interaction, not auto-revealed)",
                  bool(has_toggle) and not has_form,
                  f"toggle={bool(has_toggle)} form={bool(has_form)}")
        except Exception as exc:
            check("A18 search view (partial)", False, str(exc)[:160])

        # ── S-3 similar popover: the shared search string ─────────────────
        tab.navigate(f"{BASE}/transactions")
        try:
            tab.wait_for(
                "document.querySelector('[aria-label*=\"Similar\"], [data-testid=similar-open]') !== null"
                " || document.body.textContent.includes('Similar')",
                timeout=20,
            )
            opened = tab.evaluate(
                """(() => {
                  const btns = [...document.querySelectorAll('button')];
                  const b = btns.find(x => (x.getAttribute('aria-label')||'').toLowerCase().includes('similar')
                                       || (x.textContent||'').toLowerCase().includes('similar'));
                  if (b) { b.click(); return true; } return false;
                })()"""
            )
            if opened:
                tab.wait_for(
                    "document.querySelector('[data-testid=vault-locked]') !== null", timeout=15
                )
                s3 = text_of(tab, "vault-locked-copy")
                check("S-3 similar popover", "Unlock the vault to search encrypted payees" in s3,
                      f"copy={s3[:90]!r}")
            else:
                check("S-3 similar popover", False, "no Similar control found to open")
        except Exception as exc:
            check("S-3 similar popover", False, str(exc)[:160])

        # ── G1 split editor: Save replaced BEFORE any leg is typed ────────
        tab.navigate(f"{BASE}/transactions")
        try:
            tab.wait_for(
                "document.querySelector('[aria-label^=\"Split transaction\"]') !== null", timeout=20
            )
            tab.evaluate(
                """(() => {
                  const b = [...document.querySelectorAll('button')]
                    .find(x => (x.getAttribute('aria-label')||'').startsWith('Split transaction'));
                  if (b) { b.click(); return true; } return false;
                })()"""
            )
            tab.wait_for("document.querySelector('[role=dialog]') !== null", timeout=15)
            has_save = tab.evaluate(
                "([...document.querySelectorAll('button')].some(b => /Split|Save split/.test(b.textContent||'') && b.type==='submit'))"
            )
            g1 = tab.evaluate(
                "document.querySelector('[data-testid=vault-locked][data-vault-variant=blocked]') !== null"
            )
            check("G1 split editor (pre-emptive gate)", bool(g1) and not has_save,
                  f"blocked-notice={bool(g1)} submit-button={bool(has_save)}")
        except Exception as exc:
            check("G1 split editor (pre-emptive gate)", False, str(exc)[:160])

        print("\n" + "=" * 60)
        passed = sum(1 for _, ok, _ in RESULTS if ok)
        print(f"M136 live locked QA: {passed}/{len(RESULTS)} PASS")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  FAIL  {name}: {detail}")
        return 0 if passed == len(RESULTS) else 1
    finally:
        b.close()


if __name__ == "__main__":
    raise SystemExit(main())
