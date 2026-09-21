/**
 * How long a workspace name may be, in one place.
 *
 * `workspaces.name` is `VARCHAR(100) NOT NULL`, so 100 is the schema's
 * number and not a product preference. Three sites spelled it and a fourth
 * disagreed: the create form's `<input maxLength={120}>` let a person type
 * twenty characters the same form's own `tooLong` check then refused, and
 * the Settings rename input next to it already capped at 100.
 *
 * Modelled on `tokens.ts`'s `TOKEN_NAME_MAX` / `tokenNameValid`, and in its
 * own module for the same reason that one is: `lib/workspaces.ts` reaches
 * `next/headers` through `./session`, so a client component cannot import
 * from it.
 */
export const WORKSPACE_NAME_MAX = 100;

/** The shape check both the create route and the two forms apply. */
export function workspaceNameValid(name: string): boolean {
  const trimmed = name.trim();
  return trimmed.length > 0 && trimmed.length <= WORKSPACE_NAME_MAX;
}
