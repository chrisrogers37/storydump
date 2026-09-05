import type { WorkspaceMember } from "./types";

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
