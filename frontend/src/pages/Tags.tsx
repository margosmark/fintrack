import { useState } from "react";
import { motion } from "framer-motion";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Tag, Tags as TagsIcon } from "lucide-react";
import { listTags, createTag, updateTag, deleteTag } from "../api/tags";
import { hasErrorCode } from "../api/client";
import type { Tag as TagType } from "../api/tags";
import { Button } from "../components/ui/Button";
import { Card, CardTitle } from "../components/ui/Card";
import { EmptyState, ErrorPanel } from "../components/ui/EmptyState";
import { SkeletonRows } from "../components/ui/Skeleton";
import { staggerContainer, fadeUp } from "../components/ui/motion";
import { useToast } from "../components/ui/Toast";

const FIELD =
  "field-ring w-full rounded-lg border border-control bg-surface px-2 py-1.5 text-sm outline-none transition-colors placeholder:text-muted hover:border-accent/70";

export function Tags() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [newName, setNewName] = useState("");

  const tags = useQuery({
    queryKey: ["tags"],
    queryFn: () => listTags(),
  });

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ["tags"] });

  const create = useMutation({
    mutationFn: () => createTag({ name: newName.trim() }),
    onSuccess: () => {
      invalidate();
      setNewName("");
      toast.success("Tag created");
    },
    onError: (err) => {
      // Branch on the envelope `code`, never on `detail` (docs/08, hasErrorCode).
      const message = hasErrorCode(err, "duplicate_tag")
        ? "A tag with that name already exists."
        : err.message;
      toast.error("Could not create tag", message);
    },
  });

  if (tags.isPending) {
    return (
      <Card className="max-w-2xl p-4">
        <CardTitle>Tags</CardTitle>
        <SkeletonRows rows={5} />
      </Card>
    );
  }

  if (tags.isError) {
    return (
      <Card className="max-w-2xl p-4">
        <CardTitle>Tags</CardTitle>
        <ErrorPanel message={tags.error.message} />
      </Card>
    );
  }

  const all = tags.data.items;

  return (
    <Card hover className="max-w-2xl p-4">
      <CardTitle right={<TagsIcon className="size-4 text-muted" aria-hidden />}>
        Tags
      </CardTitle>

      <form
        className="mb-4 flex items-end gap-2 rounded-lg border border-veil/[0.06] bg-surface p-3 text-sm"
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <label className="min-w-40 flex-1">
          <span className="mb-1 block text-xs text-muted">New tag name</span>
          <input
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="e.g. recurring, tax-deductible"
            className={FIELD}
          />
        </label>
        <Button
          type="submit"
          variant="primary"
          loading={create.isPending}
          disabled={newName.trim() === ""}
        >
          Add
        </Button>
      </form>

      {all.length === 0 ? (
        <EmptyState
          icon={<Tag className="size-5" />}
          title="No tags yet"
          hint="Create your first tag to label transactions."
          preview={{
            none: "the demo tenant owns zero tags, so no capture of a populated tags list exists",
          }}
          action={{
            label: "Create a tag",
            onClick: () => {
              if (newName.trim() !== "") create.mutate();
            },
          }}
        />
      ) : (
        <motion.ul
          variants={staggerContainer()}
          initial="hidden"
          animate="show"
          className="flex flex-wrap gap-2"
        >
          {all.map((tag) => (
            <TagChip
              key={tag.id}
              tag={tag}
              onChanged={invalidate}
            />
          ))}
        </motion.ul>
      )}
    </Card>
  );
}

function TagChip({
  tag,
  onChanged,
}: {
  tag: TagType;
  onChanged: () => void;
}) {
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(tag.name);

  const rename = useMutation({
    mutationFn: () => updateTag(tag.id, { name: name.trim() }),
    onSuccess: () => {
      setEditing(false);
      onChanged();
      toast.success("Tag renamed", name.trim());
    },
    onError: (err) => {
      const message = hasErrorCode(err, "duplicate_tag")
        ? "A tag with that name already exists."
        : err.message;
      toast.error("Rename failed", message);
      setName(tag.name);
      setEditing(false);
    },
  });

  const remove = useMutation({
    mutationFn: () => deleteTag(tag.id),
    onSuccess: () => {
      onChanged();
      toast.success("Tag deleted");
    },
    onError: (err) => {
      const message = hasErrorCode(err, "tag_in_use")
        ? "Tag is in use by transactions or rules — remove it first."
        : err.message;
      toast.error("Delete blocked", message);
    },
  });

  return (
    <motion.li variants={fadeUp}>
      <div className="group flex items-center gap-1.5 rounded-full border border-accent/30 bg-accent/10 px-2.5 py-1 text-sm font-medium text-accent transition-colors hover:bg-accent/20">
        {editing ? (
          <>
            <input aria-label="Tag name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && name.trim() !== "") rename.mutate();
                if (e.key === "Escape") {
                  setEditing(false);
                  setName(tag.name);
                }
              }}
              className="field-ring w-32 rounded-md border border-control bg-surface px-1.5 py-0.5 text-xs outline-none"
              autoFocus
            />
            <Button
              variant="accent-soft"
              size="sm"
              loading={rename.isPending}
              disabled={name.trim() === ""}
              onClick={() => rename.mutate()}
            >
              Save
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setEditing(false);
                setName(tag.name);
              }}
            >
              Cancel
            </Button>
          </>
        ) : (
          <>
            <span className="cursor-pointer" onClick={() => setEditing(true)}>
              {tag.name}
            </span>
            <span className="ml-0.5 hidden shrink-0 gap-1 group-hover:inline-flex">
              <Button
                variant="ghost"
                size="sm"
                className="h-5 px-1"
                onClick={() => setEditing(true)}
              >
                Rename
              </Button>
              <Button
                variant="ghost"
                size="sm"
                className="h-5 px-1 text-danger hover:text-danger"
                onClick={() => remove.mutate()}
              >
                Delete
              </Button>
            </span>
          </>
        )}
      </div>
    </motion.li>
  );
}
