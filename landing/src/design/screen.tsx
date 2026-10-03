import { cn } from "@/lib/utils"

/**
 * A standalone page outside the dashboard (sign-in, welcome, workspaces, an
 * invitation, a sign-in error): one column on paper. `align="top"` for a page
 * whose length varies; centered otherwise.
 */
export function Screen({
  children,
  width = "sm",
  align = "center",
  className,
}: {
  children: React.ReactNode
  width?: "sm" | "md"
  align?: "center" | "top"
  className?: string
}) {
  return (
    <main
      className={cn(
        "flex min-h-svh flex-col items-center bg-paper px-4 py-16",
        align === "center" && "justify-center",
      )}
    >
      <div className={cn("w-full space-y-6", width === "sm" ? "max-w-sm" : "max-w-md", className)}>
        {children}
      </div>
    </main>
  )
}
