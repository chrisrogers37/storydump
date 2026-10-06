/** The campaign tags the site keeps from a landing URL; every other query parameter is dropped. */
export const UTM_KEYS = ["utm_source", "utm_medium", "utm_campaign"] as const

export type UtmKey = (typeof UTM_KEYS)[number]

/** The campaign tags a query string carries, each one only if it has a value. */
export function utmFrom(params: URLSearchParams): Partial<Record<UtmKey, string>> {
  const utm: Partial<Record<UtmKey, string>> = {}
  for (const key of UTM_KEYS) {
    const value = params.get(key)
    if (value) utm[key] = value
  }
  return utm
}
