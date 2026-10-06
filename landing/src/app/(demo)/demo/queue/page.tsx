import { DemoQueue } from "@/components/demo/demo-queue";
import { DemoVisit } from "@/components/demo/demo-visit";

export default function DemoQueuePage() {
  return (
    <>
      <DemoVisit page="queue" />
      <DemoQueue />
    </>
  );
}
