/**
 * An invitation's join link is shown once and kept nowhere (#1564).
 *
 * The link IS the invitation: whoever holds it can accept it, by signing in
 * as the invited address. The port stores only a hash of its token, so the
 * answer to `invite_member` is the one place it exists in full. These are the
 * modules that hold that answer as a value, browser and server, in the order
 * it passes through them. None may write it to browser storage, hand it to
 * analytics, or log it: each is a copy that outlives the screen, and the
 * server's logs and the analytics provider are read by people who are not
 * the inviter.
 *
 * WHAT THIS CANNOT SEE: a module added to the chain later. A new carrier joins
 * the list below. An unreadable source is a failure, never a skip.
 */

import { readFileSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { describe, expect, it } from "vitest";

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

const CARRIERS = [
  // Browser: the card that shows it, the control that copies it, the builder.
  "components/dashboard/settings/invite-member.tsx",
  "components/setup/copy-button.tsx",
  "lib/invitations.ts",
  // Browser: the command call that returns the answer, and its fetch.
  "lib/command-client.ts",
  "lib/bff.ts",
  // Server: the route the answer passes through, and the API call behind it.
  "app/api/workspaces/[id]/commands/[command]/route.ts",
  "lib/target-api.ts",
  "lib/route-guards.ts",
];

const KEEPERS: [string, RegExp][] = [
  ["browser storage", /\b(?:localStorage|sessionStorage|indexedDB)\b|document\.cookie|\bcaches\.open\b/],
  ["analytics", /\btrackEvent\b|\bplausible\b|\bposthog\b|navigator\.sendBeacon|@\/lib\/analytics/],
  ["a log", /\bconsole\.(?:log|info|warn|error|debug|trace)\b/],
];

describe("the join link is kept nowhere", () => {
  for (const file of CARRIERS) {
    it(`${file} stores, tracks and logs nothing`, () => {
      const source = readFileSync(path.join(SRC, file), "utf8");
      const kept = KEEPERS.filter(([, pattern]) => pattern.test(source)).map(([what]) => what);
      expect(kept, `${file} can keep the link in ${kept.join(" and ")}`).toEqual([]);
    });
  }
});
