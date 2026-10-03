import { beforeEach, describe, expect, it, vi } from "vitest"
import { NextRequest } from "next/server"

const targetFetch = vi.fn()
vi.mock("@/lib/target-api", () => ({ targetFetch: (...args: unknown[]) => targetFetch(...args) }))
const notifyAdmin = vi.fn<(email: string) => Promise<void>>(async () => {})
vi.mock("@/lib/telegram", () => ({ notifyAdmin: (email: string) => notifyAdmin(email) }))

import { POST } from "./route"

const JOINED = { status: "success", message: "You're on the list!" }
const INVALID = { status: "error", message: "Please enter a valid email address." }

function signup(email: unknown, extra: Record<string, unknown> = {}) {
  return new NextRequest("https://storydump.app/api/waitlist", {
    method: "POST",
    headers: { "content-type": "application/json", "sec-fetch-site": "same-origin" },
    body: JSON.stringify({ email, ...extra }),
  })
}

function forwarded() {
  const [path, token, init] = targetFetch.mock.calls[0]
  return { path, token, init, body: JSON.parse(init.body) }
}

describe("POST /api/waitlist", () => {
  beforeEach(() => {
    targetFetch.mockReset()
    notifyAdmin.mockClear()
    vi.spyOn(console, "error").mockImplementation(() => {})
  })

  it("hands the address to the API's public plane with no credential, then pings the admin", async () => {
    targetFetch.mockResolvedValue({ ok: true, data: { status: "received" } })
    const res = await POST(signup("  New@Example.com "))
    expect(res.status).toBe(200)
    expect(await res.json()).toEqual(JOINED)
    const { path, token, init, body } = forwarded()
    expect(path).toBe("/waitlist")
    expect(token).toBeNull()
    expect(init).toMatchObject({ method: "POST", plane: "public" })
    expect(body).toEqual({ email: "new@example.com" })
    expect(notifyAdmin).toHaveBeenCalledWith("new@example.com")
  })

  it("passes on the API's invalid_email refusal as the form's own 400", async () => {
    targetFetch.mockResolvedValue({ ok: false, status: 400, error: "invalid_email" })
    const res = await POST(signup("odd@example.com"))
    expect(res.status).toBe(400)
    expect(await res.json()).toEqual(INVALID)
    expect(notifyAdmin).not.toHaveBeenCalled()
  })

  it.each([
    [503, "target_router_unreachable"],
    [429, "http_429"],
    [500, "http_500"],
  ])("logs a %s from the API and answers the generic 500", async (status, error) => {
    targetFetch.mockResolvedValue({ ok: false, status, error })
    const res = await POST(signup("someone@example.com"))
    expect(res.status).toBe(500)
    expect(await res.json()).toEqual({
      status: "error",
      message: "Something went wrong. Please try again.",
    })
    expect(console.error).toHaveBeenCalledWith("waitlist signup failed:", status, error)
    expect(notifyAdmin).not.toHaveBeenCalled()
  })

  it("accepts a 254-character email and refuses a 255-character one without calling the API", async () => {
    targetFetch.mockResolvedValue({ ok: true, data: { status: "received" } })
    const at254 = `${"a".repeat(254 - "@example.com".length)}@example.com`
    expect((await POST(signup(at254))).status).toBe(200)

    targetFetch.mockClear()
    const res = await POST(signup(`a${at254}`))
    expect(res.status).toBe(400)
    expect(await res.json()).toEqual(INVALID)
    expect(targetFetch).not.toHaveBeenCalled()
  })

  it("refuses a non-string email, and a body that is not an object, as invalid", async () => {
    for (const email of [1, {}, null]) {
      expect((await POST(signup(email))).status).toBe(400)
    }
    for (const raw of ["null", "[]", "not json"]) {
      const req = new NextRequest("https://storydump.app/api/waitlist", {
        method: "POST",
        headers: { "content-type": "application/json", "sec-fetch-site": "same-origin" },
        body: raw,
      })
      expect((await POST(req)).status).toBe(400)
    }
    expect(targetFetch).not.toHaveBeenCalled()
  })

  it("forwards each UTM value cut to 100 characters and drops a non-string one", async () => {
    targetFetch.mockResolvedValue({ ok: true, data: { status: "received" } })
    const res = await POST(
      signup("utm@example.com", {
        utm_source: ` ${"s".repeat(150)} `,
        utm_medium: "email",
        utm_campaign: { nested: "x".repeat(500) },
      })
    )
    expect(res.status).toBe(200)
    expect(forwarded().body).toEqual({
      email: "utm@example.com",
      utm_source: "s".repeat(100),
      utm_medium: "email",
    })
  })

  it("refuses a request another site made before reading it", async () => {
    const req = new NextRequest("https://storydump.app/api/waitlist", {
      method: "POST",
      headers: { "content-type": "application/json", "sec-fetch-site": "cross-site" },
      body: JSON.stringify({ email: "x@example.com" }),
    })
    expect((await POST(req)).status).toBe(403)
    expect(targetFetch).not.toHaveBeenCalled()
  })
})
