#!/usr/bin/env bash
# =============================================================================
# Lean preview deploy for the Project Status Dashboard.
# Single Cloud Run service, source-based build (no Dockerfile push pipeline).
# Pinned to exactly one always-warm instance (min=max=1) so the in-memory OTP
# store survives between /auth/request and /auth/verify (durable fix: Redis, #20).
#
# Secrets are read from the local .env at deploy time and passed to Cloud Run
# as environment variables — nothing secret is stored in this script or in git.
#
# Prereqs: `gcloud auth login` done; billing linked to the project.
# Usage:   ./deploy-preview.sh
# =============================================================================
set -euo pipefail

PROJECT_ID="topsis-client-portal"
REGION="us-central1"
SERVICE="client-portal-preview"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -f .env ]; then
  echo "ERROR: .env not found in $SCRIPT_DIR — cannot source secrets." >&2
  exit 1
fi

# Build an env-vars file from .env (parsed safely — values may contain spaces).
# gcloud reads this YAML; nothing secret is echoed to the console or stored in git.
ENV_FILE="$(mktemp -t preview-env.XXXXXX.yaml)"
trap 'rm -f "$ENV_FILE"' EXIT
python3 - "$ENV_FILE" <<'PY'
import sys, json
from dotenv import dotenv_values
env = dotenv_values(".env")
defaults = {
    "SMTP_HOST": "smtp-relay.gmail.com", "SMTP_PORT": "587",
    "EMAIL_FROM": "info@topsisconsulting.com", "EMAIL_FROM_NAME": "Topsis Consulting",
    "SF_LOGIN_URL": "https://login.salesforce.com", "SF_API_VERSION": "v60.0",
}
keys = ["SESSION_SECRET", "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD",
        "EMAIL_FROM", "EMAIL_FROM_NAME", "JIRA_URL", "JIRA_USER_EMAIL", "JIRA_API_TOKEN",
        "SF_LOGIN_URL", "SF_API_VERSION", "SF_INSTANCE_URL", "SF_ACCESS_TOKEN"]
out = {"DEV_MODE": "false"}
for k in keys:
    out[k] = str(env.get(k) or defaults.get(k, ""))
# JSON is valid YAML; json.dump handles all quoting/escaping for gcloud.
with open(sys.argv[1], "w") as f:
    json.dump(out, f)
print("env-vars file written with keys:", ", ".join(out.keys()))
PY

echo "Deploying $SERVICE to project $PROJECT_ID ($REGION)…"
gcloud config set project "$PROJECT_ID" >/dev/null

gcloud run deploy "$SERVICE" \
  --source . \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --platform managed \
  --allow-unauthenticated \
  --port 8080 \
  --memory 512Mi \
  --cpu 1 \
  --min-instances 1 \
  --max-instances 1 \
  --env-vars-file "$ENV_FILE"

URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --project "$PROJECT_ID" --format="value(status.url)")
echo ""
echo "✓ Preview live: $URL"
echo "  Sign in at:   $URL/  (OTP emailed to a topsisconsulting.com address)"
