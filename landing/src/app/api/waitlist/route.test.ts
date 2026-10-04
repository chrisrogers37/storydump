import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { NextRequest } from "next/server"

const targetFetch = vi.fn()
vi.mock("@/lib/target-api", () => ({ targetFetch: (...args: unknown[]) => targetFetch(...args) }))
const notifyAdmin = vi.fn<(email: string) => Promise<void>>(async () => {})
vi.mock("@/lib/telegram", () => ({ notifyAdmin: (email: string) => notifyAdmin(email) }))
// The host runs an after() task once the answer is sent; here it is held, and
// a test runs it to see what it does.
const afterTasks: Array<() => unknown> = []
vi.mock("next/server", async (importOriginal) => ({
  ...(await importOriginal<typeof import("next/server")>()),
  after: (task: () => unknown) => void afterTasks.push(task),
}))

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
    afterTasks.length = 0
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
    expect(init.signal).toBeInstanceOf(AbortSignal)
    expect(body).toEqual({ email: "New@Example.com" })
    // The ping is not started before the answer; it is handed to the host.
    expect(notifyAdmin).not.toHaveBeenCalled()
    expect(afterTasks).toHaveLength(1)
    await afterTasks[0]()
    expect(notifyAdmin).toHaveBeenCalledWith("new@example.com")
  })

  it("hands the host the ping's promise, so the function lives until the send ends", async () => {
    targetFetch.mockResolvedValue({ ok: true, data: { status: "received" } })
    const ping = new Promise<void>(() => {})
    notifyAdmin.mockReturnValueOnce(ping)
    await POST(signup("a@example.com"))
    expect(afterTasks[0]()).toBe(ping)
  })

  it("passes on the API's invalid_email refusal as the form's own 400", async () => {
    targetFetch.mockResolvedValue({ ok: false, status: 400, error: "invalid_email" })
    const res = await POST(signup("odd@example.com"))
    expect(res.status).toBe(400)
    expect(await res.json()).toEqual(INVALID)
    expect(afterTasks).toHaveLength(0)
  })

  it.each([
    [503, "target_router_unreachable"],
    [429, "http_429"],
    // The site deployed before the API: the route is not there yet.
    [404, "http_404"],
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
    expect(afterTasks).toHaveLength(0)
  })

  it("leaves what an address is to the API, forwarding only a string", async () => {
    targetFetch.mockResolvedValue({ ok: false, status: 400, error: "invalid_email" })
    for (const email of [1, {}, null, "no-at-sign"]) {
      expect((await POST(signup(email))).status).toBe(400)
    }
    expect(targetFetch.mock.calls.map(([, , init]) => JSON.parse(init.body).email)).toEqual([
      "",
      "",
      "",
      "no-at-sign",
    ])
  })

  it("refuses an address longer than 254 characters without asking the API", async () => {
    targetFetch.mockResolvedValue({ ok: true, data: { status: "received" } })
    const at254 = `${"é".repeat(254 - "@example.com".length)}@example.com`
    expect((await POST(signup(at254))).status).toBe(200)
    targetFetch.mockClear()
    expect((await POST(signup(`  ${at254}\n`))).status).toBe(200)
    targetFetch.mockClear()
    const res = await POST(signup(`é${at254}`))
    expect(res.status).toBe(400)
    expect(await res.json()).toEqual(INVALID)
    expect(targetFetch).not.toHaveBeenCalled()
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

  it("forwards the campaign keys as strings cut by whole characters, and nothing else", async () => {
    targetFetch.mockResolvedValue({ ok: true, data: { status: "received" } })
    const res = await POST(
      signup("utm@example.com", {
        utm_source: "s".repeat(99) + "😀😀",
        utm_medium: { nested: "x" },
        utm_campaign: "launch",
        other: "dropped",
      })
    )
    expect(res.status).toBe(200)
    expect(forwarded().body).toEqual({
      email: "utm@example.com",
      utm_source: "s".repeat(99) + "😀",
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
  describe("the site's secret", () => {
    afterEach(() => vi.unstubAllEnvs())

    function from(headers: Record<string, string>) {
      return new NextRequest("https://storydump.app/api/waitlist", {
        method: "POST",
        headers: { "content-type": "application/json", "sec-fetch-site": "same-origin", ...headers },
        body: JSON.stringify({ email: "v@example.com" }),
      })
    }

    async function sent(req: NextRequest) {
      targetFetch.mockResolvedValue({ ok: true, data: { status: "received" } })
      expect((await POST(req)).status).toBe(200)
      return new Headers(forwarded().init.headers)
    }

    it("sends neither the secret nor the visitor while it is unset", async () => {
      vi.stubEnv("WAITLIST_SITE_SECRET", "")
      const headers = await sent(from({ "x-real-ip": "203.0.113.7" }))
      expect(headers.has("x-waitlist-site-secret")).toBe(false)
      expect(headers.has("x-waitlist-visitor-ip")).toBe(false)
    })

    it("sends the secret and the visitor Vercel reports", async () => {
      vi.stubEnv("WAITLIST_SITE_SECRET", "test-secret-not-real")
      const headers = await sent(from({ "x-real-ip": "203.0.113.7" }))
      expect(headers.get("x-waitlist-site-secret")).toBe("test-secret-not-real")
      expect(headers.get("x-waitlist-visitor-ip")).toBe("203.0.113.7")
    })

    // Read from the raw headers object: new Headers() would strip the same
    // whitespace and hide a missing trim.
    it("sends the secret without surrounding whitespace", async () => {
      vi.stubEnv("WAITLIST_SITE_SECRET", " test-secret-not-real\n")
      await sent(from({ "x-real-ip": "203.0.113.7" }))
      expect(forwarded().init.headers).toMatchObject({
        "X-Waitlist-Site-Secret": "test-secret-not-real",
      })
    })

    it("treats a whitespace-only secret as unset", async () => {
      vi.stubEnv("WAITLIST_SITE_SECRET", " \n")
      const headers = await sent(from({ "x-real-ip": "203.0.113.7" }))
      expect(headers.has("x-waitlist-site-secret")).toBe(false)
      expect(headers.has("x-waitlist-visitor-ip")).toBe(false)
    })

    it("prefers x-real-ip when both headers are present", async () => {
      vi.stubEnv("WAITLIST_SITE_SECRET", "test-secret-not-real")
      const headers = await sent(
        from({ "x-real-ip": "203.0.113.7", "x-forwarded-for": "198.51.100.1, 10.0.0.1" })
      )
      expect(headers.get("x-waitlist-visitor-ip")).toBe("203.0.113.7")
    })

    it("takes the first x-forwarded-for entry when x-real-ip is absent", async () => {
      vi.stubEnv("WAITLIST_SITE_SECRET", "test-secret-not-real")
      const headers = await sent(from({ "x-forwarded-for": " 2001:db8::1 , 10.0.0.1" }))
      expect(headers.get("x-waitlist-visitor-ip")).toBe("2001:db8::1")
    })

    it("sends the secret alone when no visitor address is known", async () => {
      vi.stubEnv("WAITLIST_SITE_SECRET", "test-secret-not-real")
      const headers = await sent(from({}))
      expect(headers.get("x-waitlist-site-secret")).toBe("test-secret-not-real")
      expect(headers.has("x-waitlist-visitor-ip")).toBe(false)
    })
  })
})
