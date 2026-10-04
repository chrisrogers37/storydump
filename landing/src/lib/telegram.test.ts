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

  /** Every log line, flattened, to show the token and the chat id are in none of them. */
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

  it.each([
    [
      "Telegram's refusal with its reason",
      () => Response.json({ ok: false, description: "Bad Request: chat not found" }, { status: 400 }),
      ["waitlist admin ping refused:", 400, "Bad Request: chat not found"],
    ],
    [
      "a refusal whose body is not Telegram's by status alone",
      () => new Response("<html>bad gateway</html>", { status: 502 }),
      ["waitlist admin ping refused:", 502, "(no description)"],
    ],
    [
      "a long reason cut to 200 characters",
      () => Response.json({ ok: false, description: "x".repeat(500) }, { status: 400 }),
      ["waitlist admin ping refused:", 400, "x".repeat(200)],
    ],
  ])("logs %s, never the token", async (_, response, logs) => {
    fetchMock.mockResolvedValue(response())
    await notifyAdmin("a@example.com")
    expect(console.error).toHaveBeenCalledWith(...logs)
    expect(logged()).not.toMatch(/test-token|test-chat/)
  })

  it.each([
    [
      "a send that never got an answer by the error's name and code, never its message",
      Object.assign(
        new TypeError("fetch failed: https://api.telegram.org/bottest-token/sendMessage"),
        { cause: { code: "ENOTFOUND" } }
      ),
      ["waitlist admin ping failed:", "TypeError", "ENOTFOUND"],
    ],
    [
      "a timed-out send by name",
      new DOMException("The operation was aborted due to timeout", "TimeoutError"),
      ["waitlist admin ping failed:", "TimeoutError"],
    ],
    ["a rejection that is not an Error by its type", null, ["waitlist admin ping failed:", "object"]],
  ])("logs %s", async (_, error, logs) => {
    fetchMock.mockRejectedValue(error)
    await expect(notifyAdmin("a@example.com")).resolves.toBeUndefined()
    expect(console.error).toHaveBeenCalledWith(...logs)
    expect(logged()).not.toMatch(/test-token|test-chat/)
  })

  it.each([
    [["TELEGRAM_BOT_TOKEN"], "TELEGRAM_BOT_TOKEN"],
    [["ADMIN_TELEGRAM_CHAT_ID"], "ADMIN_TELEGRAM_CHAT_ID"],
    [["TELEGRAM_BOT_TOKEN", "ADMIN_TELEGRAM_CHAT_ID"], "TELEGRAM_BOT_TOKEN, ADMIN_TELEGRAM_CHAT_ID"],
    // A blank pasted value counts as not set.
    [["TELEGRAM_BOT_TOKEN"], "TELEGRAM_BOT_TOKEN", " \n"],
    [["ADMIN_TELEGRAM_CHAT_ID"], "ADMIN_TELEGRAM_CHAT_ID", " \n"],
  ])("names the missing setting %j and sends nothing", async (unset, named, value = "") => {
    for (const name of unset) vi.stubEnv(name, value)
    await notifyAdmin("a@example.com")
    expect(fetchMock).not.toHaveBeenCalled()
    expect(console.warn).toHaveBeenCalledWith("waitlist admin ping skipped: not set:", named)
  })
})
