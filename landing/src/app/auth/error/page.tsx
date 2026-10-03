import Link from "next/link";
import { siteConfig } from "@/config/site";
import { resolveContent, resolveFlow } from "./content";
import { noindexMetadata } from "@/lib/seo";
import { buttonVariants } from "@/components/ui/button";
import { PageHeader } from "@/design/page-header";
import { Screen } from "@/design/screen";

/**
 * The three sign-in failure states, which are not interchangeable.
 *
 * Each is specified separately by the security model and a person hitting the
 * third will otherwise believe the product has lost their account. Purely
 * presentational — it renders a reason, it does not decide one.
 *
 * An unrecognised reason falls back to the generic shape rather than a blank
 * page or a raw code.
 */

/**
 * The tab title is part of the claim. "Sign-in problem" on a Drive failure is
 * the same false statement as the heading was, in the one place a reader sees
 * before the page even paints.
 */
export async function generateMetadata({
  searchParams,
}: {
  searchParams: Promise<{ reason?: string; flow?: string }>;
}) {
  const { flow } = await searchParams;
  const resolved = resolveFlow(flow);
  const what =
    resolved === "drive"
      ? "Drive connection problem"
      : resolved === "instagram"
        ? "Instagram connection problem"
        : "Sign-in problem";
  return {
    title: what,
    description: `${siteConfig.name} couldn’t finish connecting your account.`,
    ...noindexMetadata,
  };
}

export default async function AuthErrorPage({
  searchParams,
}: {
  searchParams: Promise<{ reason?: string; flow?: string }>;
}) {
  const { reason, flow } = await searchParams;
  const content = resolveContent(resolveFlow(flow), reason);

  return (
    <Screen className="text-center">
      <PageHeader align="center" title={content.heading} description={content.body} />

      <Link
        href={content.href}
        className={buttonVariants({ size: "lg" })}
      >
        {content.primary}
      </Link>

      {content.secondary && (
        <div>
          <a
            href={`mailto:${siteConfig.contact.email}`}
            className="text-sm text-muted-foreground underline underline-offset-4 transition-colors hover:text-foreground"
          >
            {content.secondary}
          </a>
        </div>
      )}
    </Screen>
  );
}
