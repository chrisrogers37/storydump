import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { notifyAdmin } from "./telegram"

describe("notifyAdmin", () => {
  const fetchMock = vi.fn()

  beforeEach(() => {
    vi.stubEnv("TELEGRAM_BOT_TOKEN", "test-token")
    vi.stubEnv("ADMIN_TELEGRAM_CHAT_ID", "test-chat")
    vi.stubGlobal("fetch", fetchMock)
    fetchMock.mockReset()
    vi.spyOn(console, "error").mockImplementation(() => {})
    vi.spyOn(console, "warn").mockImplementation(() => {})
  })

  afterEach(() => {
    vi.unstubAllEnvs()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  /** Every log line, flattened, to show the token is in none of them. */
  const logged = () =>
    JSON.stringify([vi.mocked(console.error).mock.calls, vi.mocked(console.warn).mock.calls])

  it("sends plain text with no link preview, so an address cannot format the message", async () => {
    fetchMock.mockResolvedValue(new Response("{}", { status: 200 }))
    await notifyAdmin("*bold*@example.com")
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe("https://api.telegram.org/bottest-token/sendMessage")
    const sent = JSON.parse(init.body)
    expect(sent.chat_id).toBe("test-chat")
    expect(sent).not.toHaveProperty("parse_mode")
    expect(sent.link_preview_options).toEqual({ is_disabled: true })
    expect(sent.text).toContain("*bold*@example.com")
    expect(init.signal).toBeInstanceOf(AbortSignal)
    expect(console.error).not.toHaveBeenCalled()
  })

  it("logs Telegram's refusal with its reason, never the token", async () => {
    fetchMock.mockResolvedValue(
      Response.json({ ok: false, description: "Bad Request: chat not found" }, { status: 400 })
    )
    await notifyAdmin("a@example.com")
    expect(console.error).toHaveBeenCalledWith(
      "waitlist admin ping refused:",
      400,
      "Bad Request: chat not found"
    )
    expect(logged()).not.toContain("test-token")
  })

  it("logs a refusal whose body is not Telegram's by status alone", async () => {
    fetchMock.mockResolvedValue(new Response("<html>bad gateway</html>", { status: 502 }))
    await notifyAdmin("a@example.com")
    expect(console.error).toHaveBeenCalledWith("waitlist admin ping refused:", 502, "(no description)")
  })

  it("logs a send that never got an answer by the error's name and code, never its message", async () => {
    fetchMock.mockRejectedValue(
      Object.assign(new TypeError("fetch failed: https://api.telegram.org/bottest-token/sendMessage"), {
        cause: { code: "ENOTFOUND" },
      })
    )
    await expect(notifyAdmin("a@example.com")).resolves.toBeUndefined()
    expect(console.error).toHaveBeenCalledWith("waitlist admin ping failed:", "TypeError", "ENOTFOUND")
    expect(logged()).not.toContain("test-token")
  })

  it("logs a timed-out send by name", async () => {
    fetchMock.mockRejectedValue(new DOMException("The operation was aborted due to timeout", "TimeoutError"))
    await notifyAdmin("a@example.com")
    expect(console.error).toHaveBeenCalledWith("waitlist admin ping failed:", "TimeoutError")
  })

  it.each([
    ["TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"],
    ["ADMIN_TELEGRAM_CHAT_ID", "ADMIN_TELEGRAM_CHAT_ID"],
  ])("names the missing setting %s and sends nothing", async (unset, named) => {
    vi.stubEnv(unset, "")
    await notifyAdmin("a@example.com")
    expect(fetchMock).not.toHaveBeenCalled()
    expect(console.warn).toHaveBeenCalledWith("waitlist admin ping skipped: not set:", named)
  })

  it("names both settings when neither is set", async () => {
    vi.stubEnv("TELEGRAM_BOT_TOKEN", "")
    vi.stubEnv("ADMIN_TELEGRAM_CHAT_ID", "")
    await notifyAdmin("a@example.com")
    expect(console.warn).toHaveBeenCalledWith(
      "waitlist admin ping skipped: not set:",
      "TELEGRAM_BOT_TOKEN, ADMIN_TELEGRAM_CHAT_ID"
    )
  })
})
