# Design system

One source for how Storydump looks, shared by the marketing site, sign-in and
the signed-in app (all one Next.js app under `landing/`). It lives in
`landing/src/design/`.

| File | What it holds |
|---|---|
| `tokens.css` | The palette (tap, tap-ink, paper, ink, alarm and the warm greys), the `surface-app` utility, the font roles, the radius, the shadcn semantic tokens (`--primary`, `--muted-foreground`, `--ring`, …) and the type utilities `kicker`, `section-title` and `page-title`. Imported once, by `app/globals.css`. |
| `fonts.ts` | Geist, Geist Mono and Bricolage Grotesque, self-hosted by next/font. The root layout applies `fontVariables` to `<body>`. |
| `palette.ts` | The palette for code that can't read CSS (the OG image, the manifest). `palette.test.ts` holds it equal to `tokens.css`. |
| `brand.tsx` | `Wordmark`: the mark and the name. |
| `screen.tsx` | `Screen`: a standalone page on paper (sign-in, welcome, workspaces, an invitation, a sign-in error). |
| `page-header.tsx` | `PageHeader`: a page's display-face H1 and the line under it. |

## Rules

- **The landing home page is the source of truth.** The semantic tokens hold
  the exact values the home page renders with, so a change to them changes
  every public page. Check home and a use-case page with a strict pixel diff
  before changing one.
- **The app's surfaces add `surface-app`**: paper, with the warm greys (sand,
  line, ink-muted) in place of the neutral ones, because the neutral muted grey
  is under 4.5:1 on paper. `Screen` and the dashboard layout set it; a new
  standalone app page should use `Screen` rather than set it by hand.
- **Focus is a solid 2px tap-ink ring** (`--ring`, 5.9:1 on white). Don't add
  an alpha to `ring-ring`, and hide the browser outline with
  `focus-visible:outline-hidden`, never `outline-none`, so Windows
  high-contrast mode still draws one.
- **A setup guide ends with `SetupPager`** (`landing/src/components/setup/`).
- **Primitives stay in `landing/src/components/ui`**, where shadcn puts them;
  they read the tokens above. Use `Button` or `buttonVariants` for anything
  that looks like a button (size `xl` is the site's 44px call to action),
  `CardTitle` for a card heading and `StatCard` for a figure with its label,
  rather than restating their classes.
- **Badge colours come from `TONE_CLASS`** in
  `landing/src/components/dashboard/tone.ts`: active (green), attention
  (amber), inert (grey), progress (tap) and problem (alarm red). Don't reach
  for Tailwind's default palette in a chip.
