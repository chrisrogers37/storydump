import { siteConfig } from "@/config/site"

/**
 * Generate OpenGraph metadata with a dynamic OG image for any page.
 *
 * The OG image route at /og-image.png accepts ?title= and ?subtitle=
 * query params to render page-specific social preview cards.
 */
export function ogMeta(title: string, description: string) {
  const ogImageUrl = new URL("/og-image.png", siteConfig.url)
  ogImageUrl.searchParams.set("title", title)
  ogImageUrl.searchParams.set("subtitle", description.slice(0, 100))

  return {
    openGraph: {
      title: `${title} | ${siteConfig.name}`,
      description,
      url: siteConfig.url,
      siteName: siteConfig.name,
      type: "article" as const,
      locale: "en_US",
      images: [
        {
          url: ogImageUrl.toString(),
          width: 1200,
          height: 630,
          alt: title,
        },
      ],
    },
    twitter: {
      card: "summary_large_image" as const,
      title: `${title} | ${siteConfig.name}`,
      description,
      images: [ogImageUrl.toString()],
    },
  }
}
