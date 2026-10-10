import { Suspense } from "react";
import { DemoCalendar } from "@/components/demo/demo-calendar";

export default function DemoCalendarPage() {
  // The Calendar reads its open day from the query string. The boundary keeps
  // this page prerendered whatever the string is.
  return (
    <Suspense>
      <DemoCalendar />
    </Suspense>
  );
}
