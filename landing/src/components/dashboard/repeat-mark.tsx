import { Repeat } from "lucide-react";
import { describeRepeat, type RepeatRule } from "@/lib/repeat";

/**
 * The mark a repeating story carries where it is drawn small (#485): an icon,
 * with its rule in words for a screen reader and on hover. The rule's time is
 * the story's own, so `zone` names the story's zone where the page reads
 * another. `icon` sizes the icon where it is drawn smaller still, as a
 * calendar chip draws it.
 */
export function RepeatMark({
  rule,
  zone,
  icon = "h-3.5 w-3.5",
}: {
  rule: RepeatRule;
  zone?: string;
  icon?: string;
}) {
  const words = `Repeats ${describeRepeat(rule)}${zone ? ` (${zone})` : ""}`;
  return (
    <span className="inline-flex shrink-0 text-muted-foreground" title={words}>
      <Repeat className={icon} aria-hidden />
      <span className="sr-only">{words}</span>
    </span>
  );
}
