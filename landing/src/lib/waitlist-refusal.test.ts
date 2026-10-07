import { describe, expect, it } from "vitest"
import { waitlistRefusal } from "./waitlist-refusal"

describe("waitlistRefusal", () => {
  it("is busy only for the route's busy 429", () => {
    expect(waitlistRefusal(429, { status: "error", reason: "busy" })).toBe("busy")
  })

  it.each([
    // A busy body with another status is not the route's busy answer.
    [500, { reason: "busy" }],
    [503, { reason: "busy" }],
    // A 429 without the reason (another limit) is a plain error.
    [429, { status: "error" }],
    [429, { reason: "full" }],
    [400, { status: "error", message: "Please enter a valid email address." }],
    [429, null],
  ])("is an error for %s %j", (status, body) => {
    expect(waitlistRefusal(status, body)).toBe("error")
  })
})
