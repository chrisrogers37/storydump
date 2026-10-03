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
    expect(body).toEqual({ email: "  New@Example.com " })
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

  it("leaves what an address is to the API: it forwards one this route cannot judge", async () => {
    targetFetch.mockResolvedValue({ ok: false, status: 400, error: "invalid_email" })
    for (const email of [1, {}, null, "no-at-sign", "a".repeat(300)]) {
      expect((await POST(signup(email))).status).toBe(400)
    }
    expect(targetFetch).toHaveBeenCalledTimes(5)
  })

  it("refuses a body that is not a JSON object without calling the API", async () => {
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

  it("forwards the campaign keys and nothing else", async () => {
    targetFetch.mockResolvedValue({ ok: true, data: { status: "received" } })
    const res = await POST(
      signup("utm@example.com", {
        utm_source: "newsletter",
        utm_campaign: "launch",
        other: "dropped",
      })
    )
    expect(res.status).toBe(200)
    expect(forwarded().body).toEqual({
      email: "utm@example.com",
      utm_source: "newsletter",
      utm_campaign: "launch",
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
