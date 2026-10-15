/**
 * A value with no source is stated as such, never drawn as an off switch.
 * The General tab says it for a setting it could not read, and the Approvals
 * card for a position.
 */
export function Unavailable() {
  return (
    <span className="text-sm text-muted-foreground">Not available yet</span>
  );
}
