import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

describe("notifyAdmin", () => {
  const fetchMock = vi.fn()

  beforeEach(() => {
    vi.resetModules()
    vi.stubEnv("TELEGRAM_BOT_TOKEN", "test-token")
    vi.stubEnv("ADMIN_TELEGRAM_CHAT_ID", "test-chat")
    vi.stubGlobal("fetch", fetchMock)
    fetchMock.mockReset()
    vi.spyOn(console, "error").mockImplementation(() => {})
  })

  afterEach(() => {
    vi.unstubAllEnvs()
    vi.unstubAllGlobals()
  })

  it("sends plain text with no link preview, so an address cannot format the message", async () => {
    fetchMock.mockResolvedValue(new Response("{}", { status: 200 }))
    const { notifyAdmin } = await import("./telegram")
    await notifyAdmin("*bold*@example.com")
    const sent = JSON.parse(fetchMock.mock.calls[0][1].body)
    expect(sent).not.toHaveProperty("parse_mode")
    expect(sent.link_preview_options).toEqual({ is_disabled: true })
    expect(sent.text).toContain("*bold*@example.com")
    expect(console.error).not.toHaveBeenCalled()
  })

  it("logs Telegram's refusal by status, never the token", async () => {
    fetchMock.mockResolvedValue(new Response("{}", { status: 429 }))
    const { notifyAdmin } = await import("./telegram")
    await notifyAdmin("a@example.com")
    expect(console.error).toHaveBeenCalledWith("waitlist admin ping refused:", 429)
    expect(JSON.stringify(vi.mocked(console.error).mock.calls)).not.toContain("test-token")
  })
})
