#!/usr/bin/env bash
# =============================================================================
# Topsis Client Portal — GCP Project Bootstrap
# Run this once to stand up all infrastructure for ProjectStatusDashboard.
#
# Prerequisites:
#   - gcloud CLI installed and authenticated: gcloud auth login
#   - Billing account ID ready (find it: gcloud billing accounts list)
#   - GitHub repo already exists: Topsis-Consulting/ProjectStatusDashboard
#
# Usage:
#   chmod +x gcp-setup.sh
#   ./gcp-setup.sh
# =============================================================================

set -euo pipefail

# =============================================================================
# CONFIGURATION — edit these before running
# =============================================================================

PROJECT_ID="topsis-client-portal"
PROJECT_NAME="Topsis Client Portal"
REGION="us-central1"
BILLING_ACCOUNT=""          # e.g. "01ABCD-EF1234-567890" — run: gcloud billing accounts list
GITHUB_OWNER="Topsis-Consulting"
GITHUB_REPO="ProjectStatusDashboard"
SERVICE_NAME="client-portal"
ARTIFACT_REPO="client-portal"

# =============================================================================
# STEP 1 — Create GCP project and link billing
# =============================================================================

echo ""
echo "── Step 1: Create GCP project ──────────────────────────────────────────"

if [ -z "$BILLING_ACCOUNT" ]; then
  echo "ERROR: Set BILLING_ACCOUNT at the top of this script before running."
  echo "       Run 'gcloud billing accounts list' to find yours."
  exit 1
fi

gcloud projects create "$PROJECT_ID" \
  --name="$PROJECT_NAME" \
  --set-as-default

gcloud billing projects link "$PROJECT_ID" \
  --billing-account="$BILLING_ACCOUNT"

echo "✓ Project $PROJECT_ID created and billing linked."

# =============================================================================
# STEP 2 — Enable required APIs
# =============================================================================

echo ""
echo "── Step 2: Enable APIs ─────────────────────────────────────────────────"

gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  secretmanager.googleapis.com \
  redis.googleapis.com \
  vpcaccess.googleapis.com \
  compute.googleapis.com \
  --project="$PROJECT_ID"

echo "✓ APIs enabled."

# =============================================================================
# STEP 3 — Artifact Registry repository
# =============================================================================

echo ""
echo "── Step 3: Artifact Registry ───────────────────────────────────────────"

gcloud artifacts repositories create "$ARTIFACT_REPO" \
  --repository-format=docker \
  --location="$REGION" \
  --description="Client portal container images" \
  --project="$PROJECT_ID"

echo "✓ Artifact Registry repo: $REGION-docker.pkg.dev/$PROJECT_ID/$ARTIFACT_REPO"

# =============================================================================
# STEP 4 — VPC network + Serverless VPC Access connector
#          (required for Cloud Run → Memorystore/Redis)
# =============================================================================

echo ""
echo "── Step 4: VPC + Serverless VPC Connector ──────────────────────────────"

# Use the default VPC (already exists in every new project)
# Create a /28 subnet range for the connector
gcloud compute networks subnets create vpc-connector-subnet \
  --network=default \
  --region="$REGION" \
  --range=10.8.0.0/28 \
  --project="$PROJECT_ID" 2>/dev/null || echo "  (subnet already exists, continuing)"

gcloud compute networks vpc-access connectors create client-portal-connector \
  --region="$REGION" \
  --subnet=vpc-connector-subnet \
  --subnet-project="$PROJECT_ID" \
  --min-instances=2 \
  --max-instances=3 \
  --machine-type=f1-micro \
  --project="$PROJECT_ID"

echo "✓ VPC connector: client-portal-connector"

# =============================================================================
# STEP 5 — Memorystore (Redis) for OTP cache
#          Basic tier, 1GB — sufficient for OTP storage at this scale
# =============================================================================

echo ""
echo "── Step 5: Memorystore (Redis) ─────────────────────────────────────────"

gcloud redis instances create client-portal-redis \
  --size=1 \
  --region="$REGION" \
  --redis-version=redis_7_0 \
  --tier=basic \
  --network=default \
  --project="$PROJECT_ID"

# Capture the Redis IP for use in secrets below
REDIS_HOST=$(gcloud redis instances describe client-portal-redis \
  --region="$REGION" \
  --project="$PROJECT_ID" \
  --format="value(host)")

REDIS_PORT=$(gcloud redis instances describe client-portal-redis \
  --region="$REGION" \
  --project="$PROJECT_ID" \
  --format="value(port)")

echo "✓ Redis: $REDIS_HOST:$REDIS_PORT"

# =============================================================================
# STEP 6 — Secret Manager: create secret placeholders
#          You'll populate values in Step 7 (interactive)
# =============================================================================

echo ""
echo "── Step 6: Create Secret Manager secrets ───────────────────────────────"

SECRETS=(
  "PORTAL_SESSION_SECRET"       # ≥32 char random string for cookie signing
  "PORTAL_JIRA_URL"             # https://topsis.atlassian.net
  "PORTAL_JIRA_USER_EMAIL"      # Jira service account email
  "PORTAL_JIRA_API_TOKEN"       # Jira API token (read-only scope)
  "PORTAL_EMAIL_API_KEY"        # Postmark or SendGrid API key for OTP emails
  "PORTAL_REDIS_URL"            # redis://<host>:<port>  (auto-populated below)
  "PORTAL_TENANT_REGISTRY"      # JSON blob: { "domain": { epic_key, logo_url, ... } }
)

for SECRET in "${SECRETS[@]}"; do
  gcloud secrets create "$SECRET" \
    --replication-policy=automatic \
    --project="$PROJECT_ID" 2>/dev/null || echo "  (secret $SECRET already exists)"
done

# Auto-populate Redis URL (we know it from Step 5)
echo -n "redis://$REDIS_HOST:$REDIS_PORT" | \
  gcloud secrets versions add PORTAL_REDIS_URL \
    --data-file=- \
    --project="$PROJECT_ID"

echo "✓ Secrets created. Redis URL auto-populated."
echo ""
echo "  ⚠  You must now populate the remaining secrets before deploying:"
echo "     gcloud secrets versions add PORTAL_SESSION_SECRET --data-file=- --project=$PROJECT_ID"
echo "     gcloud secrets versions add PORTAL_JIRA_URL --data-file=- --project=$PROJECT_ID"
echo "     gcloud secrets versions add PORTAL_JIRA_USER_EMAIL --data-file=- --project=$PROJECT_ID"
echo "     gcloud secrets versions add PORTAL_JIRA_API_TOKEN --data-file=- --project=$PROJECT_ID"
echo "     gcloud secrets versions add PORTAL_EMAIL_API_KEY --data-file=- --project=$PROJECT_ID"
echo "     gcloud secrets versions add PORTAL_TENANT_REGISTRY --data-file=- --project=$PROJECT_ID"

# =============================================================================
# STEP 7 — IAM: Cloud Build service account permissions
# =============================================================================

echo ""
echo "── Step 7: IAM — Cloud Build service account ───────────────────────────"

PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")
CLOUDBUILD_SA="$PROJECT_NUMBER@cloudbuild.gserviceaccount.com"

# Cloud Build needs to deploy to Cloud Run and push to Artifact Registry
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$CLOUDBUILD_SA" \
  --role="roles/run.admin"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$CLOUDBUILD_SA" \
  --role="roles/artifactregistry.writer"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$CLOUDBUILD_SA" \
  --role="roles/iam.serviceAccountUser"

echo "✓ Cloud Build SA permissions granted."

# =============================================================================
# STEP 8 — IAM: Cloud Run service account (least-privilege)
# =============================================================================

echo ""
echo "── Step 8: IAM — Cloud Run service account ─────────────────────────────"

gcloud iam service-accounts create client-portal-sa \
  --display-name="Client Portal — Cloud Run SA" \
  --project="$PROJECT_ID"

PORTAL_SA="client-portal-sa@$PROJECT_ID.iam.gserviceaccount.com"

# Read secrets at runtime
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$PORTAL_SA" \
  --role="roles/secretmanager.secretAccessor"

echo "✓ Cloud Run SA created: $PORTAL_SA"

# =============================================================================
# STEP 9 — Cloud Build GitHub trigger
# =============================================================================

echo ""
echo "── Step 9: Cloud Build GitHub trigger ──────────────────────────────────"

echo ""
echo "  ⚠  GitHub trigger must be connected manually (one-time OAuth step):"
echo ""
echo "  1. Open: https://console.cloud.google.com/cloud-build/triggers;region=$REGION?project=$PROJECT_ID"
echo "  2. Click 'Connect Repository' → GitHub → authenticate → select $GITHUB_OWNER/$GITHUB_REPO"
echo "  3. Click 'Create Trigger':"
echo "       Name:          deploy-on-push-main"
echo "       Event:         Push to branch"
echo "       Branch:        ^main$"
echo "       Config type:   Cloud Build config file (cloudbuild.yaml)"
echo "       Config path:   cloudbuild.yaml"
echo ""
echo "  After connecting, triggers can be managed via CLI. This is the only"
echo "  step that requires the browser — GitHub OAuth cannot be done via gcloud."

# =============================================================================
# STEP 10 — Initial Cloud Run service (placeholder — real deploys via Cloud Build)
# =============================================================================

echo ""
echo "── Step 10: Deploy placeholder Cloud Run service ───────────────────────"

# Deploy a hello-world placeholder so the service URL is reserved
# Real app is deployed by Cloud Build on first push to main
gcloud run deploy "$SERVICE_NAME" \
  --image=us-docker.pkg.dev/cloudrun/container/hello \
  --region="$REGION" \
  --platform=managed \
  --allow-unauthenticated \
  --port=8080 \
  --memory=512Mi \
  --cpu=1 \
  --min-instances=0 \
  --max-instances=5 \
  --service-account="$PORTAL_SA" \
  --vpc-connector=client-portal-connector \
  --vpc-egress=private-ranges-only \
  --project="$PROJECT_ID"

SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" \
  --region="$REGION" \
  --project="$PROJECT_ID" \
  --format="value(status.url)")

echo "✓ Cloud Run service live (placeholder): $SERVICE_URL"

# =============================================================================
# STEP 11 — Custom domain mapping
#           Points project.topsisconsulting.com → this Cloud Run service
# =============================================================================

echo ""
echo "── Step 11: Custom domain ──────────────────────────────────────────────"

gcloud run domain-mappings create \
  --service="$SERVICE_NAME" \
  --domain=project.topsisconsulting.com \
  --region="$REGION" \
  --project="$PROJECT_ID"

echo ""
echo "  ⚠  DNS step required in Squarespace (one-time):"
echo "     After running this script, run the following to get the DNS records:"
echo "     gcloud run domain-mappings describe --domain=project.topsisconsulting.com --region=$REGION --project=$PROJECT_ID"
echo "     Add the returned CNAME or A records to Squarespace DNS for project.topsisconsulting.com"

# =============================================================================
# DONE
# =============================================================================

echo ""
echo "════════════════════════════════════════════════════════════════════════"
echo "  Bootstrap complete."
echo ""
echo "  Project:      $PROJECT_ID"
echo "  Cloud Run:    $SERVICE_URL"
echo "  Domain:       https://project.topsisconsulting.com (DNS pending)"
echo "  Redis:        $REDIS_HOST:$REDIS_PORT"
echo "  Artifact Reg: $REGION-docker.pkg.dev/$PROJECT_ID/$ARTIFACT_REPO"
echo ""
echo "  Remaining manual steps:"
echo "    1. Populate secrets (see Step 6 output above)"
echo "    2. Connect GitHub repo in Cloud Build console (see Step 9 output)"
echo "    3. Add DNS records in Squarespace (see Step 11 output)"
echo "    4. Push cloudbuild.yaml + app code to main to trigger first real deploy"
echo "════════════════════════════════════════════════════════════════════════"
