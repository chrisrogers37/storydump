import { beforeEach, describe, expect, it, vi } from "vitest"
import { NextRequest } from "next/server"
import { DrizzleQueryError } from "drizzle-orm/errors"

const insertValues = vi.fn()
vi.mock("@/lib/db", () => ({
  getDb: () => ({ insert: () => ({ values: insertValues }) }),
}))
vi.mock("@/lib/telegram", () => ({ notifyAdmin: vi.fn(async () => {}) }))

import { POST } from "./route"

function signup(email: string, extra: Record<string, unknown> = {}) {
  return new NextRequest("https://storydump.app/api/waitlist", {
    method: "POST",
    headers: { "content-type": "application/json", "sec-fetch-site": "same-origin" },
    body: JSON.stringify({ email, ...extra }),
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

  it("answers a returning email exactly like a new one when Drizzle wraps the unique violation", async () => {
    const driverError = Object.assign(new Error("duplicate key value"), { code: "23505" })
    insertValues.mockRejectedValue(
      new DrizzleQueryError("insert into waitlist_signups ...", [], driverError)
    )
    const res = await POST(signup("again@example.com"))
    expect(res.status).toBe(200)
    expect(await res.json()).toEqual({ status: "success", message: "You're on the list!" })
  })

  it("still reads an unwrapped unique violation", async () => {
    insertValues.mockRejectedValue(Object.assign(new Error("duplicate"), { code: "23505" }))
    const res = await POST(signup("again@example.com"))
    expect(await res.json()).toEqual({ status: "success", message: "You're on the list!" })
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

  it("accepts a 254-character email and refuses a 255-character one as invalid", async () => {
    insertValues.mockResolvedValue(undefined)
    const at254 = `${"a".repeat(254 - "@example.com".length)}@example.com`
    expect((await POST(signup(at254))).status).toBe(200)

    insertValues.mockClear()
    const res = await POST(signup(`a${at254}`))
    expect(res.status).toBe(400)
    expect(await res.json()).toEqual({
      status: "error",
      message: "Please enter a valid email address.",
    })
    expect(insertValues).not.toHaveBeenCalled()
  })

  it("refuses a non-string email as invalid, not as a server error", async () => {
    for (const email of [1, {}, null]) {
      const res = await POST(signup(email as unknown as string))
      expect(res.status).toBe(400)
    }
    expect(insertValues).not.toHaveBeenCalled()
  })

  it("cuts each UTM value to 100 characters and ignores a non-string one", async () => {
    insertValues.mockResolvedValue(undefined)
    const res = await POST(
      signup("utm@example.com", {
        utm_source: ` ${"s".repeat(150)} `,
        utm_medium: "email",
        utm_campaign: { nested: "x".repeat(500) },
      })
    )
    expect(res.status).toBe(200)
    expect(insertValues).toHaveBeenCalledWith({
      email: "utm@example.com",
      notes: JSON.stringify({ utm_source: "s".repeat(100), utm_medium: "email" }),
    })
  })
})
