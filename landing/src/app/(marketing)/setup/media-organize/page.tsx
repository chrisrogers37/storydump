import type { Metadata } from "next";
import { pageMetadata } from "@/lib/seo";
import Link from "next/link";
import { ArrowLeft, ArrowRight } from "lucide-react";
import { StepCard } from "@/components/setup/step-card";
import { Callout } from "@/components/setup/callout";
import { UiTerm } from "@/components/setup/ui-term";
import { buttonVariants } from "@/components/ui/button";
import { SetupPager } from "@/components/setup/setup-pager"

const description =
  "How to set up your Google Drive folders for Storydump. Each folder you connect gets its own share of your Instagram Stories, and you set the mix.";

export const metadata: Metadata = pageMetadata({
  title: "Organize Your Media",
  description,
  path: "/setup/media-organize",
});

export default function MediaOrganize() {
  return (
    <div>
      <h1 className="page-title text-4xl text-ink">
        Organizing Your Media
      </h1>
      <p className="mt-4 text-lg text-muted-foreground">
        Storydump reads media from your Google Drive. The folders you connect
        are the groups of your posting mix.
      </p>

      <div className="mt-10 space-y-10">
        <StepCard number={1} title="Connected folders = groups">
          <p>
            Each folder you connect is a group of its own: everything inside it
            syncs, at any depth, and the group can carry a share of your posts.
            To weight two kinds of content separately, connect them as two
            folders — subfolders inside a connected folder are just structure.
            Rename folders freely; the weight follows the folder.
          </p>
          <pre className="mt-3 overflow-x-auto rounded-lg bg-muted p-4 text-xs max-md:scroll-hint sm:text-sm">
            {`My Instagram Stories/
├── memes/                     ← connect this folder  (group "memes", 70%)
│   ├── 2025/
│   │   └── funny-cat.jpg      ← synced: any depth
│   └── monday-mood.png
└── merch/                     ← connect this folder  (group "merch", 30%)
    ├── new-tshirt.jpg
    └── sale-banner.png`}
          </pre>
        </StepCard>

        <StepCard number={2} title="Media requirements">
          <ul className="list-inside list-disc space-y-1">
            <li>
              <span className="font-medium text-foreground">Aspect ratio:</span>{" "}
              9:16 (1080x1920 ideal). Other shapes are framed to fit, never
              cropped.
            </li>
            <li>
              <span className="font-medium text-foreground">Formats:</span>{" "}
              images and videos
            </li>
            <li>
              <span className="font-medium text-foreground">Max size:</span>{" "}
              8 MB per image, 40 MB per video
            </li>
          </ul>
          <Callout type="tip" className="mt-3">
            Storydump fits each file to 9:16 when it posts for you, so you
            don&apos;t need to resize anything. Do trim long videos: a file
            over the limit can&apos;t go out through Instagram.
          </Callout>
        </StepCard>

        <StepCard number={3} title="Set your posting mix">
          <p>
            On the web, open{" "}
            <UiTerm>Settings &rarr; General &rarr; Posting mix</UiTerm>. Each connected folder gets one of three settings:
          </p>
          <ul className="mt-2 list-inside list-disc space-y-1">
            <li>
              <UiTerm>Weight:</UiTerm> a
              fixed share, like 70% memes and 30% merch. Weights add up to 100.
            </li>
            <li>
              <UiTerm>Automatic:</UiTerm>{" "}
              posts in proportion to how many files the folder holds.
            </li>
            <li>
              <UiTerm>Off:</UiTerm> stays
              synced but never posts.
            </li>
          </ul>
          <p className="mt-2">
            <UiTerm>Split evenly</UiTerm>{" "}
            gives every folder the same share. Change the mix anytime; it
            applies from the next posting slot. Workspace admins can edit it.
          </p>
        </StepCard>

        <StepCard number={4} title="How many files do you need?">
          <ul className="list-inside list-disc space-y-1">
            <li>
              <span className="font-medium text-foreground">Minimum:</span> ~30
              files for a week of posting (3/day &times; 7 days + buffer)
            </li>
            <li>
              <span className="font-medium text-foreground">Ideal:</span> 100+
              for good variety
            </li>
          </ul>
          <p className="mt-2">
            Storydump tracks what&apos;s been posted. In each folder, files
            that have never been posted go first, then whatever has waited
            longest.
          </p>
        </StepCard>

        <StepCard number={5} title="Tips">
          <ul className="list-inside list-disc space-y-1">
            <li>
              Keep filenames descriptive — they show up in your Queue on the web
            </li>
            <li>
              Remove content you&apos;d never want to post — Storydump will try
              to post everything in the folder. To keep a folder synced without
              posting from it, set it to Off.
            </li>
            <li>
              You can add or remove files anytime — Storydump syncs on its own,
              and <UiTerm>Sync Now</UiTerm>{" "}
              in Settings &rarr; Integrations pulls changes in right away
            </li>
          </ul>
        </StepCard>
      </div>

      <SetupPager>
        <Link
          href="/setup/instagram"
          className="inline-flex items-center gap-2 text-sm text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4" />
          Instagram Account
        </Link>
        <Link
          href="/setup/connect"
          className={buttonVariants()}
        >
          Next: Connect Telegram (optional)
          <ArrowRight className="h-4 w-4" />
        </Link>
      </SetupPager>
    </div>
  );
}
