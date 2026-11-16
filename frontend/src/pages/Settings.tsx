import { useEffect, useMemo, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { Check, Cloud, Cpu, Tags } from "lucide-react";
import { hasErrorCode, me, tokens } from "../api/client";
import { maskAmountsInText, useMasked } from "../lib/mask";
import { NotificationsSection } from "../components/settings/NotificationsSection";
import { ScheduledBackupPanel } from "../components/ScheduledBackupPanel";
import {
  OfxExportRow,
  ScheduledExportPanel,
  vaultState,
} from "../components/ScheduledExportPanel";
import {
  createCategory,
  deleteCategory,
  listCategories,
  updateCategory,
} from "../api/categories";
import type { Category } from "../api/categories";
import {
  changePassword,
  deleteAccount,
  testLlm,
  updateMe,
  updateProfile,
} from "../api/users";
import type {
  ChangePasswordInput,
  DeleteAccountInput,
  LlmProvider,
  ProfileUpdateInput,
  UserUpdateInput,
} from "../api/users";
import { downloadExport, downloadZipExport, getFullExport } from "../api/export";
import { listTransactions, updateTransaction } from "../api/transactions";
import { listAccounts, updateAccount } from "../api/accounts";
import {
  backupSupported,
  deleteBackup,
  hasBackup,
  storeBackup,
} from "../lib/privacyBackup";
import { isUnlocked, loadVault, lockVault } from "../lib/vault";
import { VaultLocked } from "../components/e2ee/VaultLocked";
import { useVaultUnlocked } from "../components/e2ee/useUnlock";
import {
  defaultSyncDeps,
  enrollVaultSynced,
  listVaultEnvelopes,
  revokeEnvelope,
  rotatePassphrase,
  unlockVaultSynced,
} from "../lib/vaultSync";
import { backfillE2EE } from "../lib/backfill";
import { Badge } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import { Card, CardTitle } from "../components/ui/Card";
import { QueryErrorPanel } from "../components/ui/QueryErrorPanel";
import { SkeletonRows } from "../components/ui/Skeleton";
import { Toggle } from "../components/ui/Toggle";
import { fadeUp, staggerContainer } from "../components/ui/motion";
import { useToast } from "../components/ui/Toast";
import { useAuth } from "../auth/AuthContext";
import { useOptionalUser } from "../auth/authUser";
import { isReadOnlySession } from "../auth/readOnlySession";
import {
  DemoLock,
  DemoReason,
  DemoRefusal,
  demoLocks,
  useDemoRefusal,
  useDemoWriteLock,
} from "../components/ui/DemoLock";
import { AccountsSection } from "../components/settings/AccountsSection";
import { PartnerSharingRow, SharingSection } from "../components/settings/SharingSection";
import { SecuritySection } from "../components/settings/SecuritySection";
import { RecurringIntentSection } from "../components/settings/RecurringIntentSection";

/**
 * Settings page (docs/09): LLM provider config (write-only key, gated by
 * privacy mode), category hierarchy editor with in-use guards, rules editor
 * with prefill-from-payee affordance, import-profiles pointer, and the M7
 * privacy / export / encrypted-backup controls (docs/11).
 * Presentation pass: sectioned glass cards, spring-knob toggles, provider
 * badges with glyphs, masked key + animated "saved" check, toast feedback.
 */

const FIELD =
  "field-ring w-full rounded-lg border border-control bg-surface px-2 py-1.5 text-sm outline-none transition-colors placeholder:text-muted hover:border-accent/70";

/** The Account card's single visible reason (docs/47 §4) — one per panel. */
const ACCOUNT_DEMO_REASON_ID = "account-demo-reason";
const ACCOUNT_DEMO_REASON =
  "The demo account is read-only — the server refuses these writes.";

export function Settings() {
  return (
    <motion.section
      variants={staggerContainer(0.06)}
      initial="hidden"
      animate="show"
      className="space-y-6"
    >
      <motion.div variants={fadeUp}>
        <h1 className="text-xl font-semibold tracking-tight">Settings</h1>
      </motion.div>
      <motion.div variants={fadeUp}>
        <AccountSection />
      </motion.div>
      <motion.div variants={fadeUp}>
        <SecuritySection />
      </motion.div>
      <motion.div variants={fadeUp}>
        <LlmForm />
      </motion.div>
      <motion.div variants={fadeUp}>
        <AccountsSection />
      </motion.div>
      <motion.div variants={fadeUp}>
        <PrivacySection />
      </motion.div>

      {/* M90 (docs/43 §13): owner consent card; the partner's read-only row
          below renders only when GET /partner/state has an active link. */}
      <motion.div variants={fadeUp}>
        <SharingSection />
      </motion.div>
      <motion.div variants={fadeUp}>
        <PartnerSharingRow />
      </motion.div>

      <motion.div variants={fadeUp}>
        <NotificationsSection />
      </motion.div>

      <motion.div variants={fadeUp}>
        <CurrencyTrackerSection />
      </motion.div>
      {/* M174 (docs/09 §Tags that mark recurring): the tags the user NAMES as
          marking recurring intent — placed beside the category manager, since
          both are "the vocabulary my own data speaks". */}
      <motion.div variants={fadeUp}>
        <RecurringIntentSection />
      </motion.div>
      <motion.div variants={fadeUp}>
        <CategoryManager />
      </motion.div>
      <motion.div variants={fadeUp}>
        <ImportProfilesPointer />
      </motion.div>
    </motion.section>
  );
}


// --- Account (self-service): name, email, password, delete (docs/08) --------

export function AccountSection() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const navigate = useNavigate();
  const { setUser } = useAuth();
  const meQuery = useQuery({ queryKey: ["me"], queryFn: me });
  const user = meQuery.data;
  const isDemo = isReadOnlySession(user);
  // M185 F-2 residual (docs/47 §4): a refused demo write is ONE toast.info +
  // ONE local role="status" element — never toast.error (which renders AS
  // role="alert"). Declared before the mutations that route through it.
  const demo = useDemoRefusal();
  // Every dispatching control below wears the padlock on the demo tenant
  // (each targets a non-exempt write path) and binds this card's one reason.
  const profileLocked = useDemoWriteLock("PATCH", "/users/me/profile");
  const passwordLocked = useDemoWriteLock("POST", "/users/me/change-password");
  const deleteLocked = useDemoWriteLock("DELETE", "/users/me");
  const accountLocked = profileLocked || passwordLocked || deleteLocked;

  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [emailPassword, setEmailPassword] = useState("");

  const [curPw, setCurPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [confirmPw, setConfirmPw] = useState("");

  const [confirmText, setConfirmText] = useState("");
  const [deletePw, setDeletePw] = useState("");
  const [showDelete, setShowDelete] = useState(false);

  // Sync controlled inputs once the current user loads.
  useEffect(() => {
    if (user) {
      setEmail(user.email);
      setName(user.name ?? "");
    }
  }, [user]);

  const profileMutation = useMutation({
    mutationFn: (input: ProfileUpdateInput) => updateProfile(input),
    onSuccess: (updated) => {
      void queryClient.invalidateQueries({ queryKey: ["me"] });
      setUser(updated);
      setEmailPassword("");
      toast.success("Profile updated", "Your changes were saved.");
    },
    onError: (err) => {
      if (demo.onError("Save profile", err)) return;
      toast.error("Could not update profile", err.message);
    },
  });

  const passwordMutation = useMutation({
    mutationFn: (input: ChangePasswordInput) => changePassword(input),
    onSuccess: (res) => {
      tokens.storeAccess(res.access_token);
      setUser(res.user);
      setCurPw("");
      setNewPw("");
      setConfirmPw("");
      toast.success("Password changed", "All other devices were signed out.");
    },
    onError: (err) => {
      if (demo.onError("Change password", err)) return;
      toast.error("Could not change password", err.message);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (input: DeleteAccountInput) => deleteAccount(input),
    onSuccess: () => {
      tokens.clear();
      toast.success("Account deleted", "Your data has been removed.");
      navigate("/login");
    },
    onError: (err) => {
      if (demo.onError("Delete account", err)) return;
      toast.error("Could not delete account", err.message);
    },
  });

  if (meQuery.isPending) {
    return (
      <Card className="max-w-xl p-4">
        <CardTitle>Account</CardTitle>
        <SkeletonRows rows={3} />
      </Card>
    );
  }
  if (meQuery.isError || user === undefined) {
    return (
      <Card className="max-w-xl p-4">
        <CardTitle>Account</CardTitle>
        <QueryErrorPanel query={meQuery} message={meQuery.error?.message ?? "Not loaded"} />
      </Card>
    );
  }

  const emailChanged = email.trim() !== user.email;
  const nameChanged = name.trim() !== (user.name ?? "");
  const canSaveProfile =
    (emailChanged || nameChanged) &&
    !profileMutation.isPending &&
    (!emailChanged || emailPassword.length > 0);

  const pwMismatch = newPw.length > 0 && newPw !== confirmPw;
  const pwTooShort = newPw.length > 0 && newPw.length < 8;
  const canChangePassword =
    curPw.length > 0 && newPw.length >= 8 && newPw === confirmPw && !passwordMutation.isPending;

  const canDelete =
    showDelete && deletePw.length > 0 && confirmText === "DELETE" && !deleteMutation.isPending;

  return (
    <Card hover className="max-w-xl space-y-5 p-4">
      <CardTitle>Account</CardTitle>
      {demo.refusal !== null && (
        <DemoRefusal id={demo.refusalId} action={demo.refusal} />
      )}
      {isDemo && (
        <p className="text-xs leading-relaxed text-muted">
          The demo account is read-only — profile, password and account changes are not
          saved. The form stays live so you can see how it works.
        </p>
      )}
      {/* docs/47 §4: the ONE visible reason this card's padlocks bind to. */}
      {accountLocked && <DemoReason id={ACCOUNT_DEMO_REASON_ID}>{ACCOUNT_DEMO_REASON}</DemoReason>}

      {/* Profile: name + email */}
      <div className="space-y-3">
        <div className="space-y-1">
          <label className="text-xs font-medium text-muted" htmlFor="name">
            Display name
          </label>
          <input
            id="name"
            className={FIELD}
            value={name}
            placeholder="Optional"
            onChange={(e) => setName(e.target.value)}
          />
        </div>
        <div className="space-y-1">
          <label className="text-xs font-medium text-muted" htmlFor="email">
            Email
          </label>
          <input
            id="email"
            type="email"
            className={FIELD}
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </div>
        {emailChanged && (
          <div className="space-y-1">
            <label className="text-xs font-medium text-muted" htmlFor="email-pw">
              Current password (required to change email)
            </label>
            <input
              id="email-pw"
              type="password"
              className={FIELD}
              value={emailPassword}
              onChange={(e) => setEmailPassword(e.target.value)}
            />
          </div>
        )}
        <div>
          <Button
            variant="primary"
            disabled={!canSaveProfile}
            aria-describedby={profileLocked ? ACCOUNT_DEMO_REASON_ID : undefined}
            onClick={() => {
              const input: ProfileUpdateInput = {};
              if (nameChanged) input.name = name.trim();
              if (emailChanged) {
                input.email = email.trim();
                input.current_password = emailPassword;
              }
              profileMutation.mutate(input);
            }}
          >
            {profileLocked && <DemoLock />}
            Save profile
          </Button>
        </div>
      </div>

      {/* Change password */}
      <div className="space-y-3 border-t border-veil/[0.06] pt-4">
        <p className="text-xs font-medium text-muted">Change password</p>
        <div className="space-y-1">
          <label className="text-xs font-medium text-muted" htmlFor="cur-pw">
            Current password
          </label>
          <input
            id="cur-pw"
            type="password"
            className={FIELD}
            value={curPw}
            onChange={(e) => setCurPw(e.target.value)}
          />
        </div>
        <div className="space-y-1">
          <label className="text-xs font-medium text-muted" htmlFor="new-pw">
            New password
          </label>
          <input
            id="new-pw"
            type="password"
            className={FIELD}
            value={newPw}
            onChange={(e) => setNewPw(e.target.value)}
          />
        </div>
        <div className="space-y-1">
          <label className="text-xs font-medium text-muted" htmlFor="confirm-pw">
            Confirm new password
          </label>
          <input
            id="confirm-pw"
            type="password"
            className={FIELD}
            value={confirmPw}
            onChange={(e) => setConfirmPw(e.target.value)}
          />
        </div>
        {(pwMismatch || pwTooShort) && (
          <p className="text-sm text-danger">
            {pwMismatch
              ? "New password and confirmation do not match."
              : "New password must be at least 8 characters."}
          </p>
        )}
        <div>
          <Button
            variant="primary"
            disabled={!canChangePassword}
            aria-describedby={passwordLocked ? ACCOUNT_DEMO_REASON_ID : undefined}
            onClick={() =>
              passwordMutation.mutate({
                current_password: curPw,
                new_password: newPw,
              })
            }
          >
            {passwordLocked && <DemoLock />}
            Change password
          </Button>
        </div>
      </div>

      {/* Danger zone */}
      <div className="space-y-3 border-t border-danger/20 pt-4">
        <div className="flex items-center justify-between">
          <p className="text-xs font-semibold text-danger">Delete account</p>
          {!showDelete && (
            <Button
              variant="danger"
              onClick={() => {
                setShowDelete(true);
                setConfirmText("");
                setDeletePw("");
              }}
            >
              Delete account
            </Button>
          )}
        </div>
        <p className="text-xs leading-relaxed text-muted">
          Permanently removes your account and all of its data (accounts, securities, trades,
          transactions, categories and more) in a single action. This cannot be undone.
        </p>
        {showDelete && (
          <div className="space-y-3">
            <div className="space-y-1">
              <label className="text-xs font-medium text-muted" htmlFor="delete-pw">
                Current password
              </label>
              <input
                id="delete-pw"
                type="password"
                className={FIELD}
                value={deletePw}
                onChange={(e) => setDeletePw(e.target.value)}
              />
            </div>
            <div className="space-y-1">
              <label className="text-xs font-medium text-muted" htmlFor="confirm-text">
                Type DELETE to confirm
              </label>
              <input
                id="confirm-text"
                className={FIELD}
                value={confirmText}
                onChange={(e) => setConfirmText(e.target.value)}
              />
            </div>
            <div className="flex gap-2">
              <Button
                variant="danger"
                disabled={!canDelete}
                aria-describedby={deleteLocked ? ACCOUNT_DEMO_REASON_ID : undefined}
                onClick={() => deleteMutation.mutate({ current_password: deletePw })}
              >
                {deleteLocked && <DemoLock />}
                Delete
              </Button>
              <Button variant="ghost" onClick={() => setShowDelete(false)}>
                Cancel
              </Button>
            </div>
          </div>
        )}
      </div>
    </Card>
  );
}

// --- LLM provider -------------------------------------------------------------

const LOOPBACK_BASE_URL = "http://localhost:11434/v1";

/** True for localhost / 127.x / ::1 — the only destinations privacy mode
 * permits (docs/11 step 5; the server enforces this at the egress seam). */
function isLoopbackBaseUrl(url: string): boolean {
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return false;
    const host = parsed.hostname.replace(/^\[|\]$/g, "").toLowerCase();
    return host === "localhost" || host === "::1" || /^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(host);
  } catch {
    return false;
  }
}

/** The only base URL that may be sent/stored while privacy mode is on: keep a
 * stored loopback URL, replace anything else (a stored cloud URL, a typo,
 * empty) with the local Ollama default — a non-loopback destination must
 * never be re-saved or re-sent under privacy (audit H3, docs/11 step 5). */
function loopbackBase(url: string): string {
  return isLoopbackBaseUrl(url) ? url : LOOPBACK_BASE_URL;
}

/**
 * M185 W2 (docs/47 §4, PM binding decision): on the demo tenant `Test
 * connection` is disabled BEFORE dispatch with this exact local copy — it is
 * a live probe of an EXTERNAL provider, so firing it would only burn a
 * round-trip the server would refuse (403 `demo_account_restricted`), and a
 * padlock would be the wrong affordance (no refusal ever comes back to
 * explain). No exemption is added server-side; non-demo behaviour and the
 * privacy-mode egress check are untouched.
 */
export const LLM_TEST_DEMO_REASON =
  "Test connection is unavailable in the demo. The demo account blocks POST requests to keep the sample data read-only.";

/** The LLM card's single visible reason (docs/47 §4) — one per panel. */
const LLM_SAVE_REASON_ID = "llm-save-demo-reason";
const LLM_SAVE_REASON =
  "The demo account is read-only — saving LLM settings would write them.";

export function LlmForm() {
  const queryClient = useQueryClient();
  const toast = useToast();
  // M139: Settings is a D4 exempt surface (the user is configuring things),
  // but the LLM test probe's echo is D3-masked — so this ONE component
  // subscribes, and only the test-result line below transforms.
  useMasked();
  const meQuery = useQuery({ queryKey: ["me"], queryFn: me });
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  // M185 F-2 residual (docs/47 §4): the Save write (PATCH /users/me) wears the
  // padlock and routes its REAL 403 through the one refusal element below.
  // The session SCOPE (not `useAuth`) keeps this form renderable in isolation:
  // no provider ⇒ no session ⇒ no lock (auth/authUser.ts).
  const demo = useDemoRefusal();
  const saveLocked = demoLocks(useOptionalUser(), "PATCH", "/users/me");

  const user = meQuery.data;
  // M185 W2: the demo tenant's probe never leaves the browser.
  const isDemo = isReadOnlySession(user);
  const [provider, setProvider] = useState<LlmProvider | null>(null);
  const [baseUrl, setBaseUrl] = useState<string | null>(null);
  const [model, setModel] = useState<string | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [testResult, setTestResult] = useState<{ ok: boolean; detail: string } | null>(null);
  // All hooks run unconditionally; loading/empty states render below.
  const save = useMutation({
    mutationFn: () => updateMe(buildPatch()),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["me"] });
      setApiKey("");
      setError(null);
      setSaved(true);
      toast.success("Settings saved");
    },
    onError: (err) => {
      setSaved(false);
      // A refused demo write is not a save failure (docs/47 §4).
      if (demo.onError("Save LLM settings", err)) {
        setError(null);
        return;
      }
      setError(err.message);
      toast.error("Save failed", err.message);
    },
  });
  const test = useMutation({
    mutationFn: () =>
      testLlm({
        llm_provider: effectiveProvider,
        llm_base_url: effectiveBaseUrl === "" ? null : effectiveBaseUrl,
        llm_model: currentModel === "" ? null : currentModel,
        llm_api_key: apiKey === "" ? undefined : apiKey,
      }),
    onSuccess: (data) => {
      setTestResult({ ok: true, detail: `Connected · ${data.model}` });
      setError(null);
    },
    onError: (err) => {
      setTestResult({ ok: false, detail: err.message });
    },
  });

  if (meQuery.isPending) {
    return (
      <Card className="max-w-xl p-4">
        <CardTitle>LLM provider</CardTitle>
        <SkeletonRows rows={3} />
      </Card>
    );
  }
  if (meQuery.isError || user === undefined) {
    return (
      <Card className="max-w-xl p-4">
        <QueryErrorPanel query={meQuery} message={meQuery.error?.message ?? "Not loaded"} />
      </Card>
    );
  }

  // Local edits shadow the server values; null = untouched so a refetch
  // after PATCH repopulates without fighting stale local state.
  const currentProvider = provider ?? (user.llm_provider as LlmProvider);
  const currentBaseUrl = baseUrl ?? user.llm_base_url ?? "";
  const currentModel = model ?? user.llm_model ?? "";
  // Privacy-mode gating (docs/11 step 5, client-side mirror of the server's
  // privacy_mode_requires_local_llm 409): provider restricted to ollama,
  // cloud API-key and base-URL inputs hidden, and any stored cloud base URL
  // swapped for the loopback default so it is never re-sent under privacy
  // (audit H3 — the server rejects non-loopback URLs with 422).
  const privacyMode = user.privacy_mode === true;
  const effectiveProvider: LlmProvider = privacyMode ? "ollama" : currentProvider;
  const effectiveBaseUrl = privacyMode ? loopbackBase(currentBaseUrl) : currentBaseUrl;

  // Provider choices as glyph badges (ui-contract Settings must-have). The
  // cloud option is hidden under privacy mode exactly like the old select.
  const providers: { value: LlmProvider; label: string; icon: typeof Cpu; hint: string }[] = [
    { value: "ollama", label: "Ollama", icon: Cpu, hint: "local · private" },
    ...(privacyMode
      ? []
      : [
          {
            value: "openai_compatible" as LlmProvider,
            label: "OpenAI-compatible",
            icon: Cloud,
            hint: "cloud · needs key",
          },
        ]),
  ];

  return (
    <Card hover className="max-w-xl p-4">
      <CardTitle>LLM provider</CardTitle>
      {demo.refusal !== null && (
        <DemoRefusal id={demo.refusalId} action={demo.refusal} />
      )}
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        {/* Provider select rendered as logo-ish badges. */}
        <div className="flex flex-wrap gap-2" role="radiogroup" aria-label="Provider">
          {providers.map(({ value, label, icon: Icon, hint }) => {
            const active = effectiveProvider === value;
            return (
              <button
                key={value}
                type="button"
                role="radio"
                aria-checked={active}
                onClick={() => setProvider(value)}
                className={`relative flex items-center gap-2.5 rounded-lg border px-3 py-2 text-left outline-none transition-all duration-200 focus-visible:ring-2 focus-visible:ring-accent ${
                  active
                    ? "border-accent/50 bg-accent/10 shadow-glow-sm"
                    : "border-control hover:border-accent/70 hover:bg-veil/[0.04]"
                }`}
              >
                <span
                  className={`flex size-7 items-center justify-center rounded-md ${
                    active ? "bg-accent/20 text-accent" : "bg-veil/[0.06] text-muted"
                  }`}
                >
                  <Icon className="size-4" aria-hidden />
                </span>
                <span>
                  <span className={`block text-sm font-medium ${active ? "text-text" : "text-muted"}`}>
                    {label}
                  </span>
                  <span className="block text-[10px] uppercase tracking-wider text-muted">{hint}</span>
                </span>
              </button>
            );
          })}
        </div>
        {!privacyMode && (
          <label className="block text-sm">
            <span className="mb-1 block text-muted">Base URL</span>
            <input
              value={currentBaseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="http://localhost:11434/v1"
              className={FIELD}
            />
            <p className="mt-1 text-[11px] leading-snug text-muted">
              Ollama uses the OpenAI-compatible path — include{" "}
              <code className="text-accent">/v1</code> (e.g.{" "}
              <code className="text-accent">http://host:11434/v1</code>). The app appends{" "}
              <code className="text-accent">/chat/completions</code> automatically.
            </p>
          </label>
        )}
        <label className="block text-sm">
          <span className="mb-1 block text-muted">Model</span>
          <input
            value={currentModel}
            onChange={(e) => setModel(e.target.value)}
            placeholder="qwen2.5:7b-instruct"
            className={FIELD}
          />
        </label>
        {!privacyMode && (
          <label className="block text-sm">
            <span className="mb-1 flex items-center gap-2 text-muted">
              API key (write-only)
              <AnimatePresence>
                {saved && (
                  <motion.span
                    initial={{ opacity: 0, x: -6 }}
                    animate={{ opacity: 1, x: 0 }}
                    exit={{ opacity: 0 }}
                    className="inline-flex items-center gap-1 text-[11px] font-medium normal-case tracking-normal text-gain"
                  >
                    <Check className="size-3.5" aria-hidden />
                    saved
                  </motion.span>
                )}
              </AnimatePresence>
            </span>
            {/* type=password renders the masked-dot treatment. */}
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="Leave blank to keep the stored key"
              autoComplete="off"
              className={`${FIELD} tracking-widest`}
            />
          </label>
        )}
        {error !== null && <p className="text-sm text-danger">{error}</p>}
        {testResult !== null && (
          <p className={`text-sm ${testResult.ok ? "text-gain" : "text-danger"}`}>
            {/* M139 (D3): the LLM test probe's detail can echo an amount, so it
                runs through the text masker like the notification bodies do. */}
            {maskAmountsInText(testResult.detail)}
          </p>
        )}
        <div className="flex items-center gap-2">
          <Button
            type="submit"
            variant="primary"
            loading={save.isPending}
            aria-describedby={saveLocked ? LLM_SAVE_REASON_ID : undefined}
          >
            {saveLocked && <DemoLock />}
            Save
          </Button>
          <Button
            type="button"
            variant="ghost"
            loading={test.isPending}
            // M185 W2: disabled BEFORE dispatch — the single named exception
            // to the demo guard's "stays enabled" rule (docs/47 §4).
            disabled={isDemo}
            aria-describedby={isDemo ? "llm-test-disabled-reason" : undefined}
            onClick={() => {
              setTestResult(null);
              test.mutate();
            }}
          >
            Test connection
          </Button>
        </div>
        {/* docs/47 §4: the ONE visible reason the locked Save binds to. */}
        {saveLocked && <DemoReason id={LLM_SAVE_REASON_ID}>{LLM_SAVE_REASON}</DemoReason>}
        {isDemo && (
          <p
            id="llm-test-disabled-reason"
            data-testid="llm-test-disabled-reason"
            className="max-w-xl text-xs text-muted"
          >
            {LLM_TEST_DEMO_REASON}
          </p>
        )}
      </form>
    </Card>
  );

  /** Hoisted so the mutation above can call it; reads this render's state. */
  function buildPatch(): Parameters<typeof updateMe>[0] {
    const patch: Parameters<typeof updateMe>[0] = {
      llm_provider: effectiveProvider,
      llm_base_url: effectiveBaseUrl === "" ? null : effectiveBaseUrl,
      llm_model: currentModel === "" ? null : currentModel,
    };
    // Write-only field: only send when the user typed a new key (docs/02);
    // suppressed under privacy mode where the input is hidden.
    if (!privacyMode && apiKey !== "") {
      patch.llm_api_key = apiKey;
    }
    return patch;
  }
}

// --- Privacy, export & encrypted backup (M7, docs/11) -------------------------

/** The Privacy card's single visible reason (docs/47 §4) — one per panel. */
const PRIVACY_DEMO_REASON_ID = "privacy-mode-demo-reason";
const PRIVACY_DEMO_REASON =
  "The demo account is read-only — privacy mode cannot be changed.";

export function PrivacySection() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const { setUser } = useAuth();
  const meQuery = useQuery({ queryKey: ["me"], queryFn: me });
  const user = meQuery.data;
  const [error, setError] = useState<string | null>(null);
  // M185 F-2 residual (docs/47 §4): the toggle's action IS the blocked write
  // (PATCH /users/me), so it wears the padlock and its REAL 403 becomes one
  // refusal element + one toast.info — never toast.error.
  const demo = useDemoRefusal();
  const privacyLocked = useDemoWriteLock("PATCH", "/users/me");

  const setPrivacyMode = useMutation({
    mutationFn: (enabled: boolean) => {
      if (!enabled) {
        return updateMe({ privacy_mode: false });
      }
      const stored = user?.llm_base_url ?? "";
      const patch: UserUpdateInput = { privacy_mode: true };
      // Server rejects privacy_mode=true combined with
      // llm_provider=openai_compatible (409 privacy_mode_requires_local_llm);
      // mirror docs/11 step 5 client-side by switching to Ollama in the same
      // PATCH.
      if (user?.llm_provider === "openai_compatible") {
        patch.llm_provider = "ollama";
      }
      // Audit H3: enabling privacy must not leave a stored cloud base URL
      // behind — overwrite it with the loopback default in the same PATCH
      // (docs/11 step 5). Already-loopback or unset URLs are left alone.
      if (stored !== "" && !isLoopbackBaseUrl(stored)) {
        patch.llm_base_url = LOOPBACK_BASE_URL;
      }
      return updateMe(patch);
    },
    onSuccess: (updatedUser, enabled) => {
      setError(null);
      setUser(updatedUser);
      queryClient.setQueryData(["me"], updatedUser);
      void queryClient.invalidateQueries({ queryKey: ["me"] });
      if (enabled) {
        toast.success("Privacy mode enabled", "External calls are now blocked.");
      } else {
        toast.info("Privacy mode disabled", "Cloud features are available again.");
      }
    },
    onError: (err) => {
      // A refused demo write is not a failed privacy toggle (docs/47 §4).
      if (demo.onError("Change privacy mode", err)) {
        setError(null);
        return;
      }
      setError(err.message);
      toast.error("Could not change privacy mode", err.message);
    },
  });

  if (meQuery.isPending) {
    return (
      <Card className="max-w-xl p-4">
        <CardTitle>Privacy &amp; data</CardTitle>
        <SkeletonRows rows={2} />
      </Card>
    );
  }
  if (meQuery.isError || user === undefined) {
    return (
      <Card className="max-w-xl p-4">
        <QueryErrorPanel query={meQuery} message={meQuery.error?.message ?? "Not loaded"} />
      </Card>
    );
  }

  return (
    <Card hover className="max-w-xl p-4">
      <CardTitle>Privacy &amp; data</CardTitle>
      {demo.refusal !== null && (
        <DemoRefusal id={demo.refusalId} action={demo.refusal} />
      )}
      <div className="flex items-center gap-3">
        {/* Spring-knob toggle (ui-contract Settings must-have). docs/47 §4:
            the switch's ACTION is the blocked write, so it keeps its glyph
            bound to the visible reason BELOW it and stays enabled. */}
        <Toggle
          checked={user.privacy_mode === true}
          disabled={setPrivacyMode.isPending}
          onChange={(checked) => setPrivacyMode.mutate(checked)}
          ariaLabel="Privacy mode"
          ariaDescribedBy={privacyLocked ? PRIVACY_DEMO_REASON_ID : undefined}
          label={privacyLocked ? <DemoLock /> : undefined}
        />
        <Badge tone={user.privacy_mode ? "accent" : "neutral"}>
          {user.privacy_mode ? "on — cloud features blocked" : "off"}
        </Badge>
      </div>
      {/* docs/47 §4: the ONE visible reason the locked toggle binds to. */}
      {privacyLocked && <DemoReason id={PRIVACY_DEMO_REASON_ID}>{PRIVACY_DEMO_REASON}</DemoReason>}
      <p className="mt-3 text-xs leading-relaxed text-muted">
        Blocks external calls (market-data lookups, BoC Valet fetches, hosted LLMs). Manual
        rates/prices, cached market data and everything below keep working.
      </p>
      {error !== null && <p className="mt-2 text-sm text-danger">{error}</p>}
      <div className="mt-3 space-y-4 border-t border-veil/[0.06] pt-4">
        <ExportPanel userId={user.id} />
        <BackupPanel userId={user.id} />
        <ScheduledBackupPanel />
        <ScheduledExportPanel userId={user.id} />
        <EncryptionPanel userId={user.id} />
        <BackfillPanel />
      </div>
    </Card>
  );
}

/** The Currency Tracker card's single visible reason (docs/47 §4). */
const CURRENCY_TRACKER_REASON_ID = "currency-tracker-demo-reason";
const CURRENCY_TRACKER_REASON =
  "The demo account is read-only — Currency Tracker cannot be changed.";

export function CurrencyTrackerSection() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const { setUser } = useAuth();
  const meQuery = useQuery({ queryKey: ["me"], queryFn: me });
  const user = meQuery.data;
  const [error, setError] = useState<string | null>(null);
  // M185 F-2 residual (docs/47 §4): the toggle's action IS the blocked write
  // (PATCH /users/me) — padlock + the one refusal element, never toast.error.
  const demo = useDemoRefusal();
  const trackerLocked = useDemoWriteLock("PATCH", "/users/me");

  const setCurrencyTracker = useMutation({
    mutationFn: (enabled: boolean) => updateMe({ currency_tracker_enabled: enabled }),
    onSuccess: (data) => {
      setError(null);
      setUser(data);
      void queryClient.invalidateQueries({ queryKey: ["me"] });
      if (data.currency_tracker_enabled) {
        toast.success("Currency Tracker enabled", "The Currencies tab is now visible.");
      } else {
        toast.info("Currency Tracker disabled", "The Currencies tab is hidden.");
      }
    },
    onError: (err) => {
      // A refused demo write is not a failed Currency Tracker toggle.
      if (demo.onError("Change Currency Tracker", err)) {
        setError(null);
        return;
      }
      setError(err.message);
      toast.error("Could not change Currency Tracker", err.message);
    },
  });

  if (meQuery.isPending) {
    return (
      <Card className="max-w-xl p-4">
        <CardTitle>Currency Tracker</CardTitle>
        <SkeletonRows rows={2} />
      </Card>
    );
  }
  if (meQuery.isError || user === undefined) {
    return (
      <Card className="max-w-xl p-4">
        <QueryErrorPanel query={meQuery} message={meQuery.error?.message ?? "Not loaded"} />
      </Card>
    );
  }

  return (
    <Card hover className="max-w-xl p-4">
      <CardTitle>Currency Tracker</CardTitle>
      {demo.refusal !== null && (
        <DemoRefusal id={demo.refusalId} action={demo.refusal} />
      )}
      <div className="flex items-center gap-3">
        <Toggle
          checked={user.currency_tracker_enabled === true}
          disabled={setCurrencyTracker.isPending}
          onChange={(checked) => setCurrencyTracker.mutate(checked)}
          ariaLabel="Currency Tracker"
          ariaDescribedBy={trackerLocked ? CURRENCY_TRACKER_REASON_ID : undefined}
          label={trackerLocked ? <DemoLock /> : undefined}
        />
        <Badge tone={user.currency_tracker_enabled ? "accent" : "neutral"}>
          {user.currency_tracker_enabled ? "on" : "off"}
        </Badge>
      </div>
      {/* docs/47 §4: the ONE visible reason the locked toggle binds to. */}
      {trackerLocked && (
        <DemoReason id={CURRENCY_TRACKER_REASON_ID}>{CURRENCY_TRACKER_REASON}</DemoReason>
      )}
      <p className="mt-3 text-xs leading-relaxed text-muted">
        Shows or hides the Currencies tab (the Tools sidebar section). Turn off to keep the
        read-only FX board out of the navigation.
      </p>
      {error !== null && <p className="mt-2 text-sm text-danger">{error}</p>}
    </Card>
  );
}

function ExportPanel({ userId }: { userId: string | number }) {
  const toast = useToast();
  // Per-user localStorage keys so the last-export timestamps survive reloads.
  const storageKey = `last_export_user_${userId}`;
  const [lastExportAt, setLastExportAt] = useState<string | null>(() =>
    localStorage.getItem(storageKey),
  );
  const [lastZipAt, setLastZipAt] = useState<string | null>(() =>
    localStorage.getItem(`${storageKey}_zip`),
  );
  const [error, setError] = useState<string | null>(null);

  const download = useMutation({
    mutationFn: () => downloadExport(),
    onSuccess: (completedAt) => {
      localStorage.setItem(storageKey, completedAt);
      setLastExportAt(completedAt);
      setError(null);
      toast.success("Export ready", "fin-tracker-export JSON downloaded.");
    },
    onError: (err) => {
      setError(err.message);
      toast.error("Export failed", err.message);
    },
  });

  // M75: the portable ZIP — one CSV per table plus a JSON manifest, committed
  // rows only (it mirrors what the app shows; the JSON above keeps everything
  // because a backup has to). `vaultState()` decides E2EE marker vs plaintext
  // for cipher-backed fields (docs/11 § Export & the vault).
  const downloadZip = useMutation({
    mutationFn: () => downloadZipExport(vaultState()),
    onSuccess: (completedAt) => {
      localStorage.setItem(`${storageKey}_zip`, completedAt);
      setLastZipAt(completedAt);
      setError(null);
      toast.success("ZIP ready", "One CSV per table plus manifest.json downloaded.");
    },
    onError: (err) => {
      setError(err.message);
      toast.error("ZIP export failed", err.message);
    },
  });

  return (
    <div className="space-y-2">
      <h3 className="text-sm font-medium">Export my data</h3>
      <p className="text-xs leading-relaxed text-muted">
        <span className="font-medium text-text">ZIP (portable)</span> — one CSV per table plus a
        JSON manifest, committed rows only: fin-tracker-export-&lt;date&gt;.zip.{" "}
        <span className="font-medium text-text">JSON (machine)</span> — every table, staged rows
        included, for backups and scripting: fin-tracker-export-&lt;date&gt;.json. Both work
        regardless of privacy mode.
      </p>
      <div className="flex flex-wrap gap-2">
        <Button
          variant="primary"
          size="sm"
          loading={downloadZip.isPending}
          onClick={() => downloadZip.mutate()}
        >
          Download ZIP
        </Button>
        <Button
          variant="outline"
          size="sm"
          loading={download.isPending}
          onClick={() => download.mutate()}
        >
          Download JSON
        </Button>
      </div>
      <OfxExportRow />
      {(lastZipAt !== null || lastExportAt !== null) && (
        <p className="text-xs text-muted tabular">
          {lastZipAt !== null && <>ZIP: {new Date(lastZipAt).toLocaleString()}</>}
          {lastZipAt !== null && lastExportAt !== null && " · "}
          {lastExportAt !== null && <>JSON: {new Date(lastExportAt).toLocaleString()}</>}
        </p>
      )}
      {error !== null && <p className="text-sm text-danger">{error}</p>}
    </div>
  );
}

function BackupPanel({ userId }: { userId: string | number }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const storageUserId = String(userId);
  const supported = backupSupported();
  const metaQuery = useQuery({
    queryKey: ["backupMeta", storageUserId],
    queryFn: () => hasBackup(storageUserId),
    enabled: supported,
  });
  const stored = metaQuery.data ?? null;
  const [prompting, setPrompting] = useState(false);
  const [passphrase, setPassphrase] = useState("");
  const [confirmPassphrase, setConfirmPassphrase] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function disable(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await deleteBackup(storageUserId);
      await queryClient.invalidateQueries({ queryKey: ["backupMeta", storageUserId] });
      setPrompting(false);
      toast.info("Encrypted backup removed");
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setError(message);
      toast.error("Backup removal failed", message);
    } finally {
      setBusy(false);
    }
  }

  async function enable(): Promise<void> {
    if (passphrase.length < 8) {
      setError("Passphrase must be at least 8 characters.");
      return;
    }
    if (passphrase !== confirmPassphrase) {
      setError("Passphrases do not match.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      // Snapshot fetched fresh at enable time (docs/11 step 4); the export
      // endpoint has no LLM/market-data dependency (docs/11 step 7).
      const snapshot = await getFullExport();
      await storeBackup(storageUserId, snapshot, passphrase);
      await queryClient.invalidateQueries({ queryKey: ["backupMeta", storageUserId] });
      setPrompting(false);
      setPassphrase("");
      setConfirmPassphrase("");
      toast.success("Encrypted backup saved");
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setError(message);
      toast.error("Backup failed", message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-2 border-t border-veil/[0.06] pt-4">
      <h3 className="text-sm font-medium">Encrypted browser backup</h3>
      {!supported ? (
        <p className="text-xs leading-relaxed text-muted">
          Web Crypto or IndexedDB is unavailable in this browser, so encrypted backups are
          disabled — the download above still works.
        </p>
      ) : (
        <>
          <Toggle
            checked={stored !== null || prompting}
            disabled={busy || metaQuery.isPending}
            ariaLabel="Keep encrypted backup in this browser"
            onChange={(checked) => {
              setError(null);
              if (checked) {
                setPrompting(true);
              } else {
                void disable();
              }
            }}
            label={<span className="text-sm">Keep encrypted backup in this browser</span>}
          />
          {stored !== null && !prompting && (
            <p className="text-xs text-muted tabular">
              Last backup: {new Date(stored.createdAt).toLocaleString()}
            </p>
          )}
          <AnimatePresence>
            {prompting && (
              <motion.form
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: "auto" }}
                exit={{ opacity: 0, height: 0 }}
                transition={{ duration: 0.25, ease: [0.22, 1, 0.36, 1] }}
                className="space-y-2 overflow-hidden rounded-lg border border-veil/[0.08] p-3"
                onSubmit={(e) => {
                  e.preventDefault();
                  void enable();
                }}
              >
                <p className="text-xs text-muted">
                  Choose a passphrase (min 8 characters; PBKDF2 + AES-GCM; never stored).
                </p>
                <input
                  type="password"
                  value={passphrase}
                  onChange={(e) => setPassphrase(e.target.value)}
                  placeholder="Passphrase"
                  minLength={8}
                  className={`${FIELD} tracking-widest`}
                />
                <input
                  type="password"
                  value={confirmPassphrase}
                  onChange={(e) => setConfirmPassphrase(e.target.value)}
                  placeholder="Confirm passphrase"
                  minLength={8}
                  className={`${FIELD} tracking-widest`}
                />
                <div className="flex items-center gap-2 pt-1">
                  <Button
                    type="submit"
                    variant="primary"
                    size="sm"
                    loading={busy}
                    disabled={passphrase.length < 8}
                  >
                    Save backup
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    disabled={busy}
                    onClick={() => {
                      setPrompting(false);
                      setPassphrase("");
                      setConfirmPassphrase("");
                      setError(null);
                    }}
                  >
                    Cancel
                  </Button>
                </div>
              </motion.form>
            )}
          </AnimatePresence>
          <p className="text-xs leading-relaxed text-muted">
            Stored only in this browser as {"{salt, iv, ciphertext, createdAt}"} — the passphrase
            is never persisted. Write-only for this copy: it stays behind a passphrase you chose
            here, independent of the encryption passphrase. Restoring belongs to the scheduled
            backup below, whose artifact is keyed by your vault.
          </p>
        </>
      )}
      {error !== null && <p className="text-sm text-danger">{error}</p>}
    </div>
  );
}

// --- Encryption vault (Phase 1, docs/33) --------------------------------------
// Enrolls the vault (encryption passphrase -> wrapped keys), unlocks per
// session, locks on demand. Honest status (docs/33): client-side encryption
// in progress — the server can still read your data until Phases 3-4 land.

function EncryptionPanel({ userId }: { userId: string | number }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const storageUserId = String(userId);
  const supported = backupSupported(); // same requirements: Web Crypto + IndexedDB
  const deps = useMemo(() => defaultSyncDeps(storageUserId), [storageUserId]);
  const metaQuery = useQuery({
    queryKey: ["vaultMeta", storageUserId],
    queryFn: () => loadVault(storageUserId),
    enabled: supported,
  });
  // Server-side envelope list (docs/33 Phase 4): the enrollment state, the
  // revocation signal and the device list all come from here.
  const syncQuery = useQuery({
    queryKey: ["vaultEnvelopes", storageUserId],
    queryFn: () => listVaultEnvelopes(deps),
    enabled: supported,
    retry: false,
  });
  const localBundle = metaQuery.data ?? null;
  const listing = syncQuery.data ?? null;
  const ownRow = listing?.items.find((row) => row.device_id === deps.deviceId) ?? null;
  const revoked = listing?.revoked_own_device ?? false;
  const accountHasVault = (listing?.items.length ?? 0) > 0;
  const enrolledHere = ownRow !== null || localBundle !== null;
  const [unlocked, setUnlocked] = useState(isUnlocked());
  const [mode, setMode] = useState<"idle" | "enroll" | "unlock" | "rotate">("idle");
  const [passphrase, setPassphrase] = useState("");
  const [confirmPassphrase, setConfirmPassphrase] = useState("");
  const [rotateNew, setRotateNew] = useState("");
  const [rotateConfirm, setRotateConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [pendingRevoke, setPendingRevoke] = useState<number | null>(null);

  async function refreshSync(): Promise<void> {
    await queryClient.invalidateQueries({ queryKey: ["vaultMeta", storageUserId] });
    await queryClient.invalidateQueries({ queryKey: ["vaultEnvelopes", storageUserId] });
  }

  function reset(): void {
    setMode("idle");
    setPassphrase("");
    setConfirmPassphrase("");
    setRotateNew("");
    setRotateConfirm("");
    setError(null);
    setPendingRevoke(null);
  }

  async function enroll(): Promise<void> {
    if (passphrase.length < 8) {
      setError("Encryption passphrase must be at least 8 characters.");
      return;
    }
    if (passphrase !== confirmPassphrase) {
      setError("Passphrases do not match.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await enrollVaultSynced(passphrase, deps);
      if (!result.ok) {
        setError(result.message);
        toast.error("Enrollment failed", result.message);
        await refreshSync();
        return;
      }
      await refreshSync();
      setUnlocked(true);
      reset();
      if (result.synced) {
        toast.success("Encryption enrolled — keys synced to your account");
      } else {
        toast.info(
          "Encryption enrolled in this browser only",
          "This account cannot store key blobs, so other browsers cannot enroll.",
        );
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setError(message);
      toast.error("Enrollment failed", message);
    } finally {
      setBusy(false);
    }
  }

  async function unlock(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      const result = await unlockVaultSynced(passphrase, deps);
      if (result.ok) {
        setUnlocked(true);
        reset();
        await refreshSync();
        if (result.source === "enrolled") {
          toast.success("Vault unlocked — this browser is now enrolled");
        } else {
          toast.success("Vault unlocked for this session");
        }
        if (result.syncError !== undefined) {
          toast.info("Key sync unavailable", "This browser will retry on the next unlock.");
        }
        return;
      }
      const message =
        result.reason === "device_revoked"
          ? "This browser's key was revoked from another device. Clear this site's data to enroll it again."
          : result.reason === "sync_unreachable"
            ? "Can't reach key sync — the vault stays locked."
            : result.reason === "no_envelope"
              ? "No vault found on this account yet — enroll to create one."
              : "Wrong passphrase — vault stays locked.";
      setError(message);
      toast.error("Unlock failed", message);
      if (result.reason === "no_envelope") setMode("enroll");
      await refreshSync();
    } finally {
      setBusy(false);
    }
  }

  function lock(): void {
    lockVault();
    setUnlocked(false);
    reset();
    toast.info("Vault locked");
  }

  async function changePassphrase(): Promise<void> {
    if (rotateNew.length < 8) {
      setError("Encryption passphrase must be at least 8 characters.");
      return;
    }
    if (rotateNew !== rotateConfirm) {
      setError("Passphrases do not match.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await rotatePassphrase(rotateNew, deps);
      if (!result.ok) {
        const message =
          result.reason === "vault_locked"
            ? "Unlock the vault first."
            : result.reason === "sync_unreachable"
              ? "Can't reach key sync — nothing was changed."
              : `Re-wrapped ${result.replaced} of ${result.active} devices before failing: ${result.message ?? "unknown error"}`;
        setError(message);
        toast.error("Passphrase change incomplete", message);
        await refreshSync();
        return;
      }
      reset();
      await refreshSync();
      toast.success(
        `Passphrase changed — ${result.replaced} of ${result.active} device keys re-wrapped`,
      );
    } finally {
      setBusy(false);
    }
  }

  async function revoke(id: number, isThisDevice: boolean): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await revokeEnvelope(id, deps);
      if (isThisDevice) {
        // Revoking yourself: drop the disk copy NOW instead of waiting for the
        // next unlock (docs/33 revoke runbook step 2) and lock the session.
        await deps.clearLocal(storageUserId);
        lockVault();
        setUnlocked(false);
      }
      setPendingRevoke(null);
      await refreshSync();
      toast.info("Device revoked", "That browser can no longer unlock the vault.");
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setError(message);
      toast.error("Revoke failed", message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-2 border-t border-veil/[0.06] pt-4">
      <h3 className="text-sm font-medium">Client-side encryption (in progress)</h3>
      {!supported ? (
        <p className="text-xs leading-relaxed text-muted">
          Web Crypto or IndexedDB is unavailable in this browser, so the encryption vault
          is disabled here.
        </p>
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-2">
            <Badge
              tone={
                unlocked
                  ? "success"
                  : revoked
                    ? "danger"
                    : enrolledHere || accountHasVault
                      ? "warn"
                      : "neutral"
              }
            >
              {unlocked
                ? "unlocked"
                : revoked
                  ? "revoked"
                  : enrolledHere || accountHasVault
                    ? "locked"
                    : "not enrolled"}
            </Badge>
            {unlocked ? (
              <Button type="button" variant="ghost" size="sm" onClick={lock}>
                Lock vault
              </Button>
            ) : !revoked && (enrolledHere || accountHasVault) ? (
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => {
                  setError(null);
                  setMode("unlock");
                }}
              >
                Unlock
              </Button>
            ) : null}
            {!unlocked && !revoked && !enrolledHere && !accountHasVault && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => {
                  setError(null);
                  setMode("enroll");
                }}
              >
                Enroll
              </Button>
            )}
            {unlocked && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => {
                  setError(null);
                  setMode("rotate");
                }}
              >
                Change passphrase
              </Button>
            )}
          </div>
          {!unlocked && revoked && (
            <p className="text-xs leading-relaxed text-danger">
              This browser's key was revoked from another device, so it can no longer unlock the
              vault. Clear this site's data to enroll it again.
            </p>
          )}
          <AnimatePresence>
            {mode !== "idle" && (
              <motion.form
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: "auto" }}
                exit={{ opacity: 0, height: 0 }}
                transition={{ duration: 0.25, ease: [0.22, 1, 0.36, 1] }}
                className="space-y-2 overflow-hidden rounded-lg border border-veil/[0.08] p-3"
                onSubmit={(e) => {
                  e.preventDefault();
                  void (mode === "enroll"
                    ? enroll()
                    : mode === "rotate"
                      ? changePassphrase()
                      : unlock());
                }}
              >
                <p className="text-xs text-muted">
                  {mode === "enroll"
                    ? "Separate from your login password — never sent to the server, never stored. Lose it and your future encrypted data is unrecoverable."
                    : mode === "rotate"
                      ? "Sets a new passphrase and re-wraps every device's key blob under it. Your data is not re-encrypted; devices still on the old passphrase stop working."
                      : "Enter your encryption passphrase to unlock for this session."}
                </p>
                <input aria-label="New passphrase"
                  type="password"
                  value={mode === "rotate" ? rotateNew : passphrase}
                  onChange={(e) =>
                    mode === "rotate"
                      ? setRotateNew(e.target.value)
                      : setPassphrase(e.target.value)
                  }
                  placeholder={mode === "rotate" ? "New encryption passphrase" : "Encryption passphrase"}
                  minLength={8}
                  className={`${FIELD} tracking-widest`}
                />
                {mode !== "unlock" && (
                  <input aria-label="Confirm new passphrase"
                    type="password"
                    value={mode === "rotate" ? rotateConfirm : confirmPassphrase}
                    onChange={(e) =>
                      mode === "rotate"
                        ? setRotateConfirm(e.target.value)
                        : setConfirmPassphrase(e.target.value)
                    }
                    placeholder={mode === "rotate" ? "Confirm new passphrase" : "Confirm passphrase"}
                    minLength={8}
                    className={`${FIELD} tracking-widest`}
                  />
                )}
                <div className="flex items-center gap-2 pt-1">
                  <Button
                    type="submit"
                    variant="primary"
                    size="sm"
                    loading={busy}
                    disabled={
                      (mode === "rotate" ? rotateNew : passphrase).length < 8 ||
                      (mode === "enroll" && passphrase !== confirmPassphrase) ||
                      (mode === "rotate" && rotateNew !== rotateConfirm)
                    }
                  >
                    {mode === "enroll"
                      ? "Enroll & unlock"
                      : mode === "rotate"
                        ? "Change passphrase"
                        : "Unlock"}
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    disabled={busy}
                    onClick={reset}
                  >
                    Cancel
                  </Button>
                </div>
              </motion.form>
            )}
          </AnimatePresence>
          {listing !== null && listing.items.length > 0 && (
            <div className="space-y-1.5 border-t border-veil/[0.06] pt-3">
              <p className="text-[11px] uppercase tracking-widest text-muted">Devices</p>
              <ul className="space-y-1">
                {listing.items.map((row) => {
                  const isThis = row.device_id === deps.deviceId;
                  return (
                    <li
                      key={row.id}
                      className="flex items-center justify-between gap-2 text-xs"
                    >
                      <span className="min-w-0 truncate">
                        {row.label ?? "Unnamed device"}
                        {isThis && <span className="text-muted"> · this device</span>}
                        {row.created_at != null && (
                          <span className="text-muted">
                            {" "}
                            · added {new Date(row.created_at).toLocaleDateString()}
                          </span>
                        )}
                      </span>
                      {pendingRevoke === row.id ? (
                        <span className="flex shrink-0 items-center gap-1">
                          <span className="text-danger">
                            {isThis ? "Locks out this browser. " : ""}Revoke?
                          </span>
                          <Button
                            type="button"
                            variant="primary"
                            size="sm"
                            disabled={busy}
                            onClick={() => void revoke(row.id, isThis)}
                          >
                            Yes
                          </Button>
                          <Button
                            type="button"
                            variant="ghost"
                            size="sm"
                            disabled={busy}
                            onClick={() => setPendingRevoke(null)}
                          >
                            No
                          </Button>
                        </span>
                      ) : (
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          disabled={busy}
                          onClick={() => setPendingRevoke(row.id)}
                        >
                          Revoke
                        </Button>
                      )}
                    </li>
                  );
                })}
              </ul>
              <p className="text-[11px] leading-relaxed text-muted">
                Revoking a device deletes its wrapped key from the server and from that browser —
                it can no longer unlock. The passphrase is what really protects the vault: change
                it above if you think someone knows it.
              </p>
            </div>
          )}
          {syncQuery.isError && (
            <p className="text-xs leading-relaxed text-muted">
              Key sync is unreachable right now — unlock needs it, so the vault stays locked.
            </p>
          )}
          <p className="text-xs leading-relaxed text-muted">
            Client-side encryption in progress — server can still read your data. Your
            passphrase and keys stay in this browser, but true end-to-end encryption (the
            server unable to read anything) is not live yet.
          </p>
        </>
      )}
      {error !== null && <p className="text-sm text-danger">{error}</p>}
    </div>
  );
}

// --- E2EE migration: backfill + enforcement (Phase 2, docs/33) ----------------
// Encrypts legacy rows client-side (the server can never do it) and flips the
// per-user enforcement flag so new writes must carry ciphertext.

/** The Backfill panel's single visible reason (docs/47 §4) — one per panel. */
const BACKFILL_DEMO_REASON_ID = "backfill-demo-reason";
const BACKFILL_DEMO_REASON =
  "The demo account is read-only — encrypting existing data would write every row.";

function BackfillPanel() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const meQuery = useQuery({ queryKey: ["me"], queryFn: me });
  const user = meQuery.data;
  // M136: the shared subscription — unlocking inline in the notice below
  // flips this panel without the "Recheck" button it used to need.
  const unlocked = useVaultUnlocked();
  // M185 F-2 residual (docs/47 §4): the run button's writes are per-row PUTs
  // (transactions + accounts), the toggle is PATCH /users/me — both refused
  // for the demo tenant, so each wears the padlock and its REAL 403 becomes
  // one refusal element + one toast.info, never toast.error.
  const demo = useDemoRefusal();
  const backfillLocked = useDemoWriteLock("PUT", "/transactions/{id}");
  const requiredLocked = useDemoWriteLock("PATCH", "/users/me");
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function run(): Promise<void> {
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      const counts = await backfillE2EE({
        listTransactions: (limit, offset) => listTransactions({}, { limit, offset }),
        updateTransaction: (id, patch) => updateTransaction(id, patch),
        listAccounts: () => listAccounts(200, 0),
        updateAccount: (id, patch) => updateAccount(id, patch),
        onProgress: (done, total) => setProgress(`${done} / ${total} rows`),
      });
      setProgress(null);
      setResult(
        `Encrypted ${counts.transactions} transactions and ${counts.accounts} accounts (${counts.skipped} already done).`,
      );
      toast.success("Backfill complete");
    } catch (err) {
      // A refused demo write is not a failed backfill (docs/47 §4).
      if (demo.onError("Encrypt my existing data", err)) return;
      const message = err instanceof Error ? err.message : String(err);
      setError(message);
      toast.error("Backfill failed", message);
    } finally {
      setRunning(false);
    }
  }

  const setRequired = useMutation({
    mutationFn: (required: boolean) => updateMe({ encryption_required: required }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["me"] });
      toast.success("Enforcement updated");
    },
    onError: (err) => {
      // A refused demo write is not a failed enforcement flip (docs/47 §4).
      if (demo.onError("Require encryption on new writes", err)) return;
      const message = err instanceof Error ? err.message : String(err);
      setError(message);
      toast.error("Update failed", message);
    },
  });

  return (
    <div className="space-y-2 border-t border-veil/[0.06] pt-4">
      <h3 className="text-sm font-medium">Encrypt existing data</h3>
      {demo.refusal !== null && (
        <DemoRefusal id={demo.refusalId} action={demo.refusal} />
      )}
      {!unlocked ? (
        // M136 (docs/33 rule 7g): the fourth invented lock phrase, collapsed
        // onto the shared component — same string as the shell chip, the
        // search popover and the backup panel.
        <VaultLocked variant="blocked" capability="backfill" />
      ) : (
        <>
          <p className="text-xs leading-relaxed text-muted">
            Fetches each legacy transaction and account, encrypts it locally, and writes
            the ciphertext back. Safe to re-run; already-encrypted rows are skipped.
          </p>
          <Button
            type="button"
            variant="outline"
            size="sm"
            loading={running}
            disabled={running}
            aria-describedby={backfillLocked ? BACKFILL_DEMO_REASON_ID : undefined}
            onClick={() => void run()}
          >
            {backfillLocked && <DemoLock />}
            Encrypt my existing data
          </Button>
          {progress !== null && (
            <p className="text-xs text-muted tabular">Working… {progress}</p>
          )}
          {result !== null && <p className="text-xs text-muted">{result}</p>}
          <div className="pt-1">
            <Toggle
              checked={user?.encryption_required ?? false}
              disabled={setRequired.isPending || meQuery.isPending}
              ariaLabel="Require encryption on new writes"
              ariaDescribedBy={requiredLocked ? BACKFILL_DEMO_REASON_ID : undefined}
              onChange={(checked) => setRequired.mutate(checked)}
              label={
                <span className="inline-flex items-center gap-1 text-sm">
                  Require encryption on new writes
                  {requiredLocked && <DemoLock />}
                </span>
              }
            />
            <p className="mt-1 text-xs leading-relaxed text-muted">
              When on, the server rejects any transaction or account write without
              ciphertext (422). Backfill first, then flip this.
            </p>
            {/* docs/47 §4: the ONE visible reason this panel's locks bind to. */}
            {requiredLocked && (
              <DemoReason id={BACKFILL_DEMO_REASON_ID}>{BACKFILL_DEMO_REASON}</DemoReason>
            )}
          </div>
        </>
      )}
      {error !== null && <p className="text-sm text-danger">{error}</p>}
    </div>
  );
}

// --- Category tree editor ------------------------------------------------------

/** The Categories card's single visible reason (docs/47 §4) — one per panel. */
const CATEGORY_DEMO_REASON_ID = "categories-demo-reason";
const CATEGORY_DEMO_REASON =
  "The demo account is read-only — categories cannot be created, renamed or deleted here.";

async function fetchAllCategories(): Promise<Category[]> {
  const limit = 200;
  let offset = 0;
  const items: Category[] = [];
  for (;;) {
    const page = await listCategories(limit, offset);
    items.push(...page.items);
    if (page.items.length === 0 || items.length >= page.total) return items;
    offset += limit;
  }
}

function buildTree(categories: Category[]): Map<number | null, Category[]> {
  const tree = new Map<number | null, Category[]>();
  for (const category of categories) {
    const siblings = tree.get(category.parent_id) ?? [];
    siblings.push(category);
    tree.set(category.parent_id, siblings);
  }
  for (const siblings of tree.values()) {
    siblings.sort((a, b) => a.name.localeCompare(b.name));
  }
  return tree;
}

export function CategoryManager() {
  const queryClient = useQueryClient();
  const toast = useToast();
  // M185 F-2 residual (docs/47 §4): a refused demo write is ONE toast.info +
  // ONE local role="status" element — and the red action-error line below
  // stays clear. Declared before the mutations that route through it.
  const demo = useDemoRefusal();
  // The session SCOPE (not `useAuth`) keeps this editor renderable in
  // isolation: no provider ⇒ no session ⇒ no lock (auth/authUser.ts).
  const user = useOptionalUser();
  const createLocked = demoLocks(user, "POST", "/categories");
  const [actionError, setActionError] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [newParent, setNewParent] = useState("");

  const categories = useQuery({
    queryKey: ["categories", "all"],
    queryFn: fetchAllCategories,
  });

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ["categories"] });

  const create = useMutation({
    mutationFn: () =>
      createCategory({
        name: newName.trim(),
        parent_id: newParent === "" ? null : Number(newParent),
      }),
    onSuccess: () => {
      invalidate();
      setNewName("");
      setNewParent("");
      setActionError(null);
      toast.success("Category added");
    },
    onError: (err) => {
      // A refused demo write is not a red action error (docs/47 §4).
      if (demo.onError("Create category", err)) return;
      setActionError(err.message);
      toast.error("Could not add category", err.message);
    },
  });

  const remove = useMutation({
    mutationFn: (id: number) => deleteCategory(id),
    onSuccess: () => {
      invalidate();
      setActionError(null);
      toast.success("Category deleted");
    },
    onError: (err) => {
      // A refused demo write is not a red action error (docs/47 §4) — the
      // guard runs BEFORE the in-use branch below.
      if (demo.onError("Delete category", err)) return;
      // 409 {code:"category_in_use"} means reassign first — keyed off the
      // envelope `code`, never `detail` (docs/08, hasErrorCode): `err.message`
      // is the human `detail`, so matching the code token here was dead code.
      const message = hasErrorCode(err, "category_in_use")
        ? "Category is in use by transactions, subcategories, rules or learned payees — reassign first."
        : err.message;
      setActionError(message);
      toast.error("Delete blocked", message);
    },
  });

  if (categories.isPending) {
    return (
      <Card className="max-w-2xl p-4">
        <CardTitle>Categories</CardTitle>
        <SkeletonRows rows={5} />
      </Card>
    );
  }
  if (categories.isError) {
    return (
      <Card className="max-w-2xl p-4">
        <CardTitle>Categories</CardTitle>
        <QueryErrorPanel query={categories} message={categories.error.message} />
      </Card>
    );
  }

  const all = categories.data;
  const tree = buildTree(all);

  return (
    <Card hover className="max-w-2xl p-4">
      <CardTitle right={<Tags className="size-4 text-muted" aria-hidden />}>Categories</CardTitle>
      {demo.refusal !== null && (
        <DemoRefusal id={demo.refusalId} action={demo.refusal} />
      )}

      <form
        className="mb-4 flex flex-wrap items-end gap-2 rounded-lg border border-veil/[0.06] bg-surface p-3 text-sm"
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <label className="min-w-40 flex-1">
          <span className="mb-1 block text-xs text-muted">New category name</span>
          <input
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            className={FIELD}
          />
        </label>
        <label className="min-w-40 flex-1">
          <span className="mb-1 block text-xs text-muted">Parent</span>
          <select value={newParent} onChange={(e) => setNewParent(e.target.value)} className={FIELD}>
            <option value="">— top level —</option>
            {all.map((category) => (
              <option key={category.id} value={category.id}>
                {category.name}
              </option>
            ))}
          </select>
        </label>
        <Button
          type="submit"
          variant="primary"
          loading={create.isPending}
          disabled={newName.trim() === ""}
          aria-describedby={createLocked ? CATEGORY_DEMO_REASON_ID : undefined}
        >
          {createLocked && <DemoLock />}
          Add
        </Button>
      </form>

      {/* docs/47 §4: the ONE visible reason this card's padlocks (Add above,
          Rename/Delete on every row below) bind to. */}
      {createLocked && (
        <DemoReason id={CATEGORY_DEMO_REASON_ID}>{CATEGORY_DEMO_REASON}</DemoReason>
      )}

      {actionError !== null && (
        <p className="mb-2 text-sm text-danger">{actionError}</p>
      )}

      <ul className="space-y-0.5">
        {(tree.get(null) ?? []).map((category) => (
          <CategoryNode
            key={category.id}
            category={category}
            tree={tree}
            depth={0}
            onRemove={(id) => remove.mutate(id)}
          />
        ))}
      </ul>
    </Card>
  );
}

function CategoryNode({
  category,
  tree,
  depth,
  onRemove,
}: {
  category: Category;
  tree: Map<number | null, Category[]>;
  depth: number;
  onRemove: (id: number) => void;
}) {
  const toast = useToast();
  // M185 F-2 residual (docs/47 §4): the rename write routes its REAL 403
  // through this row's own refusal element, never toast.error.
  const demo = useDemoRefusal();
  // Session scope, same as the card above — this row must stay renderable in
  // isolation too (no provider ⇒ no session ⇒ no lock).
  const rowUser = useOptionalUser();
  const renameLocked = demoLocks(rowUser, "PATCH", `/categories/${category.id}`);
  const deleteLocked = demoLocks(rowUser, "DELETE", `/categories/${category.id}`);
  const children = tree.get(category.id) ?? [];
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(category.name);
  const [parentId, setParentId] = useState(String(category.parent_id ?? ""));
  const rename = useMutation({
    mutationFn: () =>
      updateCategory(category.id, {
        name: name.trim(),
        parent_id: parentId === "" ? null : Number(parentId),
      }),
    onSuccess: () => {
      setEditing(false);
      toast.success("Category updated", name.trim());
    },
    onError: (err) => {
      if (demo.onError("Rename category", err)) return;
      toast.error("Rename failed", err.message);
    },
  });
  const invalidMove =
    parentId !== "" &&
    (Number(parentId) === category.id ||
      children.some((child) => child.id === Number(parentId)));

  return (
    <li>
      {demo.refusal !== null && (
        <DemoRefusal id={demo.refusalId} action={demo.refusal} />
      )}
      <div
        className="group flex items-center gap-2 rounded-lg px-2 py-1.5 text-sm transition-colors hover:bg-veil/[0.04]"
        style={{ paddingLeft: `${depth * 20 + 8}px` }}
      >
        {editing ? (
          <>
            <input aria-label="New category name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="field-ring w-48 rounded-lg border border-control bg-surface px-2 py-1 text-sm outline-none transition-colors"
            />
            <select aria-label="Parent category"
              value={parentId}
              onChange={(e) => setParentId(e.target.value)}
              className="field-ring rounded-lg border border-control bg-surface px-2 py-1 text-sm outline-none transition-colors"
              title="Move under a new parent"
            >
              <option value="">— top level —</option>
              <SiblingOptions categoryId={category.id} tree={tree} />
            </select>
            <Button
              variant="accent-soft"
              size="sm"
              loading={rename.isPending}
              disabled={name.trim() === "" || invalidMove}
              title={invalidMove ? "Cannot move under itself or a child" : undefined}
              aria-describedby={renameLocked ? CATEGORY_DEMO_REASON_ID : undefined}
              onClick={() => rename.mutate()}
            >
              {renameLocked && <DemoLock />}
              Save
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setEditing(false);
                setName(category.name);
                setParentId(String(category.parent_id ?? ""));
              }}
            >
              Cancel
            </Button>
          </>
        ) : (
          <>
            <span>{category.name}</span>
            {category.is_system && (
              <Badge tone="neutral" dot={false}>
                system
              </Badge>
            )}
            <span className="ml-auto hidden shrink-0 gap-2 group-hover:flex">
              <Button variant="ghost" size="sm" onClick={() => setEditing(true)}>
                Rename / move
              </Button>
              {!category.is_system && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="text-danger hover:text-danger"
                  aria-describedby={deleteLocked ? CATEGORY_DEMO_REASON_ID : undefined}
                  onClick={() => onRemove(category.id)}
                  title={
                    category.is_system
                      ? "System rows cannot be deleted"
                      : "Delete (fails while in use)"
                  }
                >
                  {deleteLocked && <DemoLock />}
                  Delete
                </Button>
              )}
            </span>
          </>
        )}
      </div>
      {children.map((child) => (
        <CategoryNode
          key={child.id}
          category={child}
          tree={tree}
          depth={depth + 1}
          onRemove={onRemove}
        />
      ))}
    </li>
  );
}

/** Flattened options for the move select, excluding the node + its descendants. */
function SiblingOptions({
  categoryId,
  tree,
}: {
  categoryId: number;
  tree: Map<number | null, Category[]>;
}) {
  const forbidden = new Set<number>([categoryId]);
  const collectDescendants = (id: number) => {
    for (const child of tree.get(id) ?? []) {
      forbidden.add(child.id);
      collectDescendants(child.id);
    }
  };
  collectDescendants(categoryId);

  const options: { id: number; label: string }[] = [];
  const walk = (parent: number | null, depth: number) => {
    for (const node of tree.get(parent) ?? []) {
      if (!forbidden.has(node.id)) {
        options.push({ id: node.id, label: `${"\u00A0".repeat(depth * 2)}${node.name}` });
      }
      walk(node.id, depth + 1);
    }
  };
  walk(null, 0);

  return (
    <>
      {options.map((option) => (
        <option key={option.id} value={option.id}>
          {option.label}
        </option>
      ))}
    </>
  );
}

function ImportProfilesPointer() {
  return (
    <Card hover className="max-w-xl p-4">
      <div className="flex items-center justify-between gap-4">
        <div>
          <CardTitle>Import profiles</CardTitle>
          <p className="-mt-1 text-xs text-muted">
            CSV column mappings are edited per institution on the Imports page.
          </p>
        </div>
        <Link
          to="/imports"
          className="shrink-0 rounded-lg px-2 py-1 text-sm font-medium text-accent outline-none transition-colors hover:bg-accent/10 hover:underline focus-visible:ring-2 focus-visible:ring-accent"
        >
          Open Imports →
        </Link>
      </div>
    </Card>
  );
}
