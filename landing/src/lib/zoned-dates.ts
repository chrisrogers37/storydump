/**
 * Dates on a named clock, never on the clock the code happens to run on (#1511).
 *
 * The server renders in UTC and a visitor's browser in its own zone. A date
 * formatted "here" therefore reads differently in the two, and React rejects
 * the server's HTML (React error 418). A `YYYY-MM-DD` handed to `Date` is UTC
 * midnight, the day before anywhere west of Greenwich. So every date a screen
 * shows is read in an explicit zone: the workspace's, as the Queue's
 * `formatSlot` already does, so the server and the browser say the same thing.
 */

/** Postgres renders fractional seconds at any width; `Date` only promises three. */
function instant(value: string | Date): Date {
  return typeof value === "string"
    ? new Date(value.replace(/\.(\d{3})\d+/, ".$1"))
    : value;
}

/**
 * One formatter per zone and shape, for the process: constructing one costs
 * far more than a format call. A zone Intl does not know gets UTC rather
 * than a throw inside a render, and is remembered as having fallen back.
 */
const formatters = new Map<string, { format: Intl.DateTimeFormat; fellBack: boolean }>();

function formatter(tz: string, options: Intl.DateTimeFormatOptions) {
  const key = `${tz}|${JSON.stringify(options)}`;
  let entry = formatters.get(key);
  if (!entry) {
    try {
      entry = { format: new Intl.DateTimeFormat("en-US", { ...options, timeZone: tz }), fellBack: false };
    } catch {
      entry = { format: new Intl.DateTimeFormat("en-US", { ...options, timeZone: "UTC" }), fellBack: true };
    }
    formatters.set(key, entry);
  }
  return entry;
}

const DAY_PARTS: Intl.DateTimeFormatOptions = {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
};

/**
 * The day an instant falls on in `tz`, as `YYYY-MM-DD`: a key to compare and
 * group by, never shown, so an unknown zone silently means the UTC day.
 */
export function dateInZone(value: string | Date, tz: string): string {
  const parts = formatter(tz, DAY_PARTS).format.formatToParts(instant(value));
  const part = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find((p) => p.type === type)?.value ?? "";
  return `${part("year")}-${part("month")}-${part("day")}`;
}

const WALL_PARTS: Intl.DateTimeFormatOptions = {
  ...DAY_PARTS,
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
};

/**
 * An instant's wall time on `tz`'s clock as `YYYY-MM-DDTHH:MM`, the value a
 * `datetime-local` input takes. It seeds a time a person edits and the port
 * reads back in the same zone; an unknown zone means UTC, as above.
 */
export function wallTimeInZone(value: string | Date, tz: string): string {
  const parts = formatter(tz, WALL_PARTS).format.formatToParts(instant(value));
  const part = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find((p) => p.type === type)?.value ?? "";
  return `${part("year")}-${part("month")}-${part("day")}T${part("hour")}:${part("minute")}`;
}

/**
 * An instant, formatted on `tz`'s clock. A zone Intl does not know renders
 * in UTC and says so, as the Queue's `formatSlot` does.
 */
export function formatInZone(
  value: string | Date,
  tz: string,
  options: Intl.DateTimeFormatOptions,
): string {
  const { format, fellBack } = formatter(tz, options);
  const label = format.format(instant(value));
  return fellBack ? `${label} UTC` : label;
}

/**
 * A `YYYY-MM-DD` that already names a day (`posts_by_day.local_date` is the
 * workspace's own date), formatted as that day. Read as UTC midnight and
 * printed in UTC, it cannot shift into the day before.
 */
export function formatCalendarDate(
  localDate: string,
  options: Intl.DateTimeFormatOptions,
): string {
  return formatter("UTC", options).format.format(new Date(`${localDate}T00:00:00Z`));
}
