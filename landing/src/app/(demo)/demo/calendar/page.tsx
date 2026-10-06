import { DemoCalendar } from "@/components/demo/demo-calendar";
import { DemoVisit } from "@/components/demo/demo-visit";

export default function DemoCalendarPage() {
  return (
    <>
      <DemoVisit page="calendar" />
      <DemoCalendar />
    </>
  );
}
