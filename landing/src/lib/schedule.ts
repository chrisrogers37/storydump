/**
 * The posting schedule, said in words the card can show.
 *
 * `slotLabels` mirrors `fn_next_slot` (`059`): a window of `end - start`
 * hours (24 when they are equal) split into `postsPerDay` equal parts, the
 * first slot at the window start. The card shows these so "3 posts between
 * 2 PM and 2 AM" reads as "2 PM, 6 PM, 10 PM" — and so a workspace running
 * on UTC sees, in its own hours, why a slot passed when nothing arrived.
 */
/**
 * The posting window in hours, as `fn_next_slot` (`059`) reads it: `end - start`,
 * wrapping through midnight, and 24 when the two are equal.
 *
 * Not exported: `slotLabels` and `postingIntervalMinutes` are the two things
 * that answer with it, and the Calendar asks the second of those. The reason
 * it exists as its own function is that those two used to disagree — the card
 * recomputed the window as a bare `end - start` and guarded the result on
 * `> 0` (#1367). Both shapes this
 * handles fell through that guard — a wrap gives a negative, an equal pair
 * gives zero — so the card read "interval not set" for them. 14 → 2 is the
 * schema DEFAULT for a workspace, so that was what a new tenant saw on the one
 * page whose job is to say when posts go out.
 */
function windowHours(
  startHour: number | null,
  endHour: number | null,
): number | null {
  if (startHour === null || endHour === null) return null;
  return endHour === startHour ? 24 : (((endHour - startHour) % 24) + 24) % 24;
}

/**
 * The gap between consecutive slots, in minutes — the Calendar's "Posting
 * Rate". Null where the schedule has no answer, which is not the same as zero.
 */
export function postingIntervalMinutes(
  startHour: number | null,
  endHour: number | null,
  postsPerDay: number | null,
): number | null {
  const hours = windowHours(startHour, endHour);
  if (hours === null || !postsPerDay || postsPerDay < 1) return null;
  return Math.round((hours * 60) / postsPerDay);
}

export function slotLabels(
  startHour: number | null,
  endHour: number | null,
  postsPerDay: number | null,
): string[] {
  if (startHour === null || endHour === null || !postsPerDay || postsPerDay < 1)
    return [];
  const hours = windowHours(startHour, endHour)!;
  const labels: string[] = [];
  for (let k = 0; k < postsPerDay; k += 1) {
    const minutes = Math.round(
      (startHour * 60 + (k * hours * 60) / postsPerDay) % (24 * 60),
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
