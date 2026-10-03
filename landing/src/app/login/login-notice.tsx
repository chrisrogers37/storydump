"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Notice } from "@/components/ui/notice";
import { WAITLIST_HREF, loginError } from "./content";

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
      <Link
        href={WAITLIST_HREF}
        className="font-medium underline underline-offset-4 hover:text-foreground"
      >
        {copy.link}
      </Link>
      {copy.tail}
    </Notice>
  );
}
