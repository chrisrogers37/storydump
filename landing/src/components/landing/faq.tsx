"use client"

import { TextLink } from "@/components/landing/text-link"
import { siteConfig } from "@/config/site"
import { faqs } from "@/config/faqs"
import {
  Accordion,
  AccordionItem,
  AccordionTrigger,
  AccordionContent,
} from "@/components/ui/accordion"
import { trackEvent } from "@/lib/analytics"

export function FAQ() {
  return (
    <section aria-labelledby="faq-heading" className="py-16 md:py-24">
      <div className="mx-auto grid max-w-6xl gap-8 px-4 md:grid-cols-[1fr_1.6fr] md:gap-16">
        <h2
          id="faq-heading"
          className="section-title"
        >
          Questions
        </h2>
        <Accordion
          type="single"
          collapsible
          onValueChange={(value) => {
            if (value) {
              const idx = parseInt(value.replace("faq-", ""), 10)
              const faq = faqs[idx]
              if (faq) trackEvent("FAQ Expanded", { question: faq.question })
            }
          }}
        >
          {faqs.map((faq, i) => (
            <AccordionItem key={i} value={`faq-${i}`}>
              <AccordionTrigger className="text-base font-semibold text-ink md:text-lg">
                {faq.question}
              </AccordionTrigger>
              <AccordionContent>
                {faq.question === "Who built this?" ? (
                  <p className="text-muted-foreground">
                    Storydump is built by{" "}
                    <a
                      href={siteConfig.contact.portfolio}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="underline underline-offset-4 hover:text-foreground"
                    >
                      {siteConfig.author.name}
                    </a>
                    . Have questions or feedback? Reach out at{" "}
                    <a
                      href={`mailto:${siteConfig.contact.email}`}
                      className="underline underline-offset-4 hover:text-foreground"
                    >
                      {siteConfig.contact.email}
                    </a>
                    .
                  </p>
                ) : (
                  <p className="text-muted-foreground">{faq.answer}</p>
                )}
              </AccordionContent>
            </AccordionItem>
          ))}
        </Accordion>
        <p className="mt-8 text-sm text-ink/70">
          Read more on the blog:{" "}
          <TextLink href="/blog/automate-instagram-stories">how to automate Instagram Stories</TextLink>
          {" "}and{" "}
          <TextLink href="/blog/telegram-instagram-approval-workflow">approving Instagram Stories in Telegram</TextLink>
          .
        </p>
      </div>
    </section>
  )
}
