# ⚡ Autonomous AdOps Orchestrator (Agentic PPC Monitor)

**Executive Summary:** An enterprise-grade, headless asynchronous AI orchestration system designed to monitor, analyze, and route alerts for large-scale B2B and e-commerce advertising portfolios. Currently managing ~5,000 active campaigns across Meta and Google Ads, this system leverages Anthropic's Claude to transition media buying from reactive dashboard-checking to proactive, AI-driven exception management.

---

## 🏗️ System Architecture & Data Flow

As an Architect-designed system, the orchestrator strictly separates the data ingestion, reasoning engine, and presentation layer.

```mermaid
graph TD
    subgraph Data Ingestion
        A[Meta Ads API] -->|Raw Metrics| C(Monitoring Engine)
        B[Google Ads API] -->|Raw Metrics| C
    end
    
    subgraph Core Engine & Reasoning
        C -->|State Check| D[(Supabase Relational DB)]
        C -->|Anomalies| E{Routing & Logic}
        E -->|Payload| F[Anthropic Claude API]
        F -->|Agentic Synthesis| E
    end
    
    subgraph Presentation Layer 
        E -->|Alerts / Summaries| G[Discord Interface - Current MVP]
        E -.->|Upcoming| H[Slack Enterprise Grid]
        E -.->|Upcoming| I[MS Teams Integration]
    end(Note: The core architecture is completely headless. The current Discord integration operates purely as a low-latency presentation and command layer. The decoupled routing module allows plug-and-play integrations for enterprise communication platforms like Slack or Teams.)

💼 The Business Case & ROI
Managing thousands of ad campaigns manually leads to human error, budget bleed, and alert fatigue. This system enforces a Zero-Waste AdOps Pipeline:

Intelligent Fault-Isolation: Partial API failures from Meta/Google do not halt the entire pipeline. Healthy accounts are processed; failing accounts are explicitly flagged.

Algorithmic Alert Routing: Mitigates alert fatigue via deduplication, strict quiet-hour enforcement (no critical alerts outside business hours), and multi-tenant user routing.

Agentic Insights: Replaces manual data-pulling with Claude-powered anomaly detection (e.g., sudden ROAS drops, zero-conversion budget burn) on a scheduled Cron basis.

🧠 Core Engineering Principles
Cascading KPI Inheritance: Account-level targets automatically trickle down to thousands of campaigns, with localized overrides permitted for specialized ad sets. Null-safe evaluation ensures only explicitly defined KPIs trigger anomalies.

Strict Pagination & Rate Limit Handling: Custom paginators engineered to stay under UI presentation limits (e.g., 4096-character embeds) and PostgREST's silent 1000-row truncation, ensuring 100% data fidelity for high-volume accounts.

Read/Write Segregation: Immediate, ad-hoc summary requests (Read) are intentionally isolated from the hourly cron cycle (Write) to prevent alert duplication and database locking.

⚙️ Data Model (Simplified)
The application is built on a robust, multi-tenant relational data model, executed via a containerized Python worker.

clients / ad_accounts / campaigns: Strict hierarchical multi-tenancy.

assignments / account_assignments: RBAC (Role-Based Access Control) matrix determining alert visibility.

campaign_kpis / ad_account_kpis: Nullable metric boundaries (ROAS, CPA, Spend, CTR).

audit_log: Immutable ledger for all administrative configuration changes.

🚀 Deployment & Safety
This system is currently deployed in a production environment.

Execution: Containerized worker running on Railway.

Environment: Relies strictly on injected .env secrets. Zero hardcoded tokens.

(Note: Local Google Ads SDK testing requires Python 3.12 compatibility.)

🛠️ Local Development Setup
Clone the repository and enter the directory:
git clone <repo-url>
cd ppc-monitor

Create a virtual environment and install dependencies:
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

Copy the environment template and configure your secrets:
cp .env.example .env

Apply Supabase database migrations (see supabase/migrations/), then start the orchestrator:
python -m src.bot.main

📂 Project Structure
src/bot/            # Presentation layer (Discord interface & slash commands)

src/integrations/   # External API clients (Meta, Google, ClickUp, Claude)

src/monitoring/     # Core monitoring engine and anomaly detection

src/routing/        # Alert targeting and destination routing logic

src/storage/        # Supabase database communication layer (CRUD)

src/utils/          # Global helpers (time management, quiet hours, logging)

supabase/migrations/# SQL schema migrations

scripts/            # Maintenance and one-off execution scripts

tests/              # Test suite (Unit & Integration)

License: Proprietary — PlanSmart. All rights reserved.