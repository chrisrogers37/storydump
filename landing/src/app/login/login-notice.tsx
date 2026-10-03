"use client";

import { WaitlistLink } from "@/components/layout/waitlist-link";
import { useSearchParams } from "next/navigation";
import { Notice } from "@/components/ui/notice";
import { loginError } from "./content";

/**
 * The refusal `?error=` names, above the sign-in button. A client component
 * so the page stays prerendered; the page wraps it in Suspense, as Next
 * requires of a static page that reads the query.
 */
export function LoginNotice() {
  const copy = loginError(useSearchParams().get("error"));
  if (!copy) return null;
  return (
    <Notice>
      {copy.lead}
      <WaitlistLink
        location="login"
        className="font-medium underline underline-offset-4 hover:text-foreground"
      >
        {copy.link}
      </WaitlistLink>
      {copy.tail}
    </Notice>
  );
}
