# Design

One source for how Storydump looks, shared by the marketing site, sign-in and
the signed-in app (all one Next.js app under `landing/`).

| File | What it holds |
|---|---|
| `tokens.css` | The palette, the font roles, the radius, the shadcn semantic tokens (`--primary`, `--muted-foreground`, …) pointed at the palette, and the type utilities `kicker`, `section-title` and `page-title`. Imported once, by `app/globals.css`. |
| `fonts.ts` | Geist, Geist Mono and Bricolage Grotesque, self-hosted by next/font. The root layout applies `fontVariables` to `<body>`. |
| `palette.ts` | The palette for code that can't read CSS (the OG image, the manifest). `palette.test.ts` holds it equal to `tokens.css`. |
| `brand.tsx` | `Wordmark`: the mark and the name. |
| `screen.tsx` | `Screen`: a standalone page on paper (sign-in, welcome, workspaces, an invitation, a sign-in error). |
| `page-header.tsx` | `PageHeader`: a page's display-face H1 and the line under it. |

The primitives (`Button`, `Card`, `Input`, …) stay in `components/ui`, where
shadcn puts them; they read the tokens above. Use `Button` or `buttonVariants`
for anything that looks like a button, and `CardTitle`, `CardLabel` and
`CardValue` for card headings and stats, rather than restating their classes.
