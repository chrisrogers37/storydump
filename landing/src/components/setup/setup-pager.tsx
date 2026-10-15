/** The back and next links at the foot of a setup guide. Stacked on a phone,
 *  in reading and Tab order, so a long next button never pushes the page
 *  sideways. */
export function SetupPager({ children }: { children: React.ReactNode }) {
  return (
    <div className="mt-12 flex flex-col items-start gap-4 sm:flex-row sm:items-center sm:justify-between">
      {children}
    </div>
  )
}
