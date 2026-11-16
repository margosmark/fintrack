import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { me } from "../../api/client";
import { listTags, type Tag } from "../../api/tags";
import { updateMe } from "../../api/users";
import { Card, CardTitle } from "../ui/Card";
import { Button } from "../ui/Button";
import { SkeletonRows } from "../ui/Skeleton";
import { useToast } from "../ui/Toast";

/**
 * M174 (docs/09 §Tags that mark recurring) — "Tags that mark recurring".
 *
 * Mark's ask: *"for recurring stuff can we make that based on tags since there
 * can be multiple"*. The ruling that shapes this control is the PM's: **NO magic
 * tag names**. A heuristic on `/sub|recurring|monthly|bill/i` would be a second,
 * invisible normalizer — it would silently decide that a tag called
 * "Subscriptions (old)" counts and one called "abos" does not, and the user
 * could only find out by seeing the wrong calendar. Instead the user NAMES the
 * tags here, from their own list, and the confirmed-subscription query reads
 * exactly that set (`users.recurring_intent_tag_ids`).
 *
 * **Empty is OFF, and the card says so in words.** With nothing selected the
 * tag source does not run at all: no scan, no group, no calendar dot. That is
 * stated on the card rather than left to the reader, because "no dots" is
 * indistinguishable from "the feature is broken" otherwise.
 *
 * Whole-list write: one `PATCH /users/me {recurring_intent_tag_ids: [...]}`,
 * the same shape M108-P2 gave saved views — no per-tag CRUD, no partial state.
 * The demo tenant is write-guarded server-side (docs/20), so this answers
 * `403 demo_account_restricted` there and the toast explains it.
 */
export function RecurringIntentSection() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const { data: user, isLoading } = useQuery({ queryKey: ["me"], queryFn: me });
  const { data: tags } = useQuery({ queryKey: ["tags"], queryFn: () => listTags(200) });

  const [selected, setSelected] = useState<number[] | null>(null);

  // The server is the source of truth; the local list is the user's pending
  // edit and is only seeded once (a later refetch must not clobber it).
  useEffect(() => {
    if (selected === null && user !== undefined) setSelected(user.recurring_intent_tag_ids ?? []);
  }, [selected, user]);

  const save = useMutation({
    mutationFn: (ids: number[]) => updateMe({ recurring_intent_tag_ids: ids }),
    onSuccess: (updated) => {
      queryClient.setQueryData(["me"], updated);
      setSelected(updated.recurring_intent_tag_ids ?? []);
      toast.success("Saved", "Tags that mark recurring updated.");
    },
    onError: () => {
      toast.error("Not saved", "The server refused the change — nothing was written.");
    },
  });

  if (isLoading || selected === null) return <SkeletonRows rows={3} />;

  const items: Tag[] = tags?.items ?? [];
  const stored = user?.recurring_intent_tag_ids ?? [];
  const dirty =
    selected.length !== stored.length || selected.some((id) => !stored.includes(id));

  function toggle(tagId: number): void {
    setSelected((current) =>
      (current ?? []).includes(tagId)
        ? (current ?? []).filter((id) => id !== tagId)
        : [...(current ?? []), tagId],
    );
  }

  return (
    <Card className="p-4">
      <CardTitle>Tags that mark recurring</CardTitle>
      <p className="text-sm text-muted mb-3">
        A recurring group whose rows all carry one of these tags counts as a confirmed
        subscription on the calendar — the same as a bill or a candidate you accepted.
        Nothing is inferred from a tag’s name. Leave this empty to switch the feature off:
        no tags are scanned and no subscriptions are added this way.
      </p>

      {items.length === 0 ? (
        <p className="text-sm text-muted">
          You have no tags yet — create the ones you want to use (for example “subscription”)
          on the Transactions page, then pick them here.
        </p>
      ) : (
        <div className="flex flex-wrap gap-2" data-testid="recurring-intent-tags">
          {items.map((tag) => {
            const on = selected.includes(tag.id);
            return (
              <button
                key={tag.id}
                type="button"
                aria-pressed={on}
                onClick={() => toggle(tag.id)}
                className={`inline-flex min-h-11 items-center rounded-full border px-3 text-xs font-medium outline-none transition-colors focus-visible:ring-2 focus-visible:ring-accent ${
                  on
                    ? "border-accent bg-accent/15 text-accent"
                    : "border-control bg-veil/[0.04] text-muted hover:border-accent/70 hover:text-accent"
                }`}
              >
                {tag.name}
              </button>
            );
          })}
        </div>
      )}

      <div className="mt-3 flex items-center gap-2">
        <Button
          size="sm"
          variant="primary"
          disabled={!dirty || save.isPending}
          onClick={() => save.mutate(selected)}
        >
          Save tags
        </Button>
        <span className="text-xs text-muted">
          {selected.length === 0
            ? "Off — no tag marks a subscription."
            : `${selected.length} selected`}
        </span>
      </div>
    </Card>
  );
}
