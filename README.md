# Storydump - Instagram Story Automation System

A self-hosted Instagram Story scheduling system with Telegram-based team collaboration.

## Features

- 🏢 **Multi-Tenant / Multi-Instance**: One deployment serves many independent teams, each scoped to its own Telegram group, media, queue, and schedule
- 🖥️ **Web Dashboard**: Next.js dashboard with an instance picker for managing multiple accounts
- 📅 **Smart Scheduling**: Intelligent posting schedule based on your preferences
- 📁 **Category-Based Scheduling**: Organize media by folder (memes/, merch/) with configurable ratios
- 📱 **Telegram Integration**: Team collaboration via Telegram bot with lifecycle notifications
- 🔄 **Phased Approach**: Start with manual posting, optionally enable automation
- 🔒 **TTL Locks**: Prevent premature reposts with 30-day locks
- 🚫 **Permanent Reject**: Permanently block unwanted media from ever being queued
- 📊 **Full Audit Trail**: Track who posted what and when
- 🎨 **Image Validation**: Automatic validation against Instagram requirements
- 📱 **Instagram Deep Links**: One-tap button to open Instagram app/web
- ✨ **Enhanced Captions**: Clean workflow instructions with actionable steps

## Quick Start

### 1. Installation

```bash
# Clone repository
git clone <your-repo-url>
cd storydump

# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Install CLI tool
pip install -e .
```

### 2. Configuration

```bash
# Create .env file with your credentials
nano .env
```

Add the following required variables to your `.env` file:

Required configuration:
- `TELEGRAM_BOT_TOKEN`: Get from @BotFather on Telegram
- `TELEGRAM_CHANNEL_ID`: Your Telegram channel ID
- `ADMIN_TELEGRAM_CHAT_ID`: Your personal chat ID for alerts
- `DB_PASSWORD`: PostgreSQL password (optional for local development)

### 3. Database Setup

```bash
# Create database
createdb storydump

# Run schema setup
psql -U postgres -d storydump -f scripts/setup_database.sql

# Or use Python script
python scripts/init_db.py
```

### 4. Index Your Media

```bash
# Index media files
storydump-cli index-media /path/to/media/stories

# List indexed media
storydump-cli list-media --limit 20
```

### 5. Create Schedule

```bash
# Create 7-day posting schedule
storydump-cli create-schedule --days 7

# View queue
storydump-cli list-queue
```

### 6. Run the Application

```bash
# Run in foreground (for testing)
python -m src.main

# Or run as background service (see documentation)
```

## CLI Commands

### Media Management

```bash
# Index media from directory
storydump-cli index-media /path/to/media

# List all media items
storydump-cli list-media --limit 50 --active-only

# Validate image
storydump-cli validate-image /path/to/image.jpg
```

### Queue Management

```bash
# Create posting schedule (uses category ratios)
storydump-cli create-schedule --days 7

# Process pending posts
storydump-cli process-queue

# Force process next post (development testing)
storydump-cli process-queue --force

# View queue
storydump-cli list-queue

# Reset queue (clear all pending posts)
storydump-cli reset-queue
```

### Category Management

```bash
# List categories and their posting ratios
storydump-cli list-categories

# Update category posting ratios (interactive prompts)
storydump-cli update-category-mix

# View ratio history (Type 2 SCD)
storydump-cli category-mix-history --limit 10
```

### User Management

```bash
# List users
storydump-cli list-users

# Promote user to admin
storydump-cli promote-user <telegram_user_id> --role admin
```

### Health Check

```bash
# Check system health
storydump-cli check-health
```

## Telegram Bot Commands

The bot responds to these commands in Telegram:

### Core Commands
- `/start` - Initialize bot and show welcome message
- `/status` - Show system health and queue status
- `/help` - Show all available commands

### Queue Management
- `/queue` - View pending scheduled posts
- `/next` - Force-send next scheduled post immediately
- `/schedule [N]` - Create N days of posting schedule (default: 7)
- `/reset` - Reset posting queue to empty (with confirmation)

### Operational Control
- `/pause` - Pause automatic posting
- `/resume` - Resume posting (with smart overdue handling)
- `/cleanup` - Delete recent bot messages from chat

### Information
- `/stats` - Show media library statistics
- `/history [N]` - Show last N posts (default: 5)
- `/locks` - View permanently rejected items

## Architecture

**Phase 1** (Telegram-Only Mode) - ✅ COMPLETE (v1.0.1):
- ✅ Smart scheduling + Telegram notifications
- ✅ Team posts manually to Instagram
- ✅ No Instagram API needed
- ✅ 147 comprehensive tests
- ✅ Production-tested and deployed

**Phase 1.5** (Telegram Enhancements) - ✅ COMPLETE (v1.3.0):
- ✅ Permanent Reject button for unwanted media (infinite locks)
- ✅ Bot lifecycle notifications (startup/shutdown with system status)
- ✅ Instagram deep links (one-tap Instagram app opening)
- ✅ Enhanced captions with workflow instructions
- ✅ 3-button layout: Posted, Skip, Reject
- ✅ 7 new bot commands: `/pause`, `/resume`, `/schedule`, `/stats`, `/history`, `/locks`, `/reset`
- ✅ Smart overdue handling when resuming after pause

**Phase 1.6** (Category Scheduling) - ✅ COMPLETE (v1.4.0):
- ✅ Category-based media organization (folder structure → category)
- ✅ Configurable posting ratios per category (e.g., 70% memes, 30% merch)
- ✅ Type 2 SCD tracking for ratio history
- ✅ Interactive ratio configuration during indexing
- ✅ Scheduler integration with category-aware slot allocation
- ✅ 488 comprehensive tests

**Phase 2** (Instagram API Automation) - ✅ COMPLETE (v1.5.0):
- ✅ Instagram Graph API integration with rate limiting
- ✅ Cloudinary media hosting with TTL expiration
- ✅ Encrypted token management with auto-refresh
- ✅ Multi-account support (add/switch/deactivate via Telegram)
- ✅ Hybrid mode: auto-post via API, fallback to Telegram on errors
- ✅ Per-chat settings stored in database
- ✅ "🤖 Auto Post to Instagram" button when API enabled

**Phase 1.8** (Telegram UX Improvements) - ✅ COMPLETE:
- ✅ Native Telegram command menu (autocomplete with descriptions)
- ✅ `/cleanup` command to delete recent bot messages
- ✅ `/reset` command to clear posting queue (renamed from `/clear`)
- ✅ Message tracking (100-message cache) for efficient cleanup
- ✅ TelegramService refactored from 3,500-line monolith into 5 handler modules
- ✅ Verbose settings expansion (controls more message types)
- ✅ 2,038 tests across 103 files (grown well past the v1.6.0-era count below — see [TEST_COVERAGE.md](documentation/guides/TEST_COVERAGE.md))

**Multi-Tenant / Multi-Instance Rearchitecture** - ✅ SHIPPED (post-v1.6.0, not yet version-tagged):

Storydump moved from "one self-hosted deployment per team" to a multi-tenant model: a single deployment now serves many independent teams ("Instances"), each scoped to its own Telegram group, media, queue, and schedule. See `PROJECT_MISSION.md` for the current product mental model (User → Instances → accounts/media/queue).

- ✅ `user_chat_memberships` table linking Telegram users to the instances (chat_settings rows) they belong to
- ✅ 5-branch `/start` handler (`StartCommandRouter`) — DM onboarding for new users, returning-user instance list, group linking, and unchanged existing group setup
- ✅ Web dashboard (`landing/`, Next.js on Vercel) with an instance picker and switcher — see [documentation/guides/landing-vercel-deployment.md](documentation/guides/landing-vercel-deployment.md)
- ✅ Mini App instance picker for DM-launched sessions
- ⚠️ **2026-07 security note:** a cross-tenant data-isolation gap in the onboarding/dashboard API and Telegram queue callbacks was found and fixed (see `documentation/SECURITY_REVIEW.md` §10 addendum). Role-based (member vs. admin) command authorization for multi-tenant is tracked as a follow-up, not yet implemented.

**Phase 3+ (Shopify, Printify, LLM integration, order/email automation)** - 📋 PENDING — see [documentation/planning/phases/00_MASTER_ROADMAP.md](documentation/planning/phases/00_MASTER_ROADMAP.md) for the full phase breakdown and current status.

## Development

### Running Tests

The project includes 2,038 tests (103 files) with automatic test database setup:

```bash
# Run all tests with coverage
pytest --cov=src --cov-report=html

# Run only unit tests
pytest -m unit

# Run only integration tests
pytest -m integration

# Force process next post (development testing)
storydump-cli process-queue --force
```

### Project Structure

```
storydump/
├── src/                    # Main application code (Python/FastAPI)
│   ├── api/               # REST API + Mini App onboarding routes
│   ├── config/            # Configuration management
│   ├── models/            # Database models
│   ├── repositories/      # Data access layer
│   ├── services/          # Business logic
│   │   ├── core/          # Telegram bot, posting, scheduling, dashboard, membership
│   │   ├── integrations/  # Instagram Graph API, Google Drive, Cloudinary
│   │   └── media_sources/ # Pluggable media source providers
│   ├── utils/             # Utility functions
│   └── main.py            # Application entry point (worker: bot + scheduler)
├── landing/               # Next.js web dashboard + marketing site (Vercel)
├── cli/                   # CLI commands
├── tests/                 # Test suite (100+ files)
├── scripts/               # Database scripts + migrations
└── media/                 # Local media storage (dev only — prod uses Google Drive)
    └── stories/           # Instagram stories
        ├── memes/         # Meme content
        └── merch/         # Merchandise content
```

## Documentation

📚 **[Complete Documentation Index](documentation/README.md)**

Key resources:
- **[Quick Start Guide](documentation/guides/quickstart.md)** - Get running in 10 minutes
- **[Deployment Guide](documentation/guides/deployment.md)** - Production deployment checklist
- **[Testing Guide](documentation/guides/testing-guide.md)** - How to run and write tests
- **[Master Roadmap](documentation/planning/phases/00_MASTER_ROADMAP.md)** - Architecture, phase status, and forward plan
- **[Project Mission](PROJECT_MISSION.md)** - Product vision and multi-instance mental model
- **[Developer Guide](CLAUDE.md)** - Development guidelines and architecture

## License

MIT License - see LICENSE file for details

## Support

For issues and questions, please open a GitHub issue.
