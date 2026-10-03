import { siteConfig } from "@/config/site"
import { faqs } from "@/config/faqs"
import { JsonLd } from "@/components/seo/json-ld"
import { absoluteUrl } from "@/lib/seo"

const softwareAppSchema = {
  "@context": "https://schema.org",
  "@type": "SoftwareApplication",
  name: siteConfig.name,
  description: siteConfig.description,
  url: siteConfig.url,
  applicationCategory: "SocialNetworkingApplication",
  operatingSystem: "Web",
  offers: {
    "@type": "Offer",
    price: "0",
    priceCurrency: "USD",
    description: "Free during beta",
  },
}

const organizationSchema = {
  "@context": "https://schema.org",
  "@type": "Organization",
  name: siteConfig.name,
  url: siteConfig.url,
  logo: absoluteUrl("/icon-512.png"),
  contactPoint: {
    "@type": "ContactPoint",
    email: siteConfig.contact.email,
    contactType: "customer support",
  },
}

const faqSchema = {
  "@context": "https://schema.org",
  "@type": "FAQPage",
  mainEntity: faqs.map((faq) => ({
    "@type": "Question",
    name: faq.question,
    acceptedAnswer: {
      "@type": "Answer",
      text: faq.answer,
    },
  })),
}

export function StructuredData() {
  return (
    <>
      <JsonLd data={softwareAppSchema} />
      <JsonLd data={organizationSchema} />
      <JsonLd data={faqSchema} />
    </>
  )
}
