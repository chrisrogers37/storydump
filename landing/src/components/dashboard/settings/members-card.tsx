"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  removeMemberRefusalCopy,
  submitRemoveMember,
} from "@/lib/command-client";
import { memberOrigin } from "@/lib/members";
import type { WorkspaceMember } from "@/lib/types";

const ROLE_CLASS: Record<string, string> = {
  owner: "bg-purple-100 text-purple-900",
  admin: "bg-blue-100 text-blue-900",
  member: "bg-muted text-muted-foreground",
};

/**
 * Who is in the workspace, and the revoke for every join edge (`06`: "an
 * admin removes membership explicitly"). A member who joined from a bound
 * Telegram group (`07` §14) is labelled as such: that grant outlives the
 * group, so the person who can undo it must be able to see it.
 */
export function MembersCard({
  workspaceId,
  members,
  currentUserId,
  canRemove,
}: {
  workspaceId: string;
  members: WorkspaceMember[] | null;
  currentUserId: string;
  canRemove: boolean;
}) {
  const router = useRouter();
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function remove(userId: string) {
    setError(null);
    setPending(userId);
    try {
      const result = await submitRemoveMember(workspaceId, userId);
      if (!result.ok) {
        setError(removeMemberRefusalCopy(result.error, result.status));
        return;
      }
      router.refresh();
    } finally {
      setPending(null);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Members</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {error && (
          <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">
            {error}
          </div>
        )}
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
                        onClick={() => remove(m.user_id)}
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
