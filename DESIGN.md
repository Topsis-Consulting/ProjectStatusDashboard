# Project Status Dashboard — Design Decisions

**Project:** Multi-Tenant Passwordless Client Portal  
**Domain:** project.topsisconsulting.com  
**Last updated:** 2026-06-14

---

## Email Delivery: Google Workspace SMTP Relay

**Decision:** Use Google Workspace's built-in SMTP Relay service to send OTP emails.

**Sender address:** `info@topsisconsulting.com`  
**Relay host:** `smtp-relay.gmail.com:587` (TLS)

**Why this approach:**
- `info@topsisconsulting.com` exists as a Google Group — it cannot generate App Passwords, so direct SMTP auth against that mailbox is not possible.
- Google Workspace includes SMTP Relay at no additional cost — no third-party service (SendGrid, Postmark) required.
- Emails appear to recipients as coming from `info@topsisconsulting.com` with display name "Topsis Consulting".
- Sufficient for OTP volume (2,000 emails/day relay limit far exceeds expected usage).

**Setup required (one-time, in Google Admin console):**
1. Google Admin → Apps → Google Workspace → Gmail → Routing → SMTP relay service
2. Add relay rule:
   - Allowed senders: Only addresses in my domains
   - Authentication: Require SMTP Authentication
   - Encryption: Require TLS
   - Restrict to IP ranges if Cloud Run static IP is configured (optional but recommended)
3. In the app, authenticate via OAuth2 or an authorized user's App Password scoped to send-only.

**Authentication:** Justin's Google Workspace account (`jp@topsisconsulting.com`) authenticates the SMTP relay via an App Password. One-time setup — credentials stored in Google Secret Manager, no recurring manual steps. A dedicated service user was considered but ruled out to avoid an additional Workspace license cost.

---

## Authentication: Passwordless Email OTP

**Decision:** 6-digit numeric OTP, 15-minute expiration, single-use.

- OTP stored in Redis (Google Memorystore in production, local Redis in dev)
- Token deleted immediately upon successful validation (prevents reuse)
- Generic error message if domain not in tenant registry (prevents email enumeration)
- Session bound to verified email + domain in a signed, HttpOnly, Secure, SameSite=Strict cookie

---

## Multi-Tenancy: Domain-Based Routing

**Decision:** Tenant identity derived from the authenticated email domain, never from client input.

- Tenant registry stored as `registry.json` inside the deployment package (static config)
- Each tenant entry maps: email domain → Jira project key, display name, logo URL
- All Jira API queries are server-enforced to the authenticated tenant's project key
- Adding a new client = appending one JSON entry + redeploying (< 60 seconds)

---

## Infrastructure

| Component | Choice | Reason |
|---|---|---|
| Compute | Google Cloud Run | Serverless, scales to zero, low cost |
| OTP cache | Google Memorystore (Redis) | Fast TTL-based expiry, VPC-only access |
| Secrets | Google Secret Manager | Injected at deploy time, not in source |
| CI/CD | Google Cloud Build | Triggers on push to main |
| Email | Google Workspace SMTP Relay | Already included in Workspace subscription |
| DNS | Squarespace → Cloud Run domain mapping | One-time CNAME setup |

---

## Frontend

- Vanilla HTML + Tailwind CSS (CDN) — no build step required
- Split-screen login: brand panel (left) + OTP form (right)
- Dashboard shows: Go-Live date/countdown, open issue count, in-progress count, high-priority count, issues list, recent activity feed
- Go-Live date sourced from a Jira ticket named "Go Live" if one exists in the tenant's project
- Brand color: `#f75b3c`
