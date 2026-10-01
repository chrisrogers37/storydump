export const faqs = [
  {
    question: "What is Storydump?",
    answer:
      "Storydump is an Instagram Story scheduling tool that automatically rotates your content library. Connect your Google Drive, set a posting schedule, and approve each story in the dashboard or in Telegram before it goes live.",
  },
  {
    question: "Do I need Telegram?",
    answer:
      "No. You sign in with Google and do everything from the web dashboard, including deciding on each story in the Queue. If you also link the Storydump Telegram bot, each story arrives in your chat as a card with one-tap actions.",
  },
  {
    question: "Do I need to give you my Instagram password?",
    answer:
      "No. Storydump uses the official Instagram Graph API with OAuth, so you authenticate directly with Meta. We never see or store your Instagram password.",
  },
  {
    question: "What content types are supported?",
    answer:
      "Images and videos from the Google Drive folders you connect. Before a story posts, Storydump frames it to Instagram's 9:16 story format without cropping. An image can be up to 8 MB and a video up to 40 MB.",
  },
  {
    question: "Can I manage multiple Instagram accounts?",
    answer:
      "Yes. Add each one in the dashboard under Settings › Accounts with Connect Instagram. Each account maintains its own posting schedule and content rotation.",
  },
  {
    question: "What happens if I skip or reject a story?",
    answer:
      "Skipped stories go back in the queue for later. Rejected stories are permanently excluded so they never come up again. You have full control over what gets posted.",
  },
  {
    question: "Is my content stored on your servers?",
    answer:
      "Not permanently. Your media stays in your Google Drive. To post a story, Storydump uploads a copy of the file to its media processor, which frames it for Instagram; the copy is deleted as soon as the story posts or is cancelled, and any copy left over is deleted once it is 48 hours old. What we keep is a reference to each file (its Drive ID, name, type, folder and checksum), not the file.",
  },
  {
    question: "Who built this?",
    answer:
      "Storydump is built by Chris Rogers. Have questions or feedback? Reach out at christophertrogers37@gmail.com.",
  },
]
