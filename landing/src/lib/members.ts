import type { ChannelBinding, WorkspaceMember } from "./types";

/**
 * How a person got into the workspace, from the two facts the members row
 * carries. The owner created the workspace (provisioning adds nobody's id);
 * a member with no adder joined from a bound Telegram group (`07` §14); an
 * adder means an invitation. Kept out of the card so it is testable.
 */
export function memberOrigin(
  m: Pick<WorkspaceMember, "role" | "added_by_user_id">,
): string {
  if (m.role === "owner") return "Created this workspace";
  if (m.added_by_user_id === null) return "Joined from a Telegram group";
  return "Invited";
}

/**
 * Whether this workspace's cards go to a Telegram group right now — an
 * ACTIVE `telegram_group` binding. A direct chat is one person's, so it is not
 * a group anyone else sits in. `null` (the bindings read failed) is false:
 * the removal reminder below is a nudge, and an unread list cannot claim one.
 */
export function hasActiveTelegramGroup(
  bindings: Pick<ChannelBinding, "channel" | "state">[] | null,
): boolean {
  return (bindings ?? []).some(
    (b) => b.channel === "telegram_group" && b.state === "active",
  );
}

/**
 * What the Members card says after a removal when a group is bound. Removing
 * a membership does not take anyone out of the Telegram group, and the bot
 * does not kick, so the person keeps seeing every card posted there until
 * someone removes them in Telegram.
 */
export function stillInTelegramGroupCopy(email: string | null): string {
  return (
    `${email ?? "This person"} is still in your Telegram group, so they'll ` +
    "keep seeing new Stories there. Remove them from the group in Telegram too."
  );
}
