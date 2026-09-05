/**
 * The posting schedule, said in words the card can show.
 *
 * `slotLabels` mirrors `fn_next_slot` (`059`): a window of `end - start`
 * hours (24 when they are equal) split into `postsPerDay` equal parts, the
 * first slot at the window start. The card shows these so "3 posts between
 * 2 PM and 2 AM" reads as "2 PM, 6 PM, 10 PM" — and so a workspace running
 * on UTC sees, in its own hours, why a slot passed when nothing arrived.
 */
export function slotLabels(
  startHour: number | null,
  endHour: number | null,
  postsPerDay: number | null,
): string[] {
  if (startHour === null || endHour === null || !postsPerDay || postsPerDay < 1)
    return [];
  const windowHours =
    endHour === startHour ? 24 : (((endHour - startHour) % 24) + 24) % 24;
  const labels: string[] = [];
  for (let k = 0; k < postsPerDay; k += 1) {
    const minutes = Math.round(
      (startHour * 60 + (k * windowHours * 60) / postsPerDay) % (24 * 60),
    );
    labels.push(formatMinutes(minutes));
  }
  return labels;
}

function formatMinutes(minutesOfDay: number): string {
  const h24 = Math.floor(minutesOfDay / 60);
  const m = minutesOfDay % 60;
  const h12 = h24 % 12 === 0 ? 12 : h24 % 12;
  const suffix = h24 < 12 ? "AM" : "PM";
  return `${h12}:${String(m).padStart(2, "0")} ${suffix}`;
}

const FALLBACK_ZONES = [
  "UTC",
  "America/New_York",
  "America/Chicago",
  "America/Denver",
  "America/Los_Angeles",
  "America/Phoenix",
  "America/Anchorage",
  "Pacific/Honolulu",
  "America/Toronto",
  "America/Vancouver",
  "America/Mexico_City",
  "America/Sao_Paulo",
  "Europe/London",
  "Europe/Paris",
  "Europe/Berlin",
  "Europe/Madrid",
  "Asia/Dubai",
  "Asia/Kolkata",
  "Asia/Singapore",
  "Asia/Tokyo",
  "Australia/Sydney",
];

/**
 * The IANA zones a person can pick from: the browser's full list where it
 * has one (`Intl.supportedValuesOf`), else a short one — plus UTC and the
 * saved value, so a stored zone the list lacks still shows as what it is.
 */
export function timeZoneOptions(current: string | null): string[] {
  let known: string[] = FALLBACK_ZONES;
  try {
    const intl = Intl as unknown as {
      supportedValuesOf?: (key: string) => string[];
    };
    const fromBrowser = intl.supportedValuesOf?.("timeZone");
    if (Array.isArray(fromBrowser) && fromBrowser.length > 0)
      known = fromBrowser;
  } catch {
    // An older runtime: the short list stands.
  }
  const all = new Set<string>(known);
  all.add("UTC");
  if (current) all.add(current);
  return [...all].sort();
}
