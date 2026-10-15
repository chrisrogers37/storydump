import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/** An `aria-describedby` from the ids of the sentences on screen, or none. */
export function describedBy(...ids: (string | false | null)[]): string | undefined {
  const present = ids.filter((id): id is string => Boolean(id))
  return present.length > 0 ? present.join(" ") : undefined
}

/** The site's list style: "a, b and c", no serial comma. */
export function joinWithAnd(items: readonly string[]): string {
  return items.length < 2 ? items.join("") : `${items.slice(0, -1).join(", ")} and ${items.at(-1)}`
}
