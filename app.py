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
"""

import os
import logging
import requests
import anthropic
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# ── Config ───────────────────────────────────────────────────────────────────
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]  # xapp-... token for Socket Mode
HUBSPOT_API_KEY = os.environ["HUBSPOT_API_KEY"]  # Private app access token

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

HUBSPOT_BASE = "https://api.hubapi.com"
HUBSPOT_ACCOUNT_ID = "46061347"

# ── Pipeline & Stages (Sales Pipeline New) ───────────────────────────────────
# To update: change the IDs below to match your HubSpot pipeline.
HUBSPOT_PIPELINE_ID = "876727395"

STAGES = {
    # Pre-sale
    "Demo Booked":          "1315454373",
    "Demo Completed":       "1315454374",
    "Qualified (NDA Sent)": "1315454375",
    "NDA Signed":           "1315454376",
    # Value Justification
    "POC in Progress":      "1315454377",
    "POC Value Proven":     "1315454378",
    # Pilot
    "Pilot Contract Sent":  "1315454379",
    "Pilot Company Setup":  "1315574147",
    "Pilot Kick-Off":       "1315574148",
    "Pilot Period":         "1315574149",
    "Pilot Read Out":       "1315574150",
    # Commercialization
    "Proposal/Pricing":     "1315574151",
    "Security/Procurement": "1315574152",
    "Verbal Commit":        "1315574153",
    "Closed Won":           "1315574154",
    # Hold / Terminal
    "Parked":               "1315574155",
    "Closed Lost":          "1315574156",
}

HUBSPOT_STAGE_ID = STAGES["Demo Booked"]  # Default stage for /demo-booked

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = App(token=SLACK_BOT_TOKEN)


# ── Helpers: HubSpot API ─────────────────────────────────────────────────────

def hubspot_headers():
    return {
        "Authorization": f"Bearer {HUBSPOT_API_KEY}",
        "Content-Type": "application/json",
    }


def create_or_find_contact(email: str, first_name: str, last_name: str) -> str:
    """Create a contact in HubSpot. If the email already exists, return the existing ID."""
    search_url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search"
    search_body = {
        "filterGroups": [{
            "filters": [{
                "propertyName": "email",
                "operator": "EQ",
                "value": email,
            }]
        }]
    }
    resp = requests.post(search_url, json=search_body, headers=hubspot_headers())
    resp.raise_for_status()
    results = resp.json().get("results", [])

    if results:
        contact_id = results[0]["id"]
        logger.info(f"Found existing contact {contact_id} for {email}")
        return contact_id

    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts"
    body = {
        "properties": {
            "email": email,
            "firstname": first_name,
            "lastname": last_name,
        }
    }
    resp = requests.post(url, json=body, headers=hubspot_headers())
    resp.raise_for_status()
    contact_id = resp.json()["id"]
    logger.info(f"Created contact {contact_id} for {email}")
    return contact_id


def create_or_find_company(company_name: str, company_url: str = "") -> str:
    """Create a company in HubSpot. If the name already exists, return the existing ID."""
    search_url = f"{HUBSPOT_BASE}/crm/v3/objects/companies/search"
    search_body = {
        "filterGroups": [{
            "filters": [{
                "propertyName": "name",
                "operator": "EQ",
                "value": company_name,
            }]
        }]
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
    body = {"properties": properties}
    resp = requests.post(url, json=body, headers=hubspot_headers())
    resp.raise_for_status()
    company_id = resp.json()["id"]
    logger.info(f"Created company {company_id} for {company_name}")
    return company_id


def find_hubspot_owner_by_email(email: str) -> str:
    """Look up a HubSpot owner ID by their email address."""
    url = f"{HUBSPOT_BASE}/crm/v3/owners"
    resp = requests.get(url, headers=hubspot_headers(), params={"limit": 100, "email": email})
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if results:
        owner_id = results[0]["id"]
        logger.info(f"Found HubSpot owner {owner_id} for {email}")
        return owner_id
    resp = requests.get(url, headers=hubspot_headers(), params={"limit": 100})
    resp.raise_for_status()
    for owner in resp.json().get("results", []):
        if owner.get("email", "").lower() == email.lower():
            logger.info(f"Found HubSpot owner {owner['id']} for {email}")
            return owner["id"]
    logger.warning(f"No HubSpot owner found for {email}")
    return ""


def create_deal(deal_name: str, contact_id: str, company_id: str, amount: str = "", close_date: str = "", owner_id: str = "") -> str:
    """Create a deal in the Sales Pipeline New at Demo Booked stage."""
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
            {
                "to": {"id": contact_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 3}],
            },
            {
                "to": {"id": company_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 5}],
            },
        ],
    }
    resp = requests.post(url, json=body, headers=hubspot_headers())
    resp.raise_for_status()
    deal_id = resp.json()["id"]
    logger.info(f"Created deal {deal_id}: {deal_name}")
    return deal_id


def associate_contact_to_company(contact_id: str, company_id: str):
    """Associate a contact with a company in HubSpot."""
    url = (
        f"{HUBSPOT_BASE}/crm/v4/objects/contacts/{contact_id}"
        f"/associations/companies/{company_id}"
    )
    body = [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 1}]
    resp = requests.put(url, json=body, headers=hubspot_headers())
    resp.raise_for_status()
    logger.info(f"Associated contact {contact_id} with company {company_id}")


def search_deals_by_name(query: str, all_pipelines: bool = False) -> list:
    """Search for deals by name. If all_pipelines=False, only searches Sales Pipeline New."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/deals/search"
    body = {
        "query": query,
        "properties": ["dealname", "dealstage", "pipeline", "amount"],
        "limit": 20,
    }
    if not all_pipelines:
        body["filterGroups"] = [{
            "filters": [{
                "propertyName": "pipeline",
                "operator": "EQ",
                "value": HUBSPOT_PIPELINE_ID,
            }]
        }]
    resp = requests.post(url, json=body, headers=hubspot_headers())
    resp.raise_for_status()
    results = resp.json().get("results", [])
    # Build reverse stage map for display
    stage_id_to_name = {v: k for k, v in STAGES.items()}
    deals = []
    for r in results:
        props = r.get("properties", {})
        stage_id = props.get("dealstage", "")
        deals.append({
            "id": r["id"],
            "name": props.get("dealname", "Unknown"),
            "stage": stage_id_to_name.get(stage_id, stage_id),
        })
    return deals


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
                {
                    "type": "input",
                    "block_id": "contact_name_block",
                    "label": {"type": "plain_text", "text": "Contact Full Name"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "contact_name",
                        "placeholder": {"type": "plain_text", "text": "e.g. Jane Smith"},
                    },
                },
                {
                    "type": "input",
                    "block_id": "company_block",
                    "label": {"type": "plain_text", "text": "Company Name"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "company_name",
                        "placeholder": {"type": "plain_text", "text": "e.g. Acme Inc"},
                    },
                },
                {
                    "type": "input",
                    "block_id": "email_block",
                    "label": {"type": "plain_text", "text": "Email Address"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "email",
                        "placeholder": {"type": "plain_text", "text": "e.g. jane@acme.com"},
                    },
                },
                {
                    "type": "input",
                    "block_id": "company_url_block",
                    "label": {"type": "plain_text", "text": "Company Website URL"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "company_url",
                        "placeholder": {"type": "plain_text", "text": "e.g. https://www.acme.com"},
                    },
                    "optional": True,
                },
                {
                    "type": "input",
                    "block_id": "amount_block",
                    "label": {"type": "plain_text", "text": "Deal Amount ($)"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "amount",
                        "placeholder": {"type": "plain_text", "text": "e.g. 50000"},
                    },
                    "optional": True,
                },
                {
                    "type": "input",
                    "block_id": "close_date_block",
                    "label": {"type": "plain_text", "text": "Expected Close Date"},
                    "element": {
                        "type": "datepicker",
                        "action_id": "close_date",
                        "placeholder": {"type": "plain_text", "text": "Pick a date"},
                    },
                    "optional": True,
                },
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

    owner_id = ""
    try:
        slack_user_info = client.users_info(user=user_id)
        slack_email = slack_user_info["user"]["profile"].get("email", "")
        if slack_email:
            owner_id = find_hubspot_owner_by_email(slack_email)
    except Exception as e:
        logger.warning(f"Could not look up Slack user email: {e}")

    try:
        contact_id = create_or_find_contact(email, first_name, last_name)

        if owner_id:
            update_url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}"
            requests.patch(update_url, json={"properties": {"hubspot_owner_id": owner_id}}, headers=hubspot_headers())

        company_id = create_or_find_company(company_name, company_url)
        associate_contact_to_company(contact_id, company_id)
        deal_id = create_deal(deal_name, contact_id, company_id, amount, close_date, owner_id)

        msg = (
            f"✅ *Demo Booked* created in HubSpot!\n\n"
            f"• *Deal:* {deal_name}\n"
            f"• *Contact:* {full_name} ({email})\n"
            f"• *Company:* {company_name}\n"
            f"• *Stage:* Demo Booked\n"
        )
        if amount:
            msg += f"• *Amount:* ${amount}\n"
        if close_date:
            msg += f"• *Close Date:* {close_date}\n"
        msg += f"\n<https://app.hubspot.com/contacts/{HUBSPOT_ACCOUNT_ID}/record/0-3/{deal_id}|View Deal in HubSpot>"

        client.chat_postMessage(channel=user_id, text=msg)

    except Exception as e:
        logger.error(f"HubSpot error: {e}")
        client.chat_postMessage(
            channel=user_id,
            text=f"❌ Failed to create demo in HubSpot:\n```{str(e)}```\nPlease check the logs or try again.",
        )


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND 2: /deal-update  →  Move a deal to a different stage
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
            "blocks": [
                {
                    "type": "input",
                    "block_id": "deal_search_block",
                    "label": {"type": "plain_text", "text": "Search for a deal (company name)"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "deal_search",
                        "placeholder": {"type": "plain_text", "text": "e.g. Acme"},
                    },
                },
            ],
        },
    )


@app.view("deal_update_search")
def handle_deal_search(ack, body, client, view):
    """Search for deals, then show a picker with the results + stage dropdown."""
    query = view["state"]["values"]["deal_search_block"]["deal_search"]["value"].strip()
    ack()

    user_id = body["user"]["id"]
    deals = search_deals_by_name(query)

    if not deals:
        client.chat_postMessage(
            channel=user_id,
            text=f"No deals found matching *{query}* in Sales Pipeline New. Try a different search term with `/deal-update`.",
        )
        return

    # Build deal options for the dropdown
    deal_options = [
        {
            "text": {"type": "plain_text", "text": f"{d['name']} ({d['stage']})"[:75]},
            "value": d["id"],
        }
        for d in deals
    ]

    # Build stage options
    stage_options = [
        {"text": {"type": "plain_text", "text": name}, "value": stage_id}
        for name, stage_id in STAGES.items()
    ]

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "deal_update_submit",
            "title": {"type": "plain_text", "text": "Update Deal Stage"},
            "submit": {"type": "plain_text", "text": "Update"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "deal_pick_block",
                    "label": {"type": "plain_text", "text": "Select Deal"},
                    "element": {
                        "type": "static_select",
                        "action_id": "deal_pick",
                        "options": deal_options,
                    },
                },
                {
                    "type": "input",
                    "block_id": "stage_pick_block",
                    "label": {"type": "plain_text", "text": "Move to Stage"},
                    "element": {
                        "type": "static_select",
                        "action_id": "stage_pick",
                        "options": stage_options,
                    },
                },
            ],
        },
    )


@app.view("deal_update_submit")
def handle_deal_update(ack, body, client, view):
    ack()
    values = view["state"]["values"]
    deal_id = values["deal_pick_block"]["deal_pick"]["selected_option"]["value"]
    deal_label = values["deal_pick_block"]["deal_pick"]["selected_option"]["text"]["text"]
    new_stage_id = values["stage_pick_block"]["stage_pick"]["selected_option"]["value"]
    new_stage_name = values["stage_pick_block"]["stage_pick"]["selected_option"]["text"]["text"]

    user_id = body["user"]["id"]

    try:
        url = f"{HUBSPOT_BASE}/crm/v3/objects/deals/{deal_id}"
        resp = requests.patch(url, json={"properties": {"dealstage": new_stage_id}}, headers=hubspot_headers())
        resp.raise_for_status()

        client.chat_postMessage(
            channel=user_id,
            text=(
                f"✅ Deal updated!\n\n"
                f"• *Deal:* {deal_label}\n"
                f"• *New Stage:* {new_stage_name}\n\n"
                f"<https://app.hubspot.com/contacts/{HUBSPOT_ACCOUNT_ID}/record/0-3/{deal_id}|View Deal in HubSpot>"
            ),
        )
    except Exception as e:
        logger.error(f"Deal update error: {e}")
        client.chat_postMessage(
            channel=user_id,
            text=f"❌ Failed to update deal:\n```{str(e)}```",
        )


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND 3: /log-note  →  Add a note to a deal
# ══════════════════════════════════════════════════════════════════════════════

@app.command("/log-note")
def open_log_note_form(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "log_note_search",
            "title": {"type": "plain_text", "text": "Log a Note"},
            "submit": {"type": "plain_text", "text": "Search"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "note_deal_search_block",
                    "label": {"type": "plain_text", "text": "Search for a deal (company name)"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "note_deal_search",
                        "placeholder": {"type": "plain_text", "text": "e.g. Acme"},
                    },
                },
            ],
        },
    )


@app.view("log_note_search")
def handle_note_search(ack, body, client, view):
    query = view["state"]["values"]["note_deal_search_block"]["note_deal_search"]["value"].strip()
    ack()

    user_id = body["user"]["id"]
    deals = search_deals_by_name(query)

    if not deals:
        client.chat_postMessage(
            channel=user_id,
            text=f"No deals found matching *{query}*. Try a different search with `/log-note`.",
        )
        return

    deal_options = [
        {
            "text": {"type": "plain_text", "text": f"{d['name']} ({d['stage']})"[:75]},
            "value": d["id"],
        }
        for d in deals
    ]

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "log_note_submit",
            "title": {"type": "plain_text", "text": "Log a Note"},
            "submit": {"type": "plain_text", "text": "Save Note"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "note_deal_pick_block",
                    "label": {"type": "plain_text", "text": "Select Deal"},
                    "element": {
                        "type": "static_select",
                        "action_id": "note_deal_pick",
                        "options": deal_options,
                    },
                },
                {
                    "type": "input",
                    "block_id": "note_body_block",
                    "label": {"type": "plain_text", "text": "Note"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "note_body",
                        "multiline": True,
                        "placeholder": {"type": "plain_text", "text": "Type your note here..."},
                    },
                },
            ],
        },
    )


@app.view("log_note_submit")
def handle_log_note(ack, body, client, view):
    ack()
    values = view["state"]["values"]
    deal_id = values["note_deal_pick_block"]["note_deal_pick"]["selected_option"]["value"]
    deal_label = values["note_deal_pick_block"]["note_deal_pick"]["selected_option"]["text"]["text"]
    note_text = values["note_body_block"]["note_body"]["value"].strip()

    user_id = body["user"]["id"]

    try:
        # Create the note (engagement)
        url = f"{HUBSPOT_BASE}/crm/v3/objects/notes"
        note_body = {
            "properties": {
                "hs_note_body": note_text,
                "hs_timestamp": str(int(__import__('time').time() * 1000)),
            },
            "associations": [
                {
                    "to": {"id": deal_id},
                    "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 214}],
                }
            ],
        }
        resp = requests.post(url, json=note_body, headers=hubspot_headers())
        resp.raise_for_status()

        client.chat_postMessage(
            channel=user_id,
            text=(
                f"✅ Note added to *{deal_label}*!\n\n"
                f"_{note_text[:200]}_\n\n"
                f"<https://app.hubspot.com/contacts/{HUBSPOT_ACCOUNT_ID}/record/0-3/{deal_id}|View Deal in HubSpot>"
            ),
        )
    except Exception as e:
        logger.error(f"Log note error: {e}")
        client.chat_postMessage(
            channel=user_id,
            text=f"❌ Failed to add note:\n```{str(e)}```",
        )


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND 4: /won  →  Mark a deal as Closed Won
# ══════════════════════════════════════════════════════════════════════════════

@app.command("/won")
def open_won_form(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "won_search",
            "title": {"type": "plain_text", "text": "Close Deal - Won"},
            "submit": {"type": "plain_text", "text": "Search"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "won_search_block",
                    "label": {"type": "plain_text", "text": "Search for a deal (company name)"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "won_search",
                        "placeholder": {"type": "plain_text", "text": "e.g. Acme"},
                    },
                },
            ],
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

    deal_options = [
        {
            "text": {"type": "plain_text", "text": f"{d['name']} ({d['stage']})"[:75]},
            "value": d["id"],
        }
        for d in deals
    ]

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "won_submit",
            "title": {"type": "plain_text", "text": "Close Deal - Won"},
            "submit": {"type": "plain_text", "text": "Mark as Won"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "won_deal_block",
                    "label": {"type": "plain_text", "text": "Select Deal"},
                    "element": {
                        "type": "static_select",
                        "action_id": "won_deal",
                        "options": deal_options,
                    },
                },
            ],
        },
    )


@app.view("won_submit")
def handle_won(ack, body, client, view):
    ack()
    values = view["state"]["values"]
    deal_id = values["won_deal_block"]["won_deal"]["selected_option"]["value"]
    deal_label = values["won_deal_block"]["won_deal"]["selected_option"]["text"]["text"]

    user_id = body["user"]["id"]

    try:
        url = f"{HUBSPOT_BASE}/crm/v3/objects/deals/{deal_id}"
        resp = requests.patch(url, json={"properties": {"dealstage": STAGES["Closed Won"]}}, headers=hubspot_headers())
        resp.raise_for_status()

        client.chat_postMessage(
            channel=user_id,
            text=(
                f"🎉 *Deal Won!*\n\n"
                f"• *Deal:* {deal_label}\n"
                f"• *Stage:* Closed Won\n\n"
                f"<https://app.hubspot.com/contacts/{HUBSPOT_ACCOUNT_ID}/record/0-3/{deal_id}|View Deal in HubSpot>"
            ),
        )
    except Exception as e:
        logger.error(f"Won error: {e}")
        client.chat_postMessage(channel=user_id, text=f"❌ Failed to mark deal as won:\n```{str(e)}```")


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND 5: /lost  →  Mark a deal as Closed Lost (with reason)
# ══════════════════════════════════════════════════════════════════════════════

@app.command("/lost")
def open_lost_form(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "lost_search",
            "title": {"type": "plain_text", "text": "Close Deal - Lost"},
            "submit": {"type": "plain_text", "text": "Search"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "lost_search_block",
                    "label": {"type": "plain_text", "text": "Search for a deal (company name)"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "lost_search",
                        "placeholder": {"type": "plain_text", "text": "e.g. Acme"},
                    },
                },
            ],
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

    deal_options = [
        {
            "text": {"type": "plain_text", "text": f"{d['name']} ({d['stage']})"[:75]},
            "value": d["id"],
        }
        for d in deals
    ]

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "lost_submit",
            "title": {"type": "plain_text", "text": "Close Deal - Lost"},
            "submit": {"type": "plain_text", "text": "Mark as Lost"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "lost_deal_block",
                    "label": {"type": "plain_text", "text": "Select Deal"},
                    "element": {
                        "type": "static_select",
                        "action_id": "lost_deal",
                        "options": deal_options,
                    },
                },
                {
                    "type": "input",
                    "block_id": "lost_reason_block",
                    "label": {"type": "plain_text", "text": "Reason for losing"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "lost_reason",
                        "multiline": True,
                        "placeholder": {"type": "plain_text", "text": "e.g. Went with a competitor, budget cut, etc."},
                    },
                },
            ],
        },
    )


@app.view("lost_submit")
def handle_lost(ack, body, client, view):
    ack()
    values = view["state"]["values"]
    deal_id = values["lost_deal_block"]["lost_deal"]["selected_option"]["value"]
    deal_label = values["lost_deal_block"]["lost_deal"]["selected_option"]["text"]["text"]
    reason = values["lost_reason_block"]["lost_reason"]["value"].strip()

    user_id = body["user"]["id"]

    try:
        # Update the deal stage to Closed Lost
        url = f"{HUBSPOT_BASE}/crm/v3/objects/deals/{deal_id}"
        resp = requests.patch(
            url,
            json={"properties": {
                "dealstage": STAGES["Closed Lost"],
                "closed_lost_reason": reason,
            }},
            headers=hubspot_headers(),
        )
        resp.raise_for_status()

        # Also log the reason as a note so it's visible in the timeline
        note_url = f"{HUBSPOT_BASE}/crm/v3/objects/notes"
        note_body = {
            "properties": {
                "hs_note_body": f"Closed Lost Reason: {reason}",
                "hs_timestamp": str(int(__import__('time').time() * 1000)),
            },
            "associations": [
                {
                    "to": {"id": deal_id},
                    "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 214}],
                }
            ],
        }
        requests.post(note_url, json=note_body, headers=hubspot_headers())

        client.chat_postMessage(
            channel=user_id,
            text=(
                f"❌ *Deal Lost*\n\n"
                f"• *Deal:* {deal_label}\n"
                f"• *Reason:* {reason}\n\n"
                f"<https://app.hubspot.com/contacts/{HUBSPOT_ACCOUNT_ID}/record/0-3/{deal_id}|View Deal in HubSpot>"
            ),
        )
    except Exception as e:
        logger.error(f"Lost error: {e}")
        client.chat_postMessage(channel=user_id, text=f"❌ Failed to mark deal as lost:\n```{str(e)}```")


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND 6: /tldr  →  AI-powered deal summary
# ══════════════════════════════════════════════════════════════════════════════

def get_deal_details(deal_id: str) -> dict:
    """Fetch full deal details including properties."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/deals/{deal_id}"
    params = {
        "properties": "dealname,dealstage,pipeline,amount,closedate,hubspot_owner_id,"
                       "notes_last_contacted,notes_last_updated,num_contacted_notes,"
                       "createdate,hs_deal_stage_probability",
    }
    resp = requests.get(url, headers=hubspot_headers(), params=params)
    resp.raise_for_status()
    return resp.json()


def get_deal_notes(deal_id: str) -> list:
    """Fetch recent notes associated with a deal."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/notes/search"
    body = {
        "filterGroups": [{
            "filters": [{
                "propertyName": "associations.deal",
                "operator": "EQ",
                "value": deal_id,
            }]
        }],
        "properties": ["hs_note_body", "hs_timestamp", "hs_lastmodifieddate"],
        "sorts": [{"propertyName": "hs_lastmodifieddate", "direction": "DESCENDING"}],
        "limit": 10,
    }
    resp = requests.post(url, json=body, headers=hubspot_headers())
    # If search by association doesn't work, try fetching via associations API
    if resp.status_code != 200:
        return get_deal_notes_via_associations(deal_id)
    results = resp.json().get("results", [])
    return [r.get("properties", {}).get("hs_note_body", "") for r in results if r.get("properties", {}).get("hs_note_body")]


def get_deal_notes_via_associations(deal_id: str) -> list:
    """Fallback: get notes via the associations API then fetch each note."""
    # Get associated note IDs
    url = f"{HUBSPOT_BASE}/crm/v4/objects/deals/{deal_id}/associations/notes"
    resp = requests.get(url, headers=hubspot_headers())
    if resp.status_code != 200:
        return []
    note_ids = [r["toObjectId"] for r in resp.json().get("results", [])][:10]
    if not note_ids:
        return []
    # Fetch each note
    notes = []
    for nid in note_ids:
        nurl = f"{HUBSPOT_BASE}/crm/v3/objects/notes/{nid}"
        nresp = requests.get(nurl, headers=hubspot_headers(), params={"properties": "hs_note_body"})
        if nresp.status_code == 200:
            body = nresp.json().get("properties", {}).get("hs_note_body", "")
            if body:
                notes.append(body)
    return notes


def get_deal_emails(deal_id: str) -> list:
    """Fetch recent emails associated with a deal via associations."""
    url = f"{HUBSPOT_BASE}/crm/v4/objects/deals/{deal_id}/associations/emails"
    resp = requests.get(url, headers=hubspot_headers())
    if resp.status_code != 200:
        return []
    email_ids = [r["toObjectId"] for r in resp.json().get("results", [])][:5]
    if not email_ids:
        return []
    emails = []
    for eid in email_ids:
        eurl = f"{HUBSPOT_BASE}/crm/v3/objects/emails/{eid}"
        eresp = requests.get(eurl, headers=hubspot_headers(), params={"properties": "hs_email_subject,hs_email_text,hs_timestamp"})
        if eresp.status_code == 200:
            props = eresp.json().get("properties", {})
            subject = props.get("hs_email_subject", "")
            body = props.get("hs_email_text", "")
            if subject or body:
                emails.append(f"Subject: {subject}\n{body[:300]}")
    return emails


def get_deal_meetings(deal_id: str) -> list:
    """Fetch recent meetings associated with a deal via associations."""
    url = f"{HUBSPOT_BASE}/crm/v4/objects/deals/{deal_id}/associations/meetings"
    resp = requests.get(url, headers=hubspot_headers())
    if resp.status_code != 200:
        return []
    meeting_ids = [r["toObjectId"] for r in resp.json().get("results", [])][:5]
    if not meeting_ids:
        return []
    meetings = []
    for mid in meeting_ids:
        murl = f"{HUBSPOT_BASE}/crm/v3/objects/meetings/{mid}"
        mresp = requests.get(murl, headers=hubspot_headers(), params={"properties": "hs_meeting_title,hs_meeting_body,hs_meeting_start_time"})
        if mresp.status_code == 200:
            props = mresp.json().get("properties", {})
            title = props.get("hs_meeting_title", "")
            body = props.get("hs_meeting_body", "")
            start = props.get("hs_meeting_start_time", "")
            if title or body:
                meetings.append(f"Meeting: {title} ({start})\n{body[:300]}")
    return meetings


def generate_tldr(deal_info: dict, notes: list, emails: list, meetings: list) -> str:
    """Use Claude to generate a TLDR summary of the deal."""
    stage_id_to_name = {v: k for k, v in STAGES.items()}
    props = deal_info.get("properties", {})

    stage_name = stage_id_to_name.get(props.get("dealstage", ""), "Unknown")
    deal_name = props.get("dealname", "Unknown")
    amount = props.get("amount", "Not set")
    close_date = props.get("closedate", "Not set")
    last_contacted = props.get("notes_last_contacted", "Never")
    last_activity = props.get("notes_last_updated", "Never")
    created = props.get("createdate", "Unknown")

    context = f"""Deal: {deal_name}
Stage: {stage_name}
Amount: ${amount if amount and amount != 'Not set' else 'Not set'}
Close Date: {close_date if close_date else 'Not set'}
Created: {created}
Last Contacted: {last_contacted if last_contacted else 'Never'}
Last Activity: {last_activity if last_activity else 'Never'}

"""

    if notes:
        context += "RECENT NOTES:\n"
        for i, note in enumerate(notes[:5], 1):
            # Strip HTML tags from notes
            clean = note.replace("<p>", "").replace("</p>", "\n").replace("<br>", "\n")
            clean = __import__('re').sub(r'<[^>]+>', '', clean)
            context += f"{i}. {clean[:500]}\n\n"

    if emails:
        context += "RECENT EMAILS:\n"
        for i, email in enumerate(emails[:3], 1):
            context += f"{i}. {email}\n\n"

    if meetings:
        context += "RECENT MEETINGS:\n"
        for i, meeting in enumerate(meetings[:3], 1):
            context += f"{i}. {meeting}\n\n"

    if not notes and not emails and not meetings:
        context += "No recent activity found on this deal.\n"

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": f"""You are a sales assistant. Give a brief TLDR summary of this deal's progress in 3-5 sentences.
Be specific about what's happened recently and what the current status is. Keep it conversational and useful for a sales team.
If there's no recent activity, mention that too.

{context}"""
        }],
    )
    return message.content[0].text


@app.command("/tldr")
def open_tldr_form(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "tldr_search",
            "title": {"type": "plain_text", "text": "Deal TLDR"},
            "submit": {"type": "plain_text", "text": "Search"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "tldr_search_block",
                    "label": {"type": "plain_text", "text": "Search for a deal (company name)"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "tldr_search",
                        "placeholder": {"type": "plain_text", "text": "e.g. Acme"},
                    },
                },
            ],
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

    deal_options = [
        {
            "text": {"type": "plain_text", "text": f"{d['name']} ({d['stage']})"[:75]},
            "value": d["id"],
        }
        for d in deals
    ]

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "tldr_submit",
            "title": {"type": "plain_text", "text": "Deal TLDR"},
            "submit": {"type": "plain_text", "text": "Get Summary"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "tldr_deal_block",
                    "label": {"type": "plain_text", "text": "Select Deal"},
                    "element": {
                        "type": "static_select",
                        "action_id": "tldr_deal",
                        "options": deal_options,
                    },
                },
            ],
        },
    )


@app.view("tldr_submit")
def handle_tldr(ack, body, client, view):
    ack()
    values = view["state"]["values"]
    deal_id = values["tldr_deal_block"]["tldr_deal"]["selected_option"]["value"]
    deal_label = values["tldr_deal_block"]["tldr_deal"]["selected_option"]["text"]["text"]

    user_id = body["user"]["id"]

    # Send a "working on it" message first since this takes a few seconds
    client.chat_postMessage(channel=user_id, text=f"🔍 Pulling data for *{deal_label}*... one sec.")

    try:
        # Gather all deal data
        deal_info = get_deal_details(deal_id)
        notes = get_deal_notes(deal_id)
        emails = get_deal_emails(deal_id)
        meetings = get_deal_meetings(deal_id)

        # Generate AI summary
        summary = generate_tldr(deal_info, notes, emails, meetings)

        # Get stage name for display
        stage_id_to_name = {v: k for k, v in STAGES.items()}
        props = deal_info.get("properties", {})
        stage_name = stage_id_to_name.get(props.get("dealstage", ""), "Unknown")
        amount = props.get("amount", "")
        last_contacted = props.get("notes_last_contacted", "")

        msg = f"📋 *TLDR: {deal_label}*\n\n"
        msg += f"*Stage:* {stage_name}\n"
        if amount:
            msg += f"*Amount:* ${amount}\n"
        if last_contacted:
            msg += f"*Last Contacted:* {last_contacted[:10]}\n"
        msg += f"\n---\n\n{summary}\n\n"
        msg += f"<https://app.hubspot.com/contacts/{HUBSPOT_ACCOUNT_ID}/record/0-3/{deal_id}|View Deal in HubSpot>"

        client.chat_postMessage(channel=user_id, text=msg)

    except Exception as e:
        logger.error(f"TLDR error: {e}")
        client.chat_postMessage(
            channel=user_id,
            text=f"❌ Failed to generate TLDR:\n```{str(e)}```",
        )


# ── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    print("⚡ Sapien Sales Bot is running!")
    handler.start()
