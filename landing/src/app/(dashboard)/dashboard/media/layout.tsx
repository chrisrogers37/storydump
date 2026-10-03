import { MediaTabs } from "@/components/dashboard/media/media-tabs";
import { PageHeader } from "@/design/page-header";

export default function MediaLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-6">
      <PageHeader
        title="Media library"
        description="Browse and manage your content library."
      />
      <MediaTabs />
      {children}
    </div>
  );
}
