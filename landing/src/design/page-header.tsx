import { cn } from "@/lib/utils"

/** A page's H1 in the display face, with an optional line under it. */
export function PageHeader({
  title,
  description,
  size = "md",
  align = "start",
}: {
  title: React.ReactNode
  description?: React.ReactNode
  size?: "md" | "lg"
  align?: "start" | "center"
}) {
  return (
    <div className={cn(align === "center" && "text-center")}>
      <h1 className={cn("page-title", size === "lg" ? "text-4xl" : "text-3xl")}>{title}</h1>
      {description && (
        <p className={cn("text-muted-foreground", size === "lg" ? "mt-3" : "mt-2 text-sm")}>
          {description}
        </p>
      )}
    </div>
  )
}
