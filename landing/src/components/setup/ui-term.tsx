/** A button, tab or screen name the reader will see in the app, set apart from the prose. */
export function UiTerm({ children }: { children: React.ReactNode }) {
  return <span className="font-medium text-foreground">{children}</span>
}
