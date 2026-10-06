import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { PageHeader } from "@/design/page-header";

export const metadata = {
  title: "Analytics",
};

export default function AnalyticsPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="Analytics"
        description="Detailed analytics and insights."
      />
      <Card>
        <CardHeader>
          <CardTitle>Coming soon</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            Detailed analytics with date range filtering, team performance, and
            content insights are planned for Phase 3. In the meantime, check the
            Overview page for key metrics.
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
