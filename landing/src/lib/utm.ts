/** The campaign tags the site keeps from a landing URL; every other query parameter is dropped. */
export const UTM_KEYS = ["utm_source", "utm_medium", "utm_campaign"] as const

export type UtmKey = (typeof UTM_KEYS)[number]
