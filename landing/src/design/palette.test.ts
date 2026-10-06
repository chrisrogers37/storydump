import { readFileSync } from "fs"
import path from "path"
import { describe, expect, it } from "vitest"
import { palette } from "./palette"

const tokens = readFileSync(path.join(__dirname, "tokens.css"), "utf8")

describe("palette.ts mirrors tokens.css", () => {
  it.each(Object.entries(palette))("--color-%s", (name, value) => {
    expect(tokens).toContain(`--color-${name}: ${value};`)
  })
})
