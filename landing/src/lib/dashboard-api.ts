/**
 * Client-side API helpers for dashboard components.
 * All calls go through the BFF proxy which handles auth injection.
 */

export async function postApi(path: string, body: Record<string, unknown> = {}) {
  const res = await fetch(`/api/dashboard/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: "Request failed" }));
    throw new Error(err.error || `API error ${res.status}`);
  }
  return res.json();
}

export async function getApi(path: string) {
  const res = await fetch(`/api/dashboard/${path}`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: "Request failed" }));
    throw new Error(err.error || `API error ${res.status}`);
  }
  return res.json();
}

/**
 * Fetch an OAuth URL for the given provider and open it in a new tab.
 * Throws if the backend doesn't return an auth URL.
 */
export async function openOAuthWindow(provider: string): Promise<void> {
  const data = await getApi(`oauth-url/${provider}`);
  if (!data?.auth_url) {
    throw new Error("No auth URL returned");
  }
  window.open(data.auth_url, "_blank", "noopener,noreferrer");
}
