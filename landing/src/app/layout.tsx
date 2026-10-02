import type { Metadata } from "next"
import Script from "next/script"
import { Geist, Geist_Mono } from "next/font/google"
import { siteConfig } from "@/config/site"
import { homeSocial } from "@/lib/seo"
import "./globals.css"

const plausibleDomain = process.env.NEXT_PUBLIC_PLAUSIBLE_DOMAIN

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
})

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
  // Small labels only: not worth a high-priority download ahead of the H1.
  preload: false,
})

export const metadata: Metadata = {
  title: {
    template: "%s | Storydump",
    default: siteConfig.name + " — Instagram Stories from Google Drive, on tap",
  },
  description: siteConfig.description,
  authors: [{ name: siteConfig.author.name, url: siteConfig.contact.portfolio }],
  metadataBase: new URL(siteConfig.url),
  verification: {
    google: "JqcV49p6TP9UbtzZgflEngO3ijSsHRx8jtPV4qqxAj0",
  },
  robots: {
    index: true,
    follow: true,
    googleBot: {
      index: true,
      follow: true,
      "max-video-preview": -1,
      "max-image-preview": "large",
      "max-snippet": -1,
    },
  },
  // The home page's card is every page's default; the home page adds its own
  // og:url, so no other page claims to be the home page (seo-contract.test.ts).
  ...homeSocial,
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode
}>) {
  return (
    <html lang="en">
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased`}
      >
        {children}
        {/* An invite link's path is the invitation token, a bearer credential
            that goes nowhere but the router. The exclusions build sends no
            pageview for a path matching data-exclude. It excludes pageviews
            only, so no page under /join may fire a custom event either
            (join-fires-no-analytics-event-contract.test.ts holds this). */}
        {plausibleDomain && (
          <Script
            defer
            data-domain={plausibleDomain}
            data-exclude="/join/**"
            src="https://plausible.io/js/script.exclusions.js"
            strategy="afterInteractive"
          />
        )}
      </body>
    </html>
  )
}
