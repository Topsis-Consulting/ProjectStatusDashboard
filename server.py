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

load_dotenv()

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
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]
EMAIL_FROM = os.getenv("EMAIL_FROM", "info@topsisconsulting.com")
EMAIL_FROM_NAME = os.getenv("EMAIL_FROM_NAME", "Topsis Consulting")

JIRA_URL = os.environ["JIRA_URL"]
JIRA_USER_EMAIL = os.environ["JIRA_USER_EMAIL"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
DEV_MODE = os.getenv("DEV_MODE", "false").lower() == "true"

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
            jql=f"({scope}) AND statusCategory != Done ORDER BY priority ASC, updated DESC",
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
# Routes
# ---------------------------------------------------------------------------

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
async def dashboard(session: str | None = Cookie(default=None)):
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
    epic_key = tenant.get("jira_epic_key")

    try:
        jira_data = await _fetch_jira_issues(project_key, epic_key)
    except Exception as e:
        print(f"Jira fetch failed: {e}")
        jira_data = {"open": [], "resolved": [], "go_live": []}

    stats = _summarize_issues(jira_data["open"])
    issues = [_format_issue_row(i) for i in jira_data["open"][:10]]
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
        .replace("{{GO_LIVE_HTML}}", go_live_html)
        .replace("{{OPEN_COUNT}}", str(stats["total_open"]))
    )
    return HTMLResponse(html)


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
