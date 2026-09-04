/**
 * The copy for `/auth/error`, split from the page so the VOCABULARY is
 * testable without a DOM or a React renderer.
 *
 * The page renders; this decides. Keeping them together meant the only way
 * to check that a Drive failure never says "sign in" was to read it.
 */

/**
 * TWO LEGS REACH THIS PAGE, and they are not interchangeable.
 *
 * `auth.py` sends `flow=drive` on a Drive-connect failure and nothing on
 * sign-in — and its own docstring already said what was missing: "the page
 * today is sign-in-shaped (title, CTAs), and needs to know which leg it
 * renders for." The producer shipped the parameter; this page typed
 * `{ reason?: string }` and never read it.
 *
 * The consequence was a false statement about the reader's session at the
 * moment they were trying to work out what went wrong: a declined Drive grant
 * rendered "Sign-in was cancelled." — while they were still signed in — and
 * sent them to /login to fix it. Nobody who follows that instruction gets
 * their Drive folder connected, and some of them lose the tab they were
 * working in.
 *
 * `grant_incomplete` was also absent from the vocabulary, so it fell to the
 * generic copy, which likewise says "sign you in". The comment below predicted
 * exactly that ("if the API adds a sixth, it lands here as the fallback") —
 * the sixth already existed.
 */
export type Flow = "signin" | "drive" | "instagram";

/** Reasons either leg can send. */
export type SharedReason =
  | "denied"
  | "missing_params"
  | "state_refused"
  | "exchange_failed";

/** Sign-in only: no Drive grant can collide an identity. */
export type SigninReason = SharedReason | "identity_collision";

/** Drive only: `google_drive_oauth.REDIRECT_REASON` maps
 *  `no_refresh_token` and `scope_not_granted` onto it. */
export type DriveReason = SharedReason | "grant_incomplete";

/** Instagram only: the real account is already another destination here. */
export type InstagramReason =
  | SharedReason
  | "already_connected"
  | "wrong_account"
  | "destination_gone"
  | "workspace_closing";

export type Content = {
  heading: string;
  body: string;
  /** Where the primary action goes. Every reason here is recoverable by
   *  starting again, so it is /login unless a reason says otherwise. */
  href: string;
  primary: string;
  /** Rendered only where a person genuinely cannot self-serve. */
  secondary?: string;
};

/**
 * Sign-in copy. Keyed on the union, so adding a reason to `SigninReason`
 * fails `tsc` until copy exists rather than falling silently to `generic`.
 */
export const SIGNIN: Record<SigninReason | "generic", Content> = {
  denied: {
    heading: "Sign-in was cancelled.",
    body: "You closed the Google window or declined the request. Nothing was created and nothing was shared.",
    href: "/login",
    primary: "Try again",
  },
  missing_params: {
    heading: "That sign-in link was incomplete.",
    body: "It looks like the address was cut short somewhere. Start again from the sign-in page.",
    href: "/login",
    primary: "Back to sign-in",
  },
  state_refused: {
    heading: "That sign-in attempt has expired.",
    body: "A sign-in has to finish in one go, from the browser that started it. Start again and it should work.",
    href: "/login",
    primary: "Start again",
  },
  exchange_failed: {
    heading: "We could not finish signing you in.",
    body: "Google accepted you but the last step did not complete. This one is on us — try again in a moment.",
    href: "/login",
    primary: "Try again",
  },
  identity_collision: {
    // Says WHY, not just what. The reason is a real property of the account
    // model — the API never merges two identities onto one email (D35) — and a
    // person who is told "already in use" without that will keep retrying.
    heading: "That email already belongs to another account.",
    body: "Accounts are never merged, so this address cannot be added to a second one. Sign in with the account that already uses it.",
    href: "/login",
    primary: "Sign in with that account",
    secondary: "Contact us if you think this is wrong",
  },
  generic: {
    heading: "We could not sign you in.",
    body: "Something went wrong on the way back from Google. Start again from the sign-in page.",
    href: "/login",
    primary: "Back to sign-in",
    secondary: "Contact us if this keeps happening",
  },
};

/**
 * Drive-connect copy. Same reasons where they are shared, different sentences
 * — because the reader is SIGNED IN and was connecting a folder.
 *
 * Three properties every entry here holds, and the old page broke all three:
 * it never says "sign in" or "session"; it never claims the account is
 * affected; and `href` returns to Settings, where they were, not to /login.
 */
export const DRIVE: Record<DriveReason | "generic", Content> = {
  denied: {
    // Describes the OBSERVABLE, not the cause. `denied` is reached when the
    // person declined AND when Google refused (#1116's finding), and this page
    // cannot tell which — so it says what came back rather than what someone
    // did. An earlier draft of this entry read "You closed the Google window
    // or declined access", which is the same collapse one leg over.
    heading: "Google Drive was not connected.",
    body: "Google came back without granting access. That happens if the request was declined, and it can also happen when Google refuses it. You are still signed in and nothing about your workspace changed.",
    href: "/dashboard/settings",
    primary: "Try connecting again",
  },
  missing_params: {
    heading: "That Drive connection link was incomplete.",
    body: "The address was cut short on the way back from Google. Nothing was connected. Start the connection again from Settings.",
    href: "/dashboard/settings",
    primary: "Back to settings",
  },
  state_refused: {
    heading: "That Drive connection attempt has expired.",
    body: "A connection has to finish in one go, from the browser that started it. Nothing was connected — start it again and it should work.",
    href: "/dashboard/settings",
    primary: "Try connecting again",
  },
  exchange_failed: {
    heading: "We could not finish connecting Drive.",
    body: "Google accepted the request but the last step did not complete. Nothing was connected. This one is on us — try again in a moment.",
    href: "/dashboard/settings",
    primary: "Try connecting again",
  },
  grant_incomplete: {
    // Says WHY, because the remedy depends on it: this is the one Drive
    // failure a person can fix by doing something DIFFERENT rather than by
    // repeating themselves. Retrying identically reproduces it.
    heading: "Drive was connected without the access we need.",
    body: "Google returned a grant that leaves out either offline access or the Drive permission, so syncing could not be set up. Nothing was connected. Start again and accept both prompts.",
    href: "/dashboard/settings",
    primary: "Try connecting again",
    secondary: "Contact us if it keeps coming back incomplete",
  },
  generic: {
    heading: "We could not connect Google Drive.",
    body: "Something went wrong on the way back from Google. Nothing was connected and you are still signed in.",
    href: "/dashboard/settings",
    primary: "Back to settings",
    secondary: "Contact us if this keeps happening",
  },
};

/**
 * Instagram-connect copy (#1220 step 2). The reader is SIGNED IN and was
 * connecting a destination from Settings; the same three properties the Drive
 * table holds — never "sign in", never a claim about the account, `href` back
 * to Settings.
 */
export const INSTAGRAM: Record<InstagramReason | "generic", Content> = {
  denied: {
    heading: "Instagram was not connected.",
    body: "Instagram came back without granting access. That happens if the request was declined, and it can also happen when Instagram refuses it — an account that is not a Professional account, or one the app is not yet approved for. Nothing about your workspace changed.",
    href: "/dashboard/settings",
    primary: "Try connecting again",
  },
  missing_params: {
    heading: "That Instagram connection link was incomplete.",
    body: "The address was cut short on the way back from Instagram. Nothing was connected. Start the connection again from Settings.",
    href: "/dashboard/settings",
    primary: "Back to settings",
  },
  state_refused: {
    heading: "That Instagram connection attempt has expired.",
    body: "A connection has to finish in one go, from the browser that started it, by an admin of the workspace. Nothing was connected — start it again and it should work.",
    href: "/dashboard/settings",
    primary: "Try connecting again",
  },
  exchange_failed: {
    heading: "We could not finish connecting Instagram.",
    body: "Instagram accepted the request but the last step did not complete. Nothing was connected. This one is on us — try again in a moment.",
    href: "/dashboard/settings",
    primary: "Try connecting again",
  },
  already_connected: {
    heading: "That Instagram account is already a destination here.",
    body: "The account you signed in with is already connected to another destination in this workspace, so it was not attached a second time. Nothing changed. Remove the duplicate destination, or connect a different account.",
    href: "/dashboard/settings",
    primary: "Back to settings",
  },
  wrong_account: {
    // Says WHY, because the remedy is "sign in to a different account", not
    // "try again": a destination is for ONE Instagram account, and the one
    // that signed in is not it.
    heading: "That is not the Instagram account this destination is for.",
    body: "Instagram authorised a different account than this destination was set up for. Nothing was connected and the destination was not changed. Switch Instagram to that account and try again — or add the account Instagram authorised as its own destination.",
    href: "/dashboard/settings",
    primary: "Back to settings",
  },
  workspace_closing: {
    heading: "This workspace is being deleted.",
    body: "Nothing new can be connected to a workspace that is closing, so the Instagram account you authorised was not added. Nothing was connected — restore the workspace first if that was a mistake.",
    href: "/dashboard/settings",
    primary: "Back to settings",
  },
  destination_gone: {
    heading: "That destination is no longer here.",
    body: "The destination you were connecting was removed, or its workspace is being deleted, while you were at Instagram. Nothing was connected.",
    href: "/dashboard/settings",
    primary: "Back to settings",
  },
  generic: {
    heading: "We could not connect Instagram.",
    body: "Something went wrong on the way back from Instagram. Nothing was connected.",
    href: "/dashboard/settings",
    primary: "Back to settings",
    secondary: "Contact us if this keeps happening",
  },
};

/** `auth.py` sends `flow=drive` on the Drive leg, `flow=instagram` on the
 *  Instagram connect leg, and nothing on sign-in. */
export function resolveFlow(flow: string | undefined): Flow {
  if (flow === "drive") return "drive";
  if (flow === "instagram") return "instagram";
  return "signin";
}

export function resolveContent(flow: Flow, reason: string | undefined): Content {
  const table: Record<string, Content> =
    flow === "drive" ? DRIVE : flow === "instagram" ? INSTAGRAM : SIGNIN;
  return reason && reason !== "generic" && reason in table
    ? table[reason]
    : table.generic;
}

