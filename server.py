import json
import os
import random
import secrets
import string
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncio
import time

import httpx
from dotenv import load_dotenv
from fastapi import Cookie, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jose import JWTError, jwt

load_dotenv(Path(__file__).parent / ".env")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SESSION_SECRET = os.environ["SESSION_SECRET"]
SESSION_ALGORITHM = "HS256"
SESSION_TTL_HOURS = 8
OTP_TTL_SECONDS = 900  # 15 minutes

SMTP_HOST = os.getenv("SMTP_HOST", "smtp-relay.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.environ["SMTP_USER"]
# Gmail app passwords are displayed in 4-char groups with spaces; auth wants them stripped.
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"].replace(" ", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "info@topsisconsulting.com")
EMAIL_FROM_NAME = os.getenv("EMAIL_FROM_NAME", "Topsis Consulting")

JIRA_URL = os.environ["JIRA_URL"]
JIRA_USER_EMAIL = os.environ["JIRA_USER_EMAIL"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
DEV_MODE = os.getenv("DEV_MODE", "false").lower() == "true"

# Salesforce — read-only Opportunity milestone dates.
# Production auth: Connected App + JWT Bearer flow (private key injected from
# Google Secret Manager). Dev fallback: a pre-obtained access token, e.g. from
# `sf org display --target-org prod_org --json`.
SF_LOGIN_URL = os.getenv("SF_LOGIN_URL", "https://login.salesforce.com")
SF_API_VERSION = os.getenv("SF_API_VERSION", "v60.0")
SF_CLIENT_ID = os.getenv("SF_CLIENT_ID")          # Connected App consumer key
SF_USERNAME = os.getenv("SF_USERNAME")            # integration user the JWT acts as
SF_PRIVATE_KEY = os.getenv("SF_PRIVATE_KEY")      # PEM contents from Secret Manager
SF_ACCESS_TOKEN = os.getenv("SF_ACCESS_TOKEN")    # dev fallback
SF_INSTANCE_URL = os.getenv("SF_INSTANCE_URL")    # dev fallback

# Milestone fields to display, in timeline order: (label, Opportunity field API name).
# Deliberately excludes Contract Start, Mid-Meeting, and Project Completed.
SF_MILESTONES = [
    ("Kickoff", "Kickoff_Date__c"),
    ("Finish Disco / Start Config", "Finish_Disco_Start_Config__c"),
    ("Testing Kickoff", "Testing_Kickoff__c"),
    ("Go Live / Initial Wrap", "Go_Live_Initial_Wrap__c"),
    ("Final Wrap", "Final_Wrap__c"),
    ("Project Deadline", "Project_Deadline_Date__c"),
]

# In-memory OTP store for local dev (replaced by Redis in production)
_otp_store: dict[str, tuple[str, float]] = {}  # key -> (otp, expires_at)

BASE_DIR = Path(__file__).parent

# ---------------------------------------------------------------------------
# Tenant registry
# ---------------------------------------------------------------------------

with open(BASE_DIR / "registry.json") as f:
    TENANT_REGISTRY: dict = json.load(f)


def get_tenant(domain: str) -> dict | None:
    return TENANT_REGISTRY.get(domain.lower())


# ---------------------------------------------------------------------------
# App + static files
# ---------------------------------------------------------------------------

app = FastAPI()
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


async def _otp_set(key: str, value: str, ttl: int):
    _otp_store[key] = (value, time.time() + ttl)


async def _otp_get(key: str) -> str | None:
    entry = _otp_store.get(key)
    if not entry:
        return None
    value, expires_at = entry
    if time.time() > expires_at:
        _otp_store.pop(key, None)
        return None
    return value


async def _otp_delete(key: str):
    _otp_store.pop(key, None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _render(template_name: str, replacements: dict = {}) -> HTMLResponse:
    path = BASE_DIR / "templates" / template_name
    html = path.read_text()
    for key, value in replacements.items():
        html = html.replace(f"{{{{{key}}}}}", str(value))
    return HTMLResponse(html)


def _generate_otp() -> str:
    return "".join(random.choices(string.digits, k=6))


def _create_session_token(email: str, domain: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS)
    return jwt.encode(
        {"email": email, "domain": domain, "exp": expire},
        SESSION_SECRET,
        algorithm=SESSION_ALGORITHM,
    )


def _decode_session_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, SESSION_SECRET, algorithms=[SESSION_ALGORITHM])
    except JWTError:
        return None


async def _send_otp_email(to_email: str, otp: str):
    if DEV_MODE:
        print(f"\n{'='*40}")
        print(f"  DEV MODE — OTP for {to_email}: {otp}")
        print(f"{'='*40}\n")
        return

    import aiosmtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Your Topsis Portal Access Code"
    msg["From"] = f"{EMAIL_FROM_NAME} <{EMAIL_FROM}>"
    msg["To"] = to_email

    formatted_otp = f"{otp[:3]} {otp[3:]}"
    text_body = f"Your one-time access code is: {formatted_otp}\n\nThis code expires in 15 minutes. Do not share it with anyone."
    html_body = f"""
    <div style="font-family: Inter, system-ui, sans-serif; max-width: 480px; margin: 0 auto; padding: 40px 24px;">
      <img src="https://project.topsisconsulting.com/static/Topsis_logo.png" alt="Topsis Consulting" style="height: 40px; margin-bottom: 32px;" />
      <h2 style="font-size: 20px; font-weight: 600; color: #111; margin-bottom: 8px;">Your access code</h2>
      <p style="color: #6b7280; font-size: 14px; margin-bottom: 24px;">Use this code to sign in to the Topsis Client Portal. It expires in 15 minutes.</p>
      <div style="background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 12px; padding: 24px; text-align: center; margin-bottom: 24px;">
        <span style="font-size: 36px; font-weight: 700; letter-spacing: 8px; color: #111;">{formatted_otp}</span>
      </div>
      <p style="color: #9ca3af; font-size: 12px;">If you didn't request this code, you can safely ignore this email.</p>
    </div>
    """

    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    await aiosmtplib.send(
        msg,
        hostname=SMTP_HOST,
        port=SMTP_PORT,
        username=SMTP_USER,
        password=SMTP_PASSWORD,
        start_tls=True,
    )


# ---------------------------------------------------------------------------
# Jira helpers
# ---------------------------------------------------------------------------

def _jira_auth() -> tuple[str, str]:
    return (JIRA_USER_EMAIL, JIRA_API_TOKEN)


async def _jira_search(client: httpx.AsyncClient, jql: str, fields: list, max_results: int = 50) -> list:
    resp = await client.post(
        f"{JIRA_URL}/rest/api/3/search/jql",
        json={"jql": jql, "maxResults": max_results, "fields": fields},
        auth=_jira_auth(),
    )
    resp.raise_for_status()
    return resp.json().get("issues", [])


async def _fetch_jira_issues(project_key: str, epic_key: str | None = None) -> dict:
    scope = f'"Epic Link" = {epic_key} OR parent = {epic_key}' if epic_key else f"project = {project_key}"

    async with httpx.AsyncClient() as client:
        open_issues = await _jira_search(
            client,
            jql=f"({scope}) AND statusCategory != Done ORDER BY priority DESC, updated DESC",
            fields=["summary", "status", "priority", "assignee", "issuetype", "updated"],
        )
        resolved = await _jira_search(
            client,
            jql=f"({scope}) AND statusCategory = Done AND updated >= -7d ORDER BY updated DESC",
            fields=["summary", "status", "updated", "assignee"],
            max_results=10,
        )
        go_live = await _jira_search(
            client,
            jql=f'project = {project_key} AND summary ~ "Go Live" ORDER BY created ASC',
            fields=["summary", "duedate", "status"],
            max_results=1,
        )

        epic_name = None
        if epic_key:
            epic_resp = await client.get(
                f"{JIRA_URL}/rest/api/3/issue/{epic_key}",
                params={"fields": "summary"},
                auth=_jira_auth(),
            )
            if epic_resp.status_code == 200:
                epic_name = epic_resp.json()["fields"]["summary"]

    return {
        "open": open_issues,
        "resolved": resolved,
        "go_live": go_live,
        "epic_name": epic_name,
    }


async def _fetch_epics(project_key: str, include_all: bool = False) -> list[dict]:
    """List epics in a project for the employee switcher.

    Default (include_all=False): active epics only (statusCategory != Done),
    with the '[DUPLICATE - TO DELETE]' housekeeping epics filtered out.
    """
    jql = f"project = {project_key} AND issuetype = Epic"
    if not include_all:
        jql += ' AND statusCategory != Done AND summary !~ "DUPLICATE"'
    jql += " ORDER BY summary ASC"

    # /search/jql caps at 100 results per page — paginate via nextPageToken so
    # the switcher isn't silently truncated to the first 100 epics.
    epics: list[dict] = []
    token = None
    async with httpx.AsyncClient() as client:
        for _ in range(15):  # safety cap: 15 pages = 1500 epics
            body = {"jql": jql, "maxResults": 100, "fields": ["summary", "status"]}
            if token:
                body["nextPageToken"] = token
            resp = await client.post(f"{JIRA_URL}/rest/api/3/search/jql", json=body, auth=_jira_auth())
            resp.raise_for_status()
            data = resp.json()
            for i in data.get("issues", []):
                epics.append({
                    "key": i["key"],
                    "summary": i["fields"]["summary"],
                    "status": i["fields"]["status"]["name"],
                })
            token = data.get("nextPageToken")
            if not token:
                break

    return epics


def _summarize_issues(issues: list) -> dict:
    total_open = len(issues)
    in_progress = sum(1 for i in issues if i["fields"]["status"]["name"] == "In Progress")
    questions = sum(1 for i in issues if i["fields"]["status"]["name"] == "Questions")
    high_priority = sum(
        1 for i in issues
        if i["fields"].get("priority", {}).get("name", "").lower() in ("highest", "high")
    )
    return {"total_open": total_open, "in_progress": in_progress, "questions": questions, "high_priority": high_priority}


def _format_issue_row(issue: dict) -> dict:
    fields = issue["fields"]
    status_name = fields["status"]["name"]
    status_cat = fields["status"]["statusCategory"]["key"]
    assignee = fields.get("assignee")
    priority = fields.get("priority", {}).get("name", "Medium")

    status_class = {
        "new": "status-open",
        "indeterminate": "status-inprog",
        "done": "status-done",
    }.get(status_cat, "status-open")

    return {
        "key": issue["key"],
        "summary": fields["summary"],
        "status_name": status_name,
        "status_class": status_class,
        "priority": priority,
        "priority_class": "priority-high" if priority.lower() in ("highest", "high") else
                          "priority-medium" if priority.lower() == "medium" else "priority-low",
        "assignee": assignee["displayName"] if assignee else "Unassigned",
    }


def _format_activity(resolved: list) -> list:
    items = []
    for issue in resolved[:5]:
        fields = issue["fields"]
        updated = fields.get("updated", "")
        items.append({
            "key": issue["key"],
            "summary": fields["summary"],
            "updated": updated[:10] if updated else "",
        })
    return items


def _parse_go_live(go_live_issues: list) -> dict | None:
    if not go_live_issues:
        return None
    fields = go_live_issues[0]["fields"]
    due = fields.get("duedate")
    if not due:
        return None
    due_date = datetime.strptime(due, "%Y-%m-%d").date()
    today = datetime.now(timezone.utc).date()
    days_remaining = (due_date - today).days
    formatted = due_date.strftime("%B %-d, %Y")
    return {"date": formatted, "days_remaining": max(days_remaining, 0)}


# ---------------------------------------------------------------------------
# Salesforce helpers
# ---------------------------------------------------------------------------

# Cached JWT access token: {"token": str, "instance_url": str, "expires_at": float}
_sf_token_cache: dict = {}


async def _sf_auth() -> tuple[str, str]:
    """Return (access_token, instance_url).

    Dev: use a static SF_ACCESS_TOKEN / SF_INSTANCE_URL if provided.
    Prod: mint a short-lived token via the JWT Bearer flow and cache it.
    """
    if SF_ACCESS_TOKEN and SF_INSTANCE_URL:
        return SF_ACCESS_TOKEN, SF_INSTANCE_URL

    cached = _sf_token_cache.get("data")
    if cached and time.time() < cached["expires_at"]:
        return cached["token"], cached["instance_url"]

    if not (SF_CLIENT_ID and SF_USERNAME and SF_PRIVATE_KEY):
        raise RuntimeError("Salesforce credentials not configured")

    now = int(time.time())
    assertion = jwt.encode(
        {"iss": SF_CLIENT_ID, "sub": SF_USERNAME, "aud": SF_LOGIN_URL, "exp": now + 300},
        SF_PRIVATE_KEY,
        algorithm="RS256",
    )

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{SF_LOGIN_URL}/services/oauth2/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    _sf_token_cache["data"] = {
        "token": data["access_token"],
        "instance_url": data["instance_url"],
        "expires_at": time.time() + 3600,  # refresh hourly, well within session lifetime
    }
    return data["access_token"], data["instance_url"]


async def _fetch_opportunity(opportunity_id: str) -> dict | None:
    """Query the milestone date fields for one Opportunity. Read-only."""
    token, instance_url = await _sf_auth()
    field_list = ", ".join(field for _, field in SF_MILESTONES)
    soql = f"SELECT Id, Name, {field_list} FROM Opportunity WHERE Id = '{opportunity_id}'"

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{instance_url}/services/data/{SF_API_VERSION}/query",
            params={"q": soql},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        records = resp.json().get("records", [])

    return records[0] if records else None


def _build_milestones(record: dict) -> list[dict]:
    """Turn an Opportunity record into ordered milestone entries with status.

    Status is one of: done (dated, in the past), current (next dated milestone
    still ahead — or the most recent one if all are past), upcoming (dated,
    future), pending (no date set yet).
    """
    today = datetime.now(timezone.utc).date()

    parsed = []
    for label, field in SF_MILESTONES:
        raw = record.get(field)
        date = datetime.strptime(raw, "%Y-%m-%d").date() if raw else None
        parsed.append({"label": label, "field": field, "date": date})

    dated = [m for m in parsed if m["date"]]
    upcoming = [m for m in dated if m["date"] >= today]
    current_field = None
    if upcoming:
        current_field = min(upcoming, key=lambda m: m["date"])["field"]
    elif dated:
        current_field = max(dated, key=lambda m: m["date"])["field"]

    milestones = []
    for m in parsed:
        date = m["date"]
        if date is None:
            status = "pending"
        elif m["field"] == current_field:
            status = "current"
        elif date < today:
            status = "done"
        else:
            status = "upcoming"
        milestones.append({
            "label": m["label"],
            "status": status,
            "date_display": date.strftime("%b %-d, %Y") if date else "TBD",
            "days_out": (date - today).days if date else None,
        })
    return milestones


def _render_timeline(milestones: list[dict]) -> str:
    """Render the milestone timeline card. Returns '' if there is nothing to show."""
    if not milestones:
        return ""

    dot_by_status = {
        "done": '<span class="tl-dot tl-done"><svg width="11" height="11" fill="none" viewBox="0 0 24 24" stroke="white" stroke-width="3"><path d="M5 13l4 4L19 7"/></svg></span>',
        "current": '<span class="tl-dot tl-current"></span>',
        "upcoming": '<span class="tl-dot tl-upcoming"></span>',
        "pending": '<span class="tl-dot tl-pending"></span>',
    }

    nodes = ""
    for m in milestones:
        nodes += f"""
        <div class="tl-node tl-{m['status']}">
          {dot_by_status.get(m['status'], dot_by_status['upcoming'])}
          <div class="tl-text">
            <p class="tl-label">{m['label']}</p>
            <p class="tl-date">{m['date_display']}</p>
          </div>
        </div>"""

    return f"""
    <div class="card p-5">
      <div class="flex items-center justify-between mb-5">
        <h2 class="text-sm font-semibold text-gray-900">Project Timeline</h2>
        <span class="text-xs text-gray-400">Salesforce</span>
      </div>
      <div class="tl">{nodes}
      </div>
    </div>"""


def _render_epic_switcher(epics: list[dict], current_key: str | None) -> str:
    """Searchable epic switcher for employees. Renders the active epics inline;
    a 'show all' control fetches the full list from /api/epics on demand."""
    current_label = next((e["summary"] for e in epics if e["key"] == current_key), current_key or "Select a project")

    def _item(e: dict) -> str:
        active = " epic-item-active" if e["key"] == current_key else ""
        return (
            f'<a href="/dashboard?epic={e["key"]}" class="epic-item{active}" '
            f'data-search="{(e["summary"] + " " + e["key"]).lower()}">'
            f'<span class="epic-item-name">{e["summary"]}</span>'
            f'<span class="epic-item-key">{e["key"]}</span></a>'
        )

    items = "".join(_item(e) for e in epics)
    return f"""
    <div id="epic-switcher" class="relative">
      <button type="button" onclick="toggleEpicMenu()" class="epic-trigger">
        <span class="truncate max-w-[220px]">{current_label}</span>
        <svg width="14" height="14" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path d="M6 9l6 6 6-6"/></svg>
      </button>
      <div id="epic-menu" class="epic-menu hidden">
        <div class="p-2 border-b border-gray-100">
          <input id="epic-search" type="text" placeholder="Search projects…" oninput="filterEpics(this.value)"
                 class="w-full text-sm px-2.5 py-1.5 border border-gray-200 rounded-md focus:outline-none focus:ring-2 focus:ring-brand/30" />
        </div>
        <div id="epic-list" class="epic-list">{items}</div>
        <div class="p-2 border-t border-gray-100">
          <button type="button" onclick="loadAllEpics()" id="epic-showall" class="text-xs text-gray-500 hover:text-brand">Show all epics (incl. done)</button>
        </div>
      </div>
    </div>"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/dev-login")
async def dev_login():
    if not DEV_MODE:
        raise HTTPException(status_code=404)
    token = _create_session_token("jp@topsisconsulting.com", "topsisconsulting.com")
    redirect = RedirectResponse(url="/dashboard", status_code=303)
    redirect.set_cookie(key="session", value=token, httponly=True, secure=False, samesite="strict", max_age=SESSION_TTL_HOURS * 3600)
    return redirect


@app.get("/", response_class=HTMLResponse)
async def login_page():
    return _render("login.html")


@app.post("/auth/request")
async def auth_request(email: str = Form(...)):
    domain = email.split("@")[-1].lower() if "@" in email else ""
    tenant = get_tenant(domain)

    # Generic response regardless of whether domain matches (prevents enumeration)
    if tenant:
        otp = _generate_otp()
        otp_key = f"otp:{email.lower()}"
        await _otp_set(otp_key, otp, OTP_TTL_SECONDS)
        try:
            await _send_otp_email(email, otp)
        except Exception as e:
            print(f"Email send failed: {e}")

    return JSONResponse({"status": "sent"})


@app.post("/auth/verify")
async def auth_verify(response: Response, email: str = Form(...), otp: str = Form(...)):
    domain = email.split("@")[-1].lower() if "@" in email else ""
    tenant = get_tenant(domain)

    if not tenant:
        raise HTTPException(status_code=401, detail="Invalid code.")

    otp_key = f"otp:{email.lower()}"
    stored = await _otp_get(otp_key)

    if not stored or stored != otp.strip():
        raise HTTPException(status_code=401, detail="Invalid or expired code.")

    await _otp_delete(otp_key)

    token = _create_session_token(email, domain)
    redirect = RedirectResponse(url="/dashboard", status_code=303)
    redirect.set_cookie(
        key="session",
        value=token,
        httponly=True,
        secure=not DEV_MODE,
        samesite="strict",
        max_age=SESSION_TTL_HOURS * 3600,
    )
    return redirect


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(session: str | None = Cookie(default=None), epic: str | None = None):
    if not session:
        return RedirectResponse(url="/")

    payload = _decode_session_token(session)
    if not payload:
        return RedirectResponse(url="/")

    domain = payload["domain"]
    tenant = get_tenant(domain)
    if not tenant:
        return RedirectResponse(url="/")

    project_key = tenant["jira_project_key"]
    home_epic_key = tenant.get("jira_epic_key")
    epic_key = home_epic_key
    is_employee = tenant.get("role") == "employee"

    # Employees may switch epics via ?epic=; clients are hard-locked to their own.
    # Validate the requested epic belongs to the tenant's project (prefix match)
    # so no one can read another project by guessing keys.
    if is_employee and epic and epic.upper().startswith(f"{project_key}-"):
        epic_key = epic.upper()

    try:
        jira_data = await _fetch_jira_issues(project_key, epic_key)
    except Exception as e:
        print(f"Jira fetch failed: {e}")
        jira_data = {"open": [], "resolved": [], "go_live": []}

    # Employee epic switcher (active epics only by default)
    epic_switcher_html = ""
    if is_employee:
        try:
            epics = await _fetch_epics(project_key)
            epic_switcher_html = _render_epic_switcher(epics, epic_key)
        except Exception as e:
            print(f"Epic list fetch failed: {e}")

    # Salesforce milestone timeline — only valid on the tenant's home epic, since
    # sf_opportunity_id is pinned to the tenant (there is no epic→Opportunity map yet).
    # Suppress it when an employee has switched to a different epic.
    timeline_html = ""
    sf_opportunity_id = tenant.get("sf_opportunity_id")
    if sf_opportunity_id and epic_key == home_epic_key:
        try:
            opp = await _fetch_opportunity(sf_opportunity_id)
            if opp:
                timeline_html = _render_timeline(_build_milestones(opp))
        except Exception as e:
            print(f"Salesforce fetch failed: {e}")

    stats = _summarize_issues(jira_data["open"])
    # Render the full open set so the stat counts and the click-to-filter list
    # agree (previously capped at [:10], which hid high-priority tickets ranked
    # lower and made "High Priority: 4" filter down to 1).
    issues = [_format_issue_row(i) for i in jira_data["open"]]
    activity = _format_activity(jira_data["resolved"])
    go_live = _parse_go_live(jira_data["go_live"])
    epic_name = jira_data.get("epic_name") or project_key

    # Build filter button data
    assignees = sorted(set(i["assignee"] for i in issues))
    statuses = sorted(set(i["status_name"] for i in issues))

    assignee_btns = "".join(
        f'<button class="filter-btn" onclick="filterAssignee(this, \'{a}\')">{a}</button>'
        for a in assignees
    )
    status_btns = "".join(
        f'<button class="filter-btn" onclick="filterStatus(this, \'{s}\')">{s}</button>'
        for s in statuses
    )

    # Build issues HTML
    issues_html = ""
    for issue in issues:
        issues_html += f"""
        <div class="issue-row px-5 py-3.5 flex items-start gap-3 transition-colors"
             data-assignee="{issue['assignee']}"
             data-status="{issue['status_name']}"
             data-priority="{'high' if issue['priority'].lower() in ('highest', 'high') else 'other'}"
             onclick="openIssue('{issue['key']}')">
          <div class="flex-shrink-0 mt-0.5">
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
              <rect width="14" height="14" rx="3" fill="#175cd3"/>
              <path d="M3 7h8M7 3v8" stroke="white" stroke-width="1.5" stroke-linecap="round"/>
            </svg>
          </div>
          <div class="flex-1 min-w-0">
            <div class="flex items-start justify-between gap-2">
              <p class="text-sm text-gray-800 font-medium leading-snug">{issue['summary']}</p>
              <span class="{issue['status_class']} text-xs px-2 py-0.5 rounded-full border flex-shrink-0 font-medium">{issue['status_name']}</span>
            </div>
            <p class="text-xs text-gray-400 mt-0.5">{issue['key']} · <span class="{issue['priority_class']} font-medium">{issue['priority']} priority</span> · {issue['assignee']}</p>
          </div>
          <svg class="flex-shrink-0 mt-1 text-gray-300" width="14" height="14" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path d="M9 5l7 7-7 7"/></svg>
        </div>"""

    # Build activity HTML
    activity_html = ""
    for item in activity:
        activity_html += f"""
        <div class="px-5 py-3.5">
          <div class="flex items-start gap-2.5">
            <div class="w-6 h-6 rounded-full bg-green-100 flex items-center justify-center flex-shrink-0 mt-0.5">
              <svg width="10" height="10" fill="none" viewBox="0 0 24 24" stroke="#166534" stroke-width="2.5"><path d="M5 13l4 4L19 7"/></svg>
            </div>
            <div class="min-w-0">
              <p class="text-xs text-gray-700 leading-snug font-medium">{item['key']} marked Done</p>
              <p class="text-xs text-gray-400 mt-0.5 leading-snug">{item['summary']}</p>
              <p class="text-xs text-gray-300 mt-1">{item['updated']}</p>
            </div>
          </div>
        </div>"""

    # Go-live banner HTML
    go_live_html = ""
    if go_live:
        go_live_html = f"""
        <div class="go-live-banner rounded-xl p-5 flex items-center justify-between text-white">
          <div class="flex items-center gap-4">
            <div class="w-10 h-10 rounded-xl bg-white/20 flex items-center justify-center flex-shrink-0">
              <svg width="20" height="20" fill="none" viewBox="0 0 24 24" stroke="white" stroke-width="2"><path d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"/></svg>
            </div>
            <div>
              <p class="text-xs font-medium text-white/70 uppercase tracking-wider">Target Go-Live</p>
              <p class="text-xl font-bold">{go_live['date']}</p>
            </div>
          </div>
          <div class="text-right hidden sm:block">
            <p class="text-xs text-white/70">Days remaining</p>
            <p class="text-3xl font-bold">{go_live['days_remaining']}</p>
          </div>
        </div>"""

    template = (BASE_DIR / "templates" / "dashboard.html").read_text()
    html = (
        template
        .replace("{{TENANT_NAME}}", tenant["tenant_name"])
        .replace("{{PROJECT_KEY}}", project_key)
        .replace("{{EPIC_NAME}}", epic_name)
        .replace("{{USER_EMAIL}}", payload["email"])
        .replace("{{STAT_OPEN}}", str(stats["total_open"]))
        .replace("{{STAT_INPROG}}", str(stats["in_progress"]))
        .replace("{{STAT_QUESTIONS}}", str(stats["questions"]))
        .replace("{{STAT_HIGH}}", str(stats["high_priority"]))
        .replace("{{ISSUES_HTML}}", issues_html)
        .replace("{{ASSIGNEE_FILTER_BTNS}}", assignee_btns)
        .replace("{{STATUS_FILTER_BTNS}}", status_btns)
        .replace("{{ACTIVITY_HTML}}", activity_html)
        .replace("{{TIMELINE_HTML}}", timeline_html or go_live_html)
        .replace("{{EPIC_SWITCHER_HTML}}", epic_switcher_html)
        .replace("{{OPEN_COUNT}}", str(stats["total_open"]))
    )
    return HTMLResponse(html)


@app.get("/api/epics")
async def get_epics(session: str | None = Cookie(default=None), all: bool = False):
    """Epic list for the employee switcher. Employee sessions only."""
    payload = _decode_session_token(session) if session else None
    if not payload:
        raise HTTPException(status_code=401)
    tenant = get_tenant(payload["domain"])
    if not tenant or tenant.get("role") != "employee":
        raise HTTPException(status_code=403)

    epics = await _fetch_epics(tenant["jira_project_key"], include_all=all)
    return JSONResponse({"epics": epics})


@app.get("/api/issue/{issue_key}")
async def get_issue(issue_key: str, session: str | None = Cookie(default=None)):
    if not session or not _decode_session_token(session):
        raise HTTPException(status_code=401)

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{JIRA_URL}/rest/api/3/issue/{issue_key}",
            params={"fields": "summary,description,comment,duedate,assignee,status,priority,issuetype"},
            auth=_jira_auth(),
        )
        resp.raise_for_status()

    data = resp.json()
    fields = data["fields"]

    return JSONResponse({
        "key": data["key"],
        "summary": fields["summary"],
        "status": fields["status"]["name"],
        "priority": fields.get("priority", {}).get("name", "Medium"),
        "assignee": fields["assignee"]["displayName"] if fields.get("assignee") else None,
        "duedate": fields.get("duedate"),
        "description": _adf_to_text(fields.get("description")),
        "comments": [
            {
                "author": c["author"]["displayName"],
                "created": c["created"][:10],
                "body": _adf_to_text(c.get("body")),
            }
            for c in fields.get("comment", {}).get("comments", [])
        ],
    })


def _adf_to_text(node: dict | None) -> str:
    if not node:
        return ""
    if isinstance(node, str):
        return node
    if node.get("type") == "text":
        return node.get("text", "")
    parts = []
    for child in node.get("content", []):
        text = _adf_to_text(child)
        if text:
            parts.append(text)
    separator = "\n" if node.get("type") in ("paragraph", "heading", "listItem", "bulletList", "orderedList") else ""
    return separator.join(parts)


@app.get("/auth/logout")
async def logout():
    response = RedirectResponse(url="/")
    response.delete_cookie("session")
    return response
