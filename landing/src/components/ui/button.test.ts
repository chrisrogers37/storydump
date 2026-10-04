import { describe, expect, it } from "vitest";
import { buttonVariants } from "./button";

/** cva alone concatenates, so a caller's override and the size's own class
 *  would both land and CSS order would pick; buttonVariants merges them. */
describe("buttonVariants", () => {
  it("lets a caller's class replace the size's", () => {
    const classes = buttonVariants({ size: "lg", className: "h-12 px-5" }).split(" ");
    expect(classes).toContain("h-12");
    expect(classes).toContain("px-5");
    expect(classes).not.toContain("h-10");
    expect(classes).not.toContain("px-6");
  });

  it("gives every variant the solid focus ring and a forced-colors-safe outline", () => {
    for (const variant of ["default", "destructive", "outline", "secondary", "ghost", "link"] as const) {
      const classes = buttonVariants({ variant }).split(" ");
      expect(classes, variant).toContain("focus-visible:ring-ring");
      expect(classes, variant).toContain("focus-visible:outline-hidden");
      expect(classes, variant).not.toContain("outline-none");
    }
  });
});
