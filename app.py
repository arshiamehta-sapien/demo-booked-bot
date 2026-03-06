"""
Sapien Sales Bot
================
Slack slash commands that manage deals in HubSpot's Sales Pipeline New.

Commands:
  /demo-booked  → Log a new demo (creates deal, contact, company)
  /deal-update  → Move a deal to a different stage
  /log-note     → Add a note to a deal
  /won          → Mark a deal as Closed Won
  /lost         → Mark a deal as Closed Lost (with reason)
  /tldr         → Get an AI-powered deal summary

Scheduled:
  Daily Pipeline Recap → Posts pipeline summary to a Slack channel every morning
"""

import os
import re
import time
import logging
import threading
import requests
import anthropic
from datetime import datetime, timedelta
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# ── Config ───────────────────────────────────────────────────────────────────
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
HUBSPOT_API_KEY = os.environ["HUBSPOT_API_KEY"]

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

RECAP_CHANNEL_ID = os.environ.get("RECAP_CHANNEL_ID", "")
RECAP_HOUR = int(os.environ.get("RECAP_HOUR", "9"))
RECAP_MINUTE = int(os.environ.get("RECAP_MINUTE", "0"))

HUBSPOT_BASE = "https://api.hubapi.com"
HUBSPOT_ACCOUNT_ID = "46061347"

# ── Pipeline & Stages (Sales Pipeline New) ───────────────────────────────────
HUBSPOT_PIPELINE_ID = "876727395"

STAGES = {
    "Demo Booked":          "1315454373",
    "Demo Completed":       "1315454374",
    "Qualified (NDA Sent)": "1315454375",
    "NDA Signed":           "1315454376",
    "POC in Progress":      "1315454377",
    "POC Value Proven":     "1315454378",
    "Pilot Contract Sent":  "1315454379",
    "Pilot Company Setup":  "1315574147",
    "Pilot Kick-Off":       "1315574148",
    "Pilot Period":         "1315574149",
    "Pilot Read Out":       "1315574150",
    "Proposal/Pricing":     "1315574151",
    "Security/Procurement": "1315574152",
    "Verbal Commit":        "1315574153",
    "Closed Won":           "1315574154",
    "Parked":               "1315574155",
    "Closed Lost":          "1315574156",
}

HUBSPOT_STAGE_ID = STAGES["Demo Booked"]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = App(token=SLACK_BOT_TOKEN)


# ── Helpers: HubSpot API ─────────────────────────────────────────────────────

def hubspot_headers():
    return {
        "Authorization": f"Bearer {HUBSPOT_API_KEY}",
        "Content-Type": "application/json",
    }


# ══════════════════════════════════════════════════════════════════════════════
# OWNER LOOKUP — BULLETPROOF MULTI-STRATEGY
# ══════════════════════════════════════════════════════════════════════════════

def find_hubspot_owner_id(slack_email: str, slack_real_name: str = "") -> str:
    """Find a HubSpot owner ID from a Slack user's email/name.

    Tries 4 strategies in order. Returns owner ID string or "".
    Every step is logged so you can see what's happening in Railway.
    """
    logger.info(f"=== OWNER LOOKUP START === email='{slack_email}', name='{slack_real_name}'")

    # ── Strategy 1: GET /crm/v3/owners/?email=<email> ──
    owner_id = _strategy_email_filter(slack_email)
    if owner_id:
        return owner_id

    # ── Strategy 2: GET all owners, loop and match email ──
    all_owners = _fetch_all_owners()
    owner_id = _strategy_email_loop(slack_email, all_owners)
    if owner_id:
        return owner_id

    # ── Strategy 3: Match by name ──
    owner_id = _strategy_name_match(slack_real_name, all_owners)
    if owner_id:
        return owner_id

    # ── Strategy 4: GET /settings/v3/users, find by email ──
    owner_id = _strategy_settings_users(slack_email)
    if owner_id:
        return owner_id

    logger.error(f"=== OWNER LOOKUP FAILED === No match for email='{slack_email}', name='{slack_real_name}'")
    return ""


def _strategy_email_filter(email: str) -> str:
    """Strategy 1: Use the email query param on /crm/v3/owners."""
    try:
        url = f"{HUBSPOT_BASE}/crm/v3/owners"
        resp = requests.get(url, headers=hubspot_headers(), params={"email": email, "limit": 1})
        logger.info(f"[S1] GET /crm/v3/owners?email={email} → status={resp.status_code}")
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if results:
            oid = results[0]["id"]
            logger.info(f"[S1] ✅ Found owner {oid}")
            return oid
        logger.info(f"[S1] No results returned")
    except Exception as e:
        logger.warning(f"[S1] Exception: {e}")
    return ""


def _fetch_all_owners() -> list:
    """Fetch every owner from HubSpot with pagination."""
    all_owners = []
    after = None
    try:
        while True:
            params = {"limit": 100}
            if after:
                params["after"] = after
            resp = requests.get(f"{HUBSPOT_BASE}/crm/v3/owners", headers=hubspot_headers(), params=params)
            resp.raise_for_status()
            data = resp.json()
            owners_page = data.get("results", [])
            all_owners.extend(owners_page)
            after = data.get("paging", {}).get("next", {}).get("after")
            if not after:
                break
    except Exception as e:
        logger.warning(f"[fetch_all_owners] Exception: {e}")
    logger.info(f"[fetch_all_owners] Total owners fetched: {len(all_owners)}")
    return all_owners


def _strategy_email_loop(email: str, all_owners: list) -> str:
    """Strategy 2: Loop through all owners, compare email."""
    email_lower = email.lower()
    for owner in all_owners:
        oe = owner.get("email", "") or ""
        oid = owner.get("id", "")
        name = f"{owner.get('firstName', '')} {owner.get('lastName', '')}".strip()
        logger.info(f"[S2]   owner {oid}: email='{oe}' name='{name}'")
        if oe and oe.lower() == email_lower:
            logger.info(f"[S2] ✅ Matched owner {oid} by email")
            return oid
    logger.info(f"[S2] No email match found")
    return ""


def _strategy_name_match(slack_name: str, all_owners: list) -> str:
    """Strategy 3: Match Slack display name to HubSpot owner name."""
    if not slack_name:
        logger.info(f"[S3] Skipped — no Slack name provided")
        return ""
    name_lower = slack_name.lower().strip()
    for owner in all_owners:
        owner_name = f"{owner.get('firstName', '')} {owner.get('lastName', '')}".strip()
        if owner_name and owner_name.lower() == name_lower:
            oid = owner["id"]
            logger.info(f"[S3] ✅ Matched owner {oid} by name '{owner_name}'")
            return oid
    logger.info(f"[S3] No name match for '{slack_name}'")
    return ""


def _strategy_settings_users(email: str) -> str:
    """Strategy 4: Use /settings/v3/users to find user by email, then map to owner."""
    try:
        url = f"{HUBSPOT_BASE}/settings/v3/users"
        resp = requests.get(url, headers=hubspot_headers(), params={"limit": 100})
        logger.info(f"[S4] GET /settings/v3/users → status={resp.status_code}")
        if resp.status_code == 200:
            users = resp.json().get("results", [])
            email_lower = email.lower()
            for user in users:
                ue = user.get("email", "") or ""
                if ue.lower() == email_lower:
                    user_id = user.get("id", "")
                    logger.info(f"[S4] Found user by email: user_id={user_id}")
                    # The user ID in settings often matches the owner ID
                    # Verify by checking /crm/v3/owners/{user_id}
                    try:
                        check = requests.get(f"{HUBSPOT_BASE}/crm/v3/owners/{user_id}", headers=hubspot_headers())
                        if check.status_code == 200:
                            logger.info(f"[S4] ✅ Verified owner {user_id}")
                            return user_id
                    except Exception:
                        pass
                    # Return the user_id anyway — it usually works as owner ID
                    logger.info(f"[S4] ✅ Using user_id {user_id} as owner (unverified)")
                    return user_id
            logger.info(f"[S4] No user matched email '{email}'")
        else:
            logger.info(f"[S4] Settings API returned {resp.status_code} — may lack scope")
    except Exception as e:
        logger.warning(f"[S4] Exception: {e}")
    return ""


def force_set_deal_owner(deal_id: str, owner_id: str):
    """Forcefully PATCH the deal to set the owner. Separate call for reliability."""
    if not owner_id:
        return
    try:
        url = f"{HUBSPOT_BASE}/crm/v3/objects/deals/{deal_id}"
        body = {"properties": {"hubspot_owner_id": owner_id}}
        resp = requests.patch(url, json=body, headers=hubspot_headers())
        logger.info(f"[force_set_deal_owner] PATCH deal {deal_id} owner={owner_id} → status={resp.status_code}")
        if resp.status_code != 200:
            logger.error(f"[force_set_deal_owner] Response: {resp.text}")
        resp.raise_for_status()
        logger.info(f"[force_set_deal_owner] ✅ Owner set successfully")
    except Exception as e:
        logger.error(f"[force_set_deal_owner] ❌ Failed: {e}")


# ── Other HubSpot Helpers ────────────────────────────────────────────────────

def create_or_find_contact(email: str, first_name: str, last_name: str) -> str:
    search_url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search"
    search_body = {
        "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}]
    }
    resp = requests.post(search_url, json=search_body, headers=hubspot_headers())
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if results:
        contact_id = results[0]["id"]
        logger.info(f"Found existing contact {contact_id} for {email}")
        return contact_id

    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts"
    body = {"properties": {"email": email, "firstname": first_name, "lastname": last_name}}
    resp = requests.post(url, json=body, headers=hubspot_headers())
    resp.raise_for_status()
    contact_id = resp.json()["id"]
    logger.info(f"Created contact {contact_id} for {email}")
    return contact_id


def create_or_find_company(company_name: str, company_url: str = "") -> str:
    search_url = f"{HUBSPOT_BASE}/crm/v3/objects/companies/search"
    search_body = {
        "filterGroups": [{"filters": [{"propertyName": "name", "operator": "EQ", "value": company_name}]}]
    }
    resp = requests.post(search_url, json=search_body, headers=hubspot_headers())
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if results:
        company_id = results[0]["id"]
        logger.info(f"Found existing company {company_id} for {company_name}")
        if company_url:
            domain = company_url.replace("https://", "").replace("http://", "").replace("www.", "").strip("/")
            update_url = f"{HUBSPOT_BASE}/crm/v3/objects/companies/{company_id}"
            requests.patch(update_url, json={"properties": {"domain": domain, "website": company_url}}, headers=hubspot_headers())
        return company_id

    url = f"{HUBSPOT_BASE}/crm/v3/objects/companies"
    properties = {"name": company_name}
    if company_url:
        domain = company_url.replace("https://", "").replace("http://", "").replace("www.", "").strip("/")
        properties["domain"] = domain
        properties["website"] = company_url
    resp = requests.post(url, json={"properties": properties}, headers=hubspot_headers())
    resp.raise_for_status()
    company_id = resp.json()["id"]
    logger.info(f"Created company {company_id} for {company_name}")
    return company_id


def create_deal(deal_name: str, contact_id: str, company_id: str, amount: str = "", close_date: str = "", owner_id: str = "") -> str:
    url = f"{HUBSPOT_BASE}/crm/v3/objects/deals"
    properties = {
        "dealname": deal_name,
        "pipeline": HUBSPOT_PIPELINE_ID,
        "dealstage": HUBSPOT_STAGE_ID,
    }
    if amount:
        properties["amount"] = amount
    if close_date:
        properties["closedate"] = close_date
    if owner_id:
        properties["hubspot_owner_id"] = owner_id
    body = {
        "properties": properties,
        "associations": [
            {"to": {"id": contact_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 3}]},
            {"to": {"id": company_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 5}]},
        ],
    }
    resp = requests.post(url, json=body, headers=hubspot_headers())
    resp.raise_for_status()
    deal_id = resp.json()["id"]
    logger.info(f"Created deal {deal_id}: {deal_name} (owner_id in create={owner_id})")
    return deal_id


def associate_contact_to_company(contact_id: str, company_id: str):
    url = f"{HUBSPOT_BASE}/crm/v4/objects/contacts/{contact_id}/associations/companies/{company_id}"
    body = [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 1}]
    resp = requests.put(url, json=body, headers=hubspot_headers())
    resp.raise_for_status()
    logger.info(f"Associated contact {contact_id} with company {company_id}")


def search_deals_by_name(query: str, all_pipelines: bool = False) -> list:
    url = f"{HUBSPOT_BASE}/crm/v3/objects/deals/search"
    body = {"query": query, "properties": ["dealname", "dealstage", "pipeline", "amount"], "limit": 20}
    if not all_pipelines:
        body["filterGroups"] = [{"filters": [{"propertyName": "pipeline", "operator": "EQ", "value": HUBSPOT_PIPELINE_ID}]}]
    resp = requests.post(url, json=body, headers=hubspot_headers())
    resp.raise_for_status()
    results = resp.json().get("results", [])
    stage_id_to_name = {v: k for k, v in STAGES.items()}
    return [
        {"id": r["id"], "name": r.get("properties", {}).get("dealname", "Unknown"), "stage": stage_id_to_name.get(r.get("properties", {}).get("dealstage", ""), r.get("properties", {}).get("dealstage", ""))}
        for r in results
    ]


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND 1: /demo-booked  →  Log a new demo
# ══════════════════════════════════════════════════════════════════════════════

@app.command("/demo-booked")
def open_demo_form(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "demo_booked_submit",
            "title": {"type": "plain_text", "text": "Log Demo Booked"},
            "submit": {"type": "plain_text", "text": "Create in HubSpot"},
            "blocks": [
                {"type": "input", "block_id": "contact_name_block", "label": {"type": "plain_text", "text": "Contact Full Name"}, "element": {"type": "plain_text_input", "action_id": "contact_name", "placeholder": {"type": "plain_text", "text": "e.g. Jane Smith"}}},
                {"type": "input", "block_id": "company_block", "label": {"type": "plain_text", "text": "Company Name"}, "element": {"type": "plain_text_input", "action_id": "company_name", "placeholder": {"type": "plain_text", "text": "e.g. Acme Inc"}}},
                {"type": "input", "block_id": "email_block", "label": {"type": "plain_text", "text": "Email Address"}, "element": {"type": "plain_text_input", "action_id": "email", "placeholder": {"type": "plain_text", "text": "e.g. jane@acme.com"}}},
                {"type": "input", "block_id": "company_url_block", "label": {"type": "plain_text", "text": "Company Website URL"}, "element": {"type": "plain_text_input", "action_id": "company_url", "placeholder": {"type": "plain_text", "text": "e.g. https://www.acme.com"}}, "optional": True},
                {"type": "input", "block_id": "amount_block", "label": {"type": "plain_text", "text": "Deal Amount ($)"}, "element": {"type": "plain_text_input", "action_id": "amount", "placeholder": {"type": "plain_text", "text": "e.g. 50000"}}, "optional": True},
                {"type": "input", "block_id": "close_date_block", "label": {"type": "plain_text", "text": "Expected Close Date"}, "element": {"type": "datepicker", "action_id": "close_date", "placeholder": {"type": "plain_text", "text": "Pick a date"}}, "optional": True},
            ],
        },
    )


@app.view("demo_booked_submit")
def handle_demo_submission(ack, body, client, view):
    values = view["state"]["values"]
    full_name = values["contact_name_block"]["contact_name"]["value"].strip()
    company_name = values["company_block"]["company_name"]["value"].strip()
    email = values["email_block"]["email"]["value"].strip()
    company_url = (values["company_url_block"]["company_url"]["value"] or "").strip()
    amount = (values["amount_block"]["amount"]["value"] or "").strip()
    close_date = (values["close_date_block"]["close_date"]["selected_date"] or "")

    name_parts = full_name.split(" ", 1)
    first_name = name_parts[0]
    last_name = name_parts[1] if len(name_parts) > 1 else ""

    if "@" not in email:
        ack(response_action="errors", errors={"email_block": "Please enter a valid email address."})
        return

    ack()

    user_id = body["user"]["id"]
    deal_name = company_name

    # ── Step 1: Get Slack user info ──
    slack_email = ""
    slack_real_name = ""
    try:
        slack_user_info = client.users_info(user=user_id)
        slack_profile = slack_user_info["user"]["profile"]
        slack_email = slack_profile.get("email", "") or ""
        slack_real_name = slack_user_info["user"].get("real_name", "") or slack_profile.get("real_name", "") or ""
        logger.info(f"Slack user {user_id}: email='{slack_email}', name='{slack_real_name}'")
    except Exception as e:
        logger.error(f"Failed to get Slack user info: {e}", exc_info=True)

    # ── Step 2: Find HubSpot owner ──
    owner_id = ""
    if slack_email:
        owner_id = find_hubspot_owner_id(slack_email, slack_real_name)

    try:
        # ── Step 3: Create contact, company, deal ──
        contact_id = create_or_find_contact(email, first_name, last_name)

        if owner_id:
            requests.patch(
                f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}",
                json={"properties": {"hubspot_owner_id": owner_id}},
                headers=hubspot_headers(),
            )

        company_id = create_or_find_company(company_name, company_url)
        associate_contact_to_company(contact_id, company_id)
        deal_id = create_deal(deal_name, contact_id, company_id, amount, close_date, owner_id)

        # ── Step 4: FORCE set owner with a separate PATCH (belt AND suspenders) ──
        if owner_id:
            force_set_deal_owner(deal_id, owner_id)

        msg = (
            f"✅ *Demo Booked* created in HubSpot!\n\n"
            f"• *Deal:* {deal_name}\n"
            f"• *Contact:* {full_name} ({email})\n"
            f"• *Company:* {company_name}\n"
            f"• *Stage:* Demo Booked\n"
        )
        if owner_id:
            msg += f"• *Owner:* Assigned to you\n"
        else:
            msg += f"• *Owner:* ⚠️ Could not auto-assign — please set manually in HubSpot\n"
        if amount:
            msg += f"• *Amount:* ${amount}\n"
        if close_date:
            msg += f"• *Close Date:* {close_date}\n"
        msg += f"\n<https://app.hubspot.com/contacts/{HUBSPOT_ACCOUNT_ID}/record/0-3/{deal_id}|View Deal in HubSpot>"

        client.chat_postMessage(channel=user_id, text=msg)

    except Exception as e:
        logger.error(f"HubSpot error: {e}", exc_info=True)
        client.chat_postMessage(
            channel=user_id,
            text=f"❌ Failed to create demo in HubSpot:\n```{str(e)}```\nPlease check the logs or try again.",
        )


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND 2: /deal-update
# ══════════════════════════════════════════════════════════════════════════════

@app.command("/deal-update")
def open_deal_update_form(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "deal_update_search",
            "title": {"type": "plain_text", "text": "Update Deal Stage"},
            "submit": {"type": "plain_text", "text": "Search"},
            "blocks": [{"type": "input", "block_id": "deal_search_block", "label": {"type": "plain_text", "text": "Search for a deal (company name)"}, "element": {"type": "plain_text_input", "action_id": "deal_search", "placeholder": {"type": "plain_text", "text": "e.g. Acme"}}}],
        },
    )


@app.view("deal_update_search")
def handle_deal_search(ack, body, client, view):
    query = view["state"]["values"]["deal_search_block"]["deal_search"]["value"].strip()
    ack()
    user_id = body["user"]["id"]
    deals = search_deals_by_name(query)
    if not deals:
        client.chat_postMessage(channel=user_id, text=f"No deals found matching *{query}* in Sales Pipeline New.")
        return
    deal_options = [{"text": {"type": "plain_text", "text": f"{d['name']} ({d['stage']})"[:75]}, "value": d["id"]} for d in deals]
    stage_options = [{"text": {"type": "plain_text", "text": name}, "value": sid} for name, sid in STAGES.items()]
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal", "callback_id": "deal_update_submit",
            "title": {"type": "plain_text", "text": "Update Deal Stage"},
            "submit": {"type": "plain_text", "text": "Update"},
            "blocks": [
                {"type": "input", "block_id": "deal_pick_block", "label": {"type": "plain_text", "text": "Select Deal"}, "element": {"type": "static_select", "action_id": "deal_pick", "options": deal_options}},
                {"type": "input", "block_id": "stage_pick_block", "label": {"type": "plain_text", "text": "Move to Stage"}, "element": {"type": "static_select", "action_id": "stage_pick", "options": stage_options}},
            ],
        },
    )


@app.view("deal_update_submit")
def handle_deal_update(ack, body, client, view):
    ack()
    vals = view["state"]["values"]
    deal_id = vals["deal_pick_block"]["deal_pick"]["selected_option"]["value"]
    deal_label = vals["deal_pick_block"]["deal_pick"]["selected_option"]["text"]["text"]
    new_stage_id = vals["stage_pick_block"]["stage_pick"]["selected_option"]["value"]
    new_stage_name = vals["stage_pick_block"]["stage_pick"]["selected_option"]["text"]["text"]
    user_id = body["user"]["id"]
    try:
        resp = requests.patch(f"{HUBSPOT_BASE}/crm/v3/objects/deals/{deal_id}", json={"properties": {"dealstage": new_stage_id}}, headers=hubspot_headers())
        resp.raise_for_status()
        client.chat_postMessage(channel=user_id, text=f"✅ Deal updated!\n\n• *Deal:* {deal_label}\n• *New Stage:* {new_stage_name}\n\n<https://app.hubspot.com/contacts/{HUBSPOT_ACCOUNT_ID}/record/0-3/{deal_id}|View Deal in HubSpot>")
    except Exception as e:
        logger.error(f"Deal update error: {e}")
        client.chat_postMessage(channel=user_id, text=f"❌ Failed to update deal:\n```{str(e)}```")


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND 3: /log-note
# ══════════════════════════════════════════════════════════════════════════════

@app.command("/log-note")
def open_log_note_form(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal", "callback_id": "log_note_search",
            "title": {"type": "plain_text", "text": "Log a Note"},
            "submit": {"type": "plain_text", "text": "Search"},
            "blocks": [{"type": "input", "block_id": "note_deal_search_block", "label": {"type": "plain_text", "text": "Search for a deal (company name)"}, "element": {"type": "plain_text_input", "action_id": "note_deal_search", "placeholder": {"type": "plain_text", "text": "e.g. Acme"}}}],
        },
    )


@app.view("log_note_search")
def handle_note_search(ack, body, client, view):
    query = view["state"]["values"]["note_deal_search_block"]["note_deal_search"]["value"].strip()
    ack()
    user_id = body["user"]["id"]
    deals = search_deals_by_name(query, all_pipelines=True)
    if not deals:
        client.chat_postMessage(channel=user_id, text=f"No deals found matching *{query}*.")
        return
    deal_options = [{"text": {"type": "plain_text", "text": f"{d['name']} ({d['stage']})"[:75]}, "value": d["id"]} for d in deals]
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal", "callback_id": "log_note_submit",
            "title": {"type": "plain_text", "text": "Log a Note"},
            "submit": {"type": "plain_text", "text": "Save Note"},
            "blocks": [
                {"type": "input", "block_id": "note_deal_pick_block", "label": {"type": "plain_text", "text": "Select Deal"}, "element": {"type": "static_select", "action_id": "note_deal_pick", "options": deal_options}},
                {"type": "input", "block_id": "note_body_block", "label": {"type": "plain_text", "text": "Note"}, "element": {"type": "plain_text_input", "action_id": "note_body", "multiline": True, "placeholder": {"type": "plain_text", "text": "Type your note here..."}}},
            ],
        },
    )


@app.view("log_note_submit")
def handle_log_note(ack, body, client, view):
    ack()
    vals = view["state"]["values"]
    deal_id = vals["note_deal_pick_block"]["note_deal_pick"]["selected_option"]["value"]
    deal_label = vals["note_deal_pick_block"]["note_deal_pick"]["selected_option"]["text"]["text"]
    note_text = vals["note_body_block"]["note_body"]["value"].strip()
    user_id = body["user"]["id"]
    try:
        resp = requests.post(f"{HUBSPOT_BASE}/crm/v3/objects/notes", json={
            "properties": {"hs_note_body": note_text, "hs_timestamp": str(int(time.time() * 1000))},
            "associations": [{"to": {"id": deal_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 214}]}],
        }, headers=hubspot_headers())
        resp.raise_for_status()
        client.chat_postMessage(channel=user_id, text=f"✅ Note added to *{deal_label}*!\n\n_{note_text[:200]}_\n\n<https://app.hubspot.com/contacts/{HUBSPOT_ACCOUNT_ID}/record/0-3/{deal_id}|View Deal in HubSpot>")
    except Exception as e:
        logger.error(f"Log note error: {e}")
        client.chat_postMessage(channel=user_id, text=f"❌ Failed to add note:\n```{str(e)}```")


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND 4: /won
# ══════════════════════════════════════════════════════════════════════════════

@app.command("/won")
def open_won_form(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal", "callback_id": "won_search",
            "title": {"type": "plain_text", "text": "Close Deal - Won"},
            "submit": {"type": "plain_text", "text": "Search"},
            "blocks": [{"type": "input", "block_id": "won_search_block", "label": {"type": "plain_text", "text": "Search for a deal (company name)"}, "element": {"type": "plain_text_input", "action_id": "won_search", "placeholder": {"type": "plain_text", "text": "e.g. Acme"}}}],
        },
    )


@app.view("won_search")
def handle_won_search(ack, body, client, view):
    query = view["state"]["values"]["won_search_block"]["won_search"]["value"].strip()
    ack()
    user_id = body["user"]["id"]
    deals = search_deals_by_name(query)
    if not deals:
        client.chat_postMessage(channel=user_id, text=f"No deals found matching *{query}*. Try `/won` again.")
        return
    deal_options = [{"text": {"type": "plain_text", "text": f"{d['name']} ({d['stage']})"[:75]}, "value": d["id"]} for d in deals]
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal", "callback_id": "won_submit",
            "title": {"type": "plain_text", "text": "Close Deal - Won"},
            "submit": {"type": "plain_text", "text": "Mark as Won"},
            "blocks": [{"type": "input", "block_id": "won_deal_block", "label": {"type": "plain_text", "text": "Select Deal"}, "element": {"type": "static_select", "action_id": "won_deal", "options": deal_options}}],
        },
    )


@app.view("won_submit")
def handle_won(ack, body, client, view):
    ack()
    deal_id = view["state"]["values"]["won_deal_block"]["won_deal"]["selected_option"]["value"]
    deal_label = view["state"]["values"]["won_deal_block"]["won_deal"]["selected_option"]["text"]["text"]
    user_id = body["user"]["id"]
    try:
        resp = requests.patch(f"{HUBSPOT_BASE}/crm/v3/objects/deals/{deal_id}", json={"properties": {"dealstage": STAGES["Closed Won"]}}, headers=hubspot_headers())
        resp.raise_for_status()
        client.chat_postMessage(channel=user_id, text=f"🎉 *Deal Won!*\n\n• *Deal:* {deal_label}\n• *Stage:* Closed Won\n\n<https://app.hubspot.com/contacts/{HUBSPOT_ACCOUNT_ID}/record/0-3/{deal_id}|View Deal in HubSpot>")
    except Exception as e:
        logger.error(f"Won error: {e}")
        client.chat_postMessage(channel=user_id, text=f"❌ Failed to mark deal as won:\n```{str(e)}```")


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND 5: /lost
# ══════════════════════════════════════════════════════════════════════════════

@app.command("/lost")
def open_lost_form(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal", "callback_id": "lost_search",
            "title": {"type": "plain_text", "text": "Close Deal - Lost"},
            "submit": {"type": "plain_text", "text": "Search"},
            "blocks": [{"type": "input", "block_id": "lost_search_block", "label": {"type": "plain_text", "text": "Search for a deal (company name)"}, "element": {"type": "plain_text_input", "action_id": "lost_search", "placeholder": {"type": "plain_text", "text": "e.g. Acme"}}}],
        },
    )


@app.view("lost_search")
def handle_lost_search(ack, body, client, view):
    query = view["state"]["values"]["lost_search_block"]["lost_search"]["value"].strip()
    ack()
    user_id = body["user"]["id"]
    deals = search_deals_by_name(query)
    if not deals:
        client.chat_postMessage(channel=user_id, text=f"No deals found matching *{query}*. Try `/lost` again.")
        return
    deal_options = [{"text": {"type": "plain_text", "text": f"{d['name']} ({d['stage']})"[:75]}, "value": d["id"]} for d in deals]
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal", "callback_id": "lost_submit",
            "title": {"type": "plain_text", "text": "Close Deal - Lost"},
            "submit": {"type": "plain_text", "text": "Mark as Lost"},
            "blocks": [
                {"type": "input", "block_id": "lost_deal_block", "label": {"type": "plain_text", "text": "Select Deal"}, "element": {"type": "static_select", "action_id": "lost_deal", "options": deal_options}},
                {"type": "input", "block_id": "lost_reason_block", "label": {"type": "plain_text", "text": "Reason for losing"}, "element": {"type": "plain_text_input", "action_id": "lost_reason", "multiline": True, "placeholder": {"type": "plain_text", "text": "e.g. Went with a competitor, budget cut, etc."}}},
            ],
        },
    )


@app.view("lost_submit")
def handle_lost(ack, body, client, view):
    ack()
    vals = view["state"]["values"]
    deal_id = vals["lost_deal_block"]["lost_deal"]["selected_option"]["value"]
    deal_label = vals["lost_deal_block"]["lost_deal"]["selected_option"]["text"]["text"]
    reason = vals["lost_reason_block"]["lost_reason"]["value"].strip()
    user_id = body["user"]["id"]
    try:
        resp = requests.patch(f"{HUBSPOT_BASE}/crm/v3/objects/deals/{deal_id}", json={"properties": {"dealstage": STAGES["Closed Lost"], "closed_lost_reason": reason}}, headers=hubspot_headers())
        resp.raise_for_status()
        requests.post(f"{HUBSPOT_BASE}/crm/v3/objects/notes", json={
            "properties": {"hs_note_body": f"Closed Lost Reason: {reason}", "hs_timestamp": str(int(time.time() * 1000))},
            "associations": [{"to": {"id": deal_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 214}]}],
        }, headers=hubspot_headers())
        client.chat_postMessage(channel=user_id, text=f"❌ *Deal Lost*\n\n• *Deal:* {deal_label}\n• *Reason:* {reason}\n\n<https://app.hubspot.com/contacts/{HUBSPOT_ACCOUNT_ID}/record/0-3/{deal_id}|View Deal in HubSpot>")
    except Exception as e:
        logger.error(f"Lost error: {e}")
        client.chat_postMessage(channel=user_id, text=f"❌ Failed to mark deal as lost:\n```{str(e)}```")


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND 6: /tldr
# ══════════════════════════════════════════════════════════════════════════════

def get_deal_details(deal_id: str) -> dict:
    url = f"{HUBSPOT_BASE}/crm/v3/objects/deals/{deal_id}"
    params = {"properties": "dealname,dealstage,pipeline,amount,closedate,hubspot_owner_id,notes_last_contacted,notes_last_updated,num_contacted_notes,createdate,hs_deal_stage_probability"}
    resp = requests.get(url, headers=hubspot_headers(), params=params)
    resp.raise_for_status()
    return resp.json()


def get_deal_notes(deal_id: str) -> list:
    return get_deal_notes_via_associations(deal_id)


def get_deal_notes_via_associations(deal_id: str) -> list:
    url = f"{HUBSPOT_BASE}/crm/v4/objects/deals/{deal_id}/associations/notes"
    resp = requests.get(url, headers=hubspot_headers())
    if resp.status_code != 200:
        return []
    note_ids = [r["toObjectId"] for r in resp.json().get("results", [])][:10]
    notes = []
    for nid in note_ids:
        nresp = requests.get(f"{HUBSPOT_BASE}/crm/v3/objects/notes/{nid}", headers=hubspot_headers(), params={"properties": "hs_note_body"})
        if nresp.status_code == 200:
            body = nresp.json().get("properties", {}).get("hs_note_body", "")
            if body:
                notes.append(body)
    return notes


def get_deal_emails(deal_id: str) -> list:
    url = f"{HUBSPOT_BASE}/crm/v4/objects/deals/{deal_id}/associations/emails"
    resp = requests.get(url, headers=hubspot_headers())
    if resp.status_code != 200:
        return []
    email_ids = [r["toObjectId"] for r in resp.json().get("results", [])][:5]
    emails = []
    for eid in email_ids:
        eresp = requests.get(f"{HUBSPOT_BASE}/crm/v3/objects/emails/{eid}", headers=hubspot_headers(), params={"properties": "hs_email_subject,hs_email_text,hs_timestamp"})
        if eresp.status_code == 200:
            props = eresp.json().get("properties", {})
            subject = props.get("hs_email_subject", "")
            body = props.get("hs_email_text", "")
            if subject or body:
                emails.append(f"Subject: {subject}\n{body[:300]}")
    return emails


def get_deal_meetings(deal_id: str) -> list:
    url = f"{HUBSPOT_BASE}/crm/v4/objects/deals/{deal_id}/associations/meetings"
    resp = requests.get(url, headers=hubspot_headers())
    if resp.status_code != 200:
        return []
    meeting_ids = [r["toObjectId"] for r in resp.json().get("results", [])][:5]
    meetings = []
    for mid in meeting_ids:
        mresp = requests.get(f"{HUBSPOT_BASE}/crm/v3/objects/meetings/{mid}", headers=hubspot_headers(), params={"properties": "hs_meeting_title,hs_meeting_body,hs_meeting_start_time"})
        if mresp.status_code == 200:
            props = mresp.json().get("properties", {})
            title = props.get("hs_meeting_title", "")
            body = props.get("hs_meeting_body", "")
            start = props.get("hs_meeting_start_time", "")
            if title or body:
                meetings.append(f"Meeting: {title} ({start})\n{body[:300]}")
    return meetings


def generate_tldr(deal_info: dict, notes: list, emails: list, meetings: list) -> str:
    stage_id_to_name = {v: k for k, v in STAGES.items()}
    props = deal_info.get("properties", {})
    stage_name = stage_id_to_name.get(props.get("dealstage", ""), "Unknown")
    context = f"""Deal: {props.get('dealname', 'Unknown')}
Stage: {stage_name}
Amount: ${props.get('amount', 'Not set') or 'Not set'}
Close Date: {props.get('closedate', 'Not set') or 'Not set'}
Created: {props.get('createdate', 'Unknown')}
Last Contacted: {props.get('notes_last_contacted', 'Never') or 'Never'}
Last Activity: {props.get('notes_last_updated', 'Never') or 'Never'}

"""
    if notes:
        context += "RECENT NOTES:\n"
        for i, note in enumerate(notes[:5], 1):
            clean = re.sub(r'<[^>]+>', '', note.replace("<p>", "").replace("</p>", "\n").replace("<br>", "\n"))
            context += f"{i}. {clean[:500]}\n\n"
    if emails:
        context += "RECENT EMAILS:\n"
        for i, em in enumerate(emails[:3], 1):
            context += f"{i}. {em}\n\n"
    if meetings:
        context += "RECENT MEETINGS:\n"
        for i, mt in enumerate(meetings[:3], 1):
            context += f"{i}. {mt}\n\n"
    if not notes and not emails and not meetings:
        context += "No recent activity found on this deal.\n"

    ai = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = ai.messages.create(
        model="claude-sonnet-4-20250514", max_tokens=500,
        messages=[{"role": "user", "content": f"You are a sales assistant. Give a brief TLDR summary of this deal's progress in 3-5 sentences. Be specific about what's happened recently and what the current status is. Keep it conversational and useful for a sales team. If there's no recent activity, mention that too.\n\n{context}"}],
    )
    return message.content[0].text


@app.command("/tldr")
def open_tldr_form(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal", "callback_id": "tldr_search",
            "title": {"type": "plain_text", "text": "Deal TLDR"},
            "submit": {"type": "plain_text", "text": "Search"},
            "blocks": [{"type": "input", "block_id": "tldr_search_block", "label": {"type": "plain_text", "text": "Search for a deal (company name)"}, "element": {"type": "plain_text_input", "action_id": "tldr_search", "placeholder": {"type": "plain_text", "text": "e.g. Acme"}}}],
        },
    )


@app.view("tldr_search")
def handle_tldr_search(ack, body, client, view):
    query = view["state"]["values"]["tldr_search_block"]["tldr_search"]["value"].strip()
    ack()
    user_id = body["user"]["id"]
    deals = search_deals_by_name(query, all_pipelines=True)
    if not deals:
        client.chat_postMessage(channel=user_id, text=f"No deals found matching *{query}*. Try `/tldr` again.")
        return
    deal_options = [{"text": {"type": "plain_text", "text": f"{d['name']} ({d['stage']})"[:75]}, "value": d["id"]} for d in deals]
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal", "callback_id": "tldr_submit",
            "title": {"type": "plain_text", "text": "Deal TLDR"},
            "submit": {"type": "plain_text", "text": "Get Summary"},
            "blocks": [{"type": "input", "block_id": "tldr_deal_block", "label": {"type": "plain_text", "text": "Select Deal"}, "element": {"type": "static_select", "action_id": "tldr_deal", "options": deal_options}}],
        },
    )


@app.view("tldr_submit")
def handle_tldr(ack, body, client, view):
    ack()
    deal_id = view["state"]["values"]["tldr_deal_block"]["tldr_deal"]["selected_option"]["value"]
    deal_label = view["state"]["values"]["tldr_deal_block"]["tldr_deal"]["selected_option"]["text"]["text"]
    user_id = body["user"]["id"]
    client.chat_postMessage(channel=user_id, text=f"🔍 Pulling data for *{deal_label}*... one sec.")
    try:
        deal_info = get_deal_details(deal_id)
        notes = get_deal_notes(deal_id)
        emails = get_deal_emails(deal_id)
        meetings = get_deal_meetings(deal_id)
        summary = generate_tldr(deal_info, notes, emails, meetings)
        stage_id_to_name = {v: k for k, v in STAGES.items()}
        props = deal_info.get("properties", {})
        stage_name = stage_id_to_name.get(props.get("dealstage", ""), "Unknown")
        msg = f"📋 *TLDR: {deal_label}*\n\n*Stage:* {stage_name}\n"
        if props.get("amount"):
            msg += f"*Amount:* ${props['amount']}\n"
        if props.get("notes_last_contacted"):
            msg += f"*Last Contacted:* {props['notes_last_contacted'][:10]}\n"
        msg += f"\n---\n\n{summary}\n\n<https://app.hubspot.com/contacts/{HUBSPOT_ACCOUNT_ID}/record/0-3/{deal_id}|View Deal in HubSpot>"
        client.chat_postMessage(channel=user_id, text=msg)
    except Exception as e:
        logger.error(f"TLDR error: {e}")
        client.chat_postMessage(channel=user_id, text=f"❌ Failed to generate TLDR:\n```{str(e)}```")


# ── Daily Pipeline Recap ─────────────────────────────────────────────────────

def get_all_deals_in_pipeline():
    all_deals = []
    after = None
    while True:
        body = {
            "filterGroups": [{"filters": [{"propertyName": "pipeline", "operator": "EQ", "value": HUBSPOT_PIPELINE_ID}]}],
            "properties": ["dealname", "dealstage", "amount", "closedate", "hubspot_owner_id", "createdate", "notes_last_contacted", "hs_lastmodifieddate"],
            "limit": 100,
        }
        if after:
            body["after"] = after
        resp = requests.post(f"{HUBSPOT_BASE}/crm/v3/objects/deals/search", json=body, headers=hubspot_headers())
        resp.raise_for_status()
        data = resp.json()
        all_deals.extend(data.get("results", []))
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return all_deals


def build_pipeline_recap():
    stage_id_to_name = {v: k for k, v in STAGES.items()}
    deals = get_all_deals_in_pipeline()
    if not deals:
        return "📊 *Daily Pipeline Recap*\n\nNo deals found in Sales Pipeline New."

    by_stage = {}
    for deal in deals:
        props = deal.get("properties", {})
        stage_name = stage_id_to_name.get(props.get("dealstage", ""), "Unknown")
        by_stage.setdefault(stage_name, []).append(props)

    total_deals = len(deals)
    total_value = sum(float(d.get("properties", {}).get("amount", 0) or 0) for d in deals)

    today = datetime.utcnow().date()
    end_of_week = today + timedelta(days=(6 - today.weekday()))
    closing_this_week = []
    for deal in deals:
        cs = deal.get("properties", {}).get("closedate", "")
        if cs:
            try:
                cd = datetime.fromisoformat(cs.replace("Z", "+00:00")).date()
                if today <= cd <= end_of_week:
                    closing_this_week.append(deal.get("properties", {}))
            except (ValueError, TypeError):
                pass

    stale_deals = []
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    for deal in deals:
        props = deal.get("properties", {})
        sn = stage_id_to_name.get(props.get("dealstage", ""), "")
        if sn in ("Closed Won", "Closed Lost", "Parked"):
            continue
        lm = props.get("hs_lastmodifieddate", "")
        if lm:
            try:
                if datetime.fromisoformat(lm.replace("Z", "+00:00")).replace(tzinfo=None) < seven_days_ago:
                    stale_deals.append(props)
            except (ValueError, TypeError):
                pass

    yesterday = datetime.utcnow() - timedelta(hours=24)
    new_deals = []
    for deal in deals:
        cr = deal.get("properties", {}).get("createdate", "")
        if cr:
            try:
                if datetime.fromisoformat(cr.replace("Z", "+00:00")).replace(tzinfo=None) >= yesterday:
                    new_deals.append(deal.get("properties", {}))
            except (ValueError, TypeError):
                pass

    msg = f"📊 *Daily Pipeline Recap — {today.strftime('%A, %B %d')}*\n\n"
    msg += f"*{total_deals}* deals in pipeline  •  *${total_value:,.0f}* total value  •  *{len(closing_this_week)}* closing this week\n\n"
    msg += "━━━━━━━━━━━━━━━━━━━━━━━\n*Deals by Stage*\n"
    for sn in STAGES:
        ds = by_stage.get(sn, [])
        if not ds:
            continue
        sv = sum(float(d.get("amount", 0) or 0) for d in ds)
        msg += f"  {sn}: *{len(ds)}* deal{'s' if len(ds) != 1 else ''}  (${sv:,.0f})  {'█' * min(len(ds), 15)}\n"
    msg += "\n"

    if closing_this_week:
        msg += "━━━━━━━━━━━━━━━━━━━━━━━\n📅 *Closing This Week*\n"
        for d in closing_this_week:
            amt = d.get("amount", "")
            msg += f"  • {d.get('dealname', 'Unknown')}{f' — ${float(amt):,.0f}' if amt else ''} (close: {(d.get('closedate', '') or '')[:10]})\n"
        msg += "\n"

    if new_deals:
        msg += "━━━━━━━━━━━━━━━━━━━━━━━\n🆕 *New Deals (Last 24h)*\n"
        for d in new_deals:
            amt = d.get("amount", "")
            msg += f"  • {d.get('dealname', 'Unknown')}{f' — ${float(amt):,.0f}' if amt else ''}\n"
        msg += "\n"

    if stale_deals:
        msg += "━━━━━━━━━━━━━━━━━━━━━━━\n⚠️ *Needs Attention (No activity in 7+ days)*\n"
        for d in stale_deals[:10]:
            msg += f"  • {d.get('dealname', 'Unknown')} — stuck in _{stage_id_to_name.get(d.get('dealstage', ''), 'Unknown')}_\n"
        if len(stale_deals) > 10:
            msg += f"  _...and {len(stale_deals) - 10} more_\n"
        msg += "\n"

    msg += f"_View full pipeline: <https://app.hubspot.com/contacts/{HUBSPOT_ACCOUNT_ID}/objects/0-3/views/all/board|Open HubSpot>_"
    return msg


def post_daily_recap():
    if not RECAP_CHANNEL_ID:
        return
    try:
        from slack_sdk import WebClient
        cl = WebClient(token=SLACK_BOT_TOKEN)
        cl.chat_postMessage(channel=RECAP_CHANNEL_ID, text=build_pipeline_recap())
        logger.info(f"Daily recap posted to {RECAP_CHANNEL_ID}")
    except Exception as e:
        logger.error(f"Failed to post daily recap: {e}")


@app.command("/pipeline-recap")
def handle_pipeline_recap(ack, body, client):
    ack()
    uid = body["user_id"]
    client.chat_postMessage(channel=uid, text="📊 Generating pipeline recap... one sec.")
    try:
        client.chat_postMessage(channel=uid, text=build_pipeline_recap())
    except Exception as e:
        logger.error(f"Pipeline recap error: {e}")
        client.chat_postMessage(channel=uid, text=f"❌ Failed to generate recap:\n```{str(e)}```")


# ── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if RECAP_CHANNEL_ID:
        scheduler = BackgroundScheduler()
        scheduler.add_job(post_daily_recap, trigger=CronTrigger(hour=RECAP_HOUR, minute=RECAP_MINUTE, timezone="America/New_York"), id="daily_recap", name="Daily Pipeline Recap")
        scheduler.start()
        print(f"📅 Daily recap scheduled for {RECAP_HOUR}:{RECAP_MINUTE:02d} AM ET → channel {RECAP_CHANNEL_ID}")
    else:
        print("⚠️  RECAP_CHANNEL_ID not set — daily recap is disabled.")

    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    print("⚡ Sapien Sales Bot is running!")
    handler.start()
