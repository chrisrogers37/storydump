"use client";

import { useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Notice } from "@/components/ui/notice";
import {
  removeMemberRefusalCopy,
  submitRemoveMember,
} from "@/lib/command-client";
import { memberOrigin, stillInTelegramGroupCopy } from "@/lib/members";
import type { WorkspaceMember } from "@/lib/types";
import { TONE_CLASS } from "@/components/dashboard/tone";

const ROLE_CLASS: Record<string, string> = {
  owner: "bg-ink text-white",
  admin: "border-ink/10 bg-paper text-ink",
  member: TONE_CLASS.inert,
};

/**
 * Who is in the workspace, and the revoke for every join edge (`06`: "an
 * admin removes membership explicitly"). A member who joined from a bound
 * Telegram group (`07` §14) is labelled as such: that grant outlives the
 * group, so the person who can undo it must be able to see it.
 *
 * Removing someone revokes their MEMBERSHIP only: they stay in any bound
 * Telegram group, and the bot does not kick. So when a group is bound, a
 * successful removal leaves a reminder to remove them in Telegram too.
 */
export function MembersCard({
  workspaceId,
  members,
  currentUserId,
  canRemove,
  telegramGroupLinked,
  children,
}: {
  workspaceId: string;
  members: WorkspaceMember[] | null;
  currentUserId: string;
  canRemove: boolean;
  /** An active Telegram group is bound here (`hasActiveTelegramGroup`). */
  telegramGroupLinked: boolean;
  /** What the page adds for an admin above the list: inviting (#1563). */
  children?: ReactNode;
}) {
  const router = useRouter();
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Client state, so it outlives `router.refresh()` dropping the removed row.
  const [stillInGroup, setStillInGroup] = useState<string | null>(null);

  async function remove(member: WorkspaceMember) {
    setError(null);
    setStillInGroup(null);
    setPending(member.user_id);
    try {
      const result = await submitRemoveMember(workspaceId, member.user_id);
      if (!result.ok) {
        setError(removeMemberRefusalCopy(result.error, result.status));
        return;
      }
      if (telegramGroupLinked) {
        setStillInGroup(stillInTelegramGroupCopy(member.primary_email));
      }
      router.refresh();
    } finally {
      setPending(null);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Members</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {error && (
          <Notice tone="error">{error}</Notice>
        )}
        {stillInGroup && <Notice>{stillInGroup}</Notice>}
        {children}
        {members === null ? (
          <p className="text-sm text-muted-foreground">
            Members could not be loaded just now. Reload to try again.
          </p>
        ) : (
          <ul className="divide-y">
            {members.map((m) => {
              const isSelf = m.user_id === currentUserId;
              return (
                <li
                  key={m.user_id}
                  className="flex items-center justify-between gap-3 py-2"
                >
                  <div className="min-w-0">
                    <p className="truncate text-sm">
                      {m.primary_email ?? "No email on file"}
                      {isSelf ? " (you)" : ""}
                    </p>
                    <p className="text-xs text-muted-foreground">
                      {memberOrigin(m)}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <Badge
                      variant="secondary"
                      className={ROLE_CLASS[m.role] ?? "bg-muted"}
                    >
                      {m.role}
                    </Badge>
                    {canRemove && m.role !== "owner" && !isSelf && (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => remove(m)}
                        disabled={pending !== null}
                      >
                        {pending === m.user_id ? "Removing..." : "Remove"}
                      </Button>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
        <p className="text-xs text-muted-foreground">
          People who speak in a bound Telegram group join as members
          automatically once their Telegram is linked; leaving the group removes
          nobody. Removing someone here revokes their access to this workspace.
        </p>
      </CardContent>
    </Card>
  );
}
