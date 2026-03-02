"""
Demo Booked Slack Bot
=====================
A Slack slash command (/demo-booked) that creates a deal in HubSpot's
Sales Pipeline under the "Demo Booked" stage, along with a contact and company.

Usage in Slack:  /demo-booked
  → Opens a form to enter Contact Name, Company, and Email
  → Creates everything in HubSpot and links them together
"""

import os
import logging
import requests
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# ── Config ───────────────────────────────────────────────────────────────────
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]  # xapp-... token for Socket Mode
HUBSPOT_API_KEY = os.environ["HUBSPOT_API_KEY"]  # Private app access token

# HubSpot constants (from your account)
HUBSPOT_PIPELINE_ID = "default"           # Sales Pipeline
HUBSPOT_STAGE_ID = "1296063606"           # Demo Booked stage

HUBSPOT_BASE = "https://api.hubapi.com"

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
    # Try to find existing contact by email first
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

    # Create new contact
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


def create_or_find_company(company_name: str) -> str:
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
        return company_id

    url = f"{HUBSPOT_BASE}/crm/v3/objects/companies"
    body = {"properties": {"name": company_name}}
    resp = requests.post(url, json=body, headers=hubspot_headers())
    resp.raise_for_status()
    company_id = resp.json()["id"]
    logger.info(f"Created company {company_id} for {company_name}")
    return company_id


def create_deal(deal_name: str, contact_id: str, company_id: str) -> str:
    """Create a deal in the Sales Pipeline at Demo Booked stage, associated with contact + company."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/deals"
    body = {
        "properties": {
            "dealname": deal_name,
            "pipeline": HUBSPOT_PIPELINE_ID,
            "dealstage": HUBSPOT_STAGE_ID,
        },
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


# ── Slack: Slash Command → Open Modal ────────────────────────────────────────

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
            ],
        },
    )


# ── Slack: Modal Submission → Create in HubSpot ─────────────────────────────

@app.view("demo_booked_submit")
def handle_submission(ack, body, client, view):
    # Extract values from the modal
    values = view["state"]["values"]
    full_name = values["contact_name_block"]["contact_name"]["value"].strip()
    company_name = values["company_block"]["company_name"]["value"].strip()
    email = values["email_block"]["email"]["value"].strip()

    # Parse first/last name
    name_parts = full_name.split(" ", 1)
    first_name = name_parts[0]
    last_name = name_parts[1] if len(name_parts) > 1 else ""

    # Validate email (basic)
    if "@" not in email:
        ack(response_action="errors", errors={"email_block": "Please enter a valid email address."})
        return

    ack()

    user_id = body["user"]["id"]
    deal_name = f"Demo - {company_name} ({full_name})"

    try:
        # 1. Create or find contact
        contact_id = create_or_find_contact(email, first_name, last_name)

        # 2. Create or find company
        company_id = create_or_find_company(company_name)

        # 3. Associate contact ↔ company
        associate_contact_to_company(contact_id, company_id)

        # 4. Create deal linked to both
        deal_id = create_deal(deal_name, contact_id, company_id)

        # Notify the user via DM
        client.chat_postMessage(
            channel=user_id,
            text=(
                f"✅ *Demo Booked* created in HubSpot!\n\n"
                f"• *Deal:* {deal_name}\n"
                f"• *Contact:* {full_name} ({email})\n"
                f"• *Company:* {company_name}\n"
                f"• *Stage:* Demo Booked (Sales Pipeline)\n\n"
                f"<https://app.hubspot.com/contacts/46061347/record/0-3/{deal_id}|View Deal in HubSpot>"
            ),
        )

    except Exception as e:
        logger.error(f"HubSpot error: {e}")
        client.chat_postMessage(
            channel=user_id,
            text=f"❌ Failed to create demo in HubSpot:\n```{str(e)}```\nPlease check the logs or try again.",
        )


# ── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    print("⚡ Demo Booked bot is running!")
    handler.start()
