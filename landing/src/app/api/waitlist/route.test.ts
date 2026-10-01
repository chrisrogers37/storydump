import { beforeEach, describe, expect, it, vi } from "vitest"
import { NextRequest } from "next/server"
import { DrizzleQueryError } from "drizzle-orm/errors"

const insertValues = vi.fn()
vi.mock("@/lib/db", () => ({
  getDb: () => ({ insert: () => ({ values: insertValues }) }),
}))
vi.mock("@/lib/telegram", () => ({ notifyAdmin: vi.fn(async () => {}) }))

import { POST } from "./route"

function signup(email: string) {
  return new NextRequest("https://storydump.app/api/waitlist", {
    method: "POST",
    headers: { "content-type": "application/json", "sec-fetch-site": "same-origin" },
    body: JSON.stringify({ email }),
  })
}

describe("POST /api/waitlist", () => {
  beforeEach(() => {
    insertValues.mockReset()
    vi.spyOn(console, "error").mockImplementation(() => {})
  })

  it("adds a new email", async () => {
    insertValues.mockResolvedValue(undefined)
    const res = await POST(signup("new@example.com"))
    expect(res.status).toBe(200)
    expect(await res.json()).toMatchObject({ status: "success", message: "You're on the list!" })
  })

  it("answers a returning email as already on the list when Drizzle wraps the unique violation", async () => {
    const driverError = Object.assign(new Error("duplicate key value"), { code: "23505" })
    insertValues.mockRejectedValue(
      new DrizzleQueryError("insert into waitlist_signups ...", [], driverError)
    )
    const res = await POST(signup("again@example.com"))
    expect(res.status).toBe(200)
    expect(await res.json()).toMatchObject({ status: "success", alreadyRegistered: true })
  })

  it("still reads an unwrapped unique violation", async () => {
    insertValues.mockRejectedValue(Object.assign(new Error("duplicate"), { code: "23505" }))
    const res = await POST(signup("again@example.com"))
    expect(await res.json()).toMatchObject({ alreadyRegistered: true })
  })

  it("logs any other failure and answers 500", async () => {
    const missingTable = Object.assign(new Error('relation "waitlist_signups" does not exist'), {
      code: "42P01",
    })
    insertValues.mockRejectedValue(new DrizzleQueryError("insert ...", [], missingTable))
    const res = await POST(signup("someone@example.com"))
    expect(res.status).toBe(500)
    expect(console.error).toHaveBeenCalledWith("waitlist signup failed:", expect.anything())
  })
})
