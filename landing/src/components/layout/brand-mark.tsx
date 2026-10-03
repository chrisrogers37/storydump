/** The wordmark's symbol: an orange ring around an ink dot, as in the site icon. */
export function BrandMark({ className = "size-5" }: { className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={`flex shrink-0 items-center justify-center rounded-full border-[3px] border-tap ${className}`}
    >
      <span className="size-[30%] rounded-full bg-ink" />
    </span>
  )
}
