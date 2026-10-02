import { HardDrive, RefreshCw, BarChart3, Users, Clock, Shield } from "lucide-react"
import {
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
} from "@/components/ui/card"

const features = [
  {
    icon: HardDrive,
    title: "Your Cloud, Your Content",
    description:
      "Your media library stays in your Google Drive. Storydump keeps a reference to each file, not the file.",
  },
  {
    icon: RefreshCw,
    title: "Smart Rotation",
    description:
      "Every piece of content gets its fair turn. The algorithm prioritizes unposted and least-posted media first.",
  },
  {
    icon: BarChart3,
    title: "Category Mixing",
    description:
      "Set ratios across content categories. Balance memes, product shots, and behind-the-scenes exactly how you want.",
  },
  {
    icon: Users,
    title: "Multi-Account",
    description:
      "Manage multiple Instagram accounts from a single setup. Switch between accounts in Telegram with one tap.",
  },
  {
    icon: Clock,
    title: "Flexible Scheduling",
    description:
      "Pick your posting window, frequency, and timing. Stories are spaced out with natural jitter built in.",
  },
  {
    icon: Shield,
    title: "Your content stays yours",
    description: "Storydump orchestrates — it never owns your content.",
  },
]

export function Features() {
  return (
    <section aria-labelledby="features-heading" className="bg-muted/50 py-16 md:py-24">
      <div className="mx-auto max-w-5xl px-4">
        <h2 id="features-heading" className="text-center text-3xl font-bold tracking-tight">
          Features
        </h2>
        <div className="mt-12 grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
          {features.map((feature) => (
            <Card
              key={feature.title}
              className="transition-shadow hover:shadow-md"
            >
              <CardHeader>
                <feature.icon className="mb-2 h-6 w-6 text-muted-foreground" />
                <CardTitle className="text-base">{feature.title}</CardTitle>
                <CardDescription>{feature.description}</CardDescription>
              </CardHeader>
            </Card>
          ))}
        </div>
      </div>
    </section>
  )
}
