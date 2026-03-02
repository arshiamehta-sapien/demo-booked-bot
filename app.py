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
        # Update the domain if a URL was provided and company already exists
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
    # If exact email param doesn't work, search all owners
    resp = requests.get(url, headers=hubspot_headers(), params={"limit": 100})
    resp.raise_for_status()
    for owner in resp.json().get("results", []):
        if owner.get("email", "").lower() == email.lower():
            logger.info(f"Found HubSpot owner {owner['id']} for {email}")
            return owner["id"]
    logger.warning(f"No HubSpot owner found for {email}")
    return ""


def create_deal(deal_name: str, contact_id: str, company_id: str, amount: str = "", close_date: str = "", owner_id: str = "") -> str:
    """Create a deal in the Sales Pipeline at Demo Booked stage, associated with contact + company."""
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


# ── Slack: Modal Submission → Create in HubSpot ─────────────────────────────

@app.view("demo_booked_submit")
def handle_submission(ack, body, client, view):
    # Extract values from the modal
    values = view["state"]["values"]
    full_name = values["contact_name_block"]["contact_name"]["value"].strip()
    company_name = values["company_block"]["company_name"]["value"].strip()
    email = values["email_block"]["email"]["value"].strip()
    company_url = (values["company_url_block"]["company_url"]["value"] or "").strip()
    amount = (values["amount_block"]["amount"]["value"] or "").strip()
    close_date = (values["close_date_block"]["close_date"]["selected_date"] or "")

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
    deal_name = company_name

    # Look up the Slack user's email to find their HubSpot owner ID
    owner_id = ""
    try:
        slack_user_info = client.users_info(user=user_id)
        slack_email = slack_user_info["user"]["profile"].get("email", "")
        if slack_email:
            owner_id = find_hubspot_owner_by_email(slack_email)
    except Exception as e:
        logger.warning(f"Could not look up Slack user email: {e}")

    try:
        # 1. Create or find contact
        contact_id = create_or_find_contact(email, first_name, last_name)

        # 2. Set contact owner if we found one
        if owner_id:
            update_url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}"
            requests.patch(update_url, json={"properties": {"hubspot_owner_id": owner_id}}, headers=hubspot_headers())
            logger.info(f"Set contact {contact_id} owner to {owner_id}")

        # 3. Create or find company
        company_id = create_or_find_company(company_name, company_url)

        # 4. Associate contact ↔ company
        associate_contact_to_company(contact_id, company_id)

        # 5. Create deal linked to both
        deal_id = create_deal(deal_name, contact_id, company_id, amount, close_date, owner_id)

        # Notify the user via DM
        msg = (
            f"✅ *Demo Booked* created in HubSpot!\n\n"
            f"• *Deal:* {deal_name}\n"
            f"• *Contact:* {full_name} ({email})\n"
            f"• *Company:* {company_name}\n"
            f"• *Stage:* Demo Booked (Sales Pipeline)\n"
        )
        if amount:
            msg += f"• *Amount:* ${amount}\n"
        if close_date:
            msg += f"• *Close Date:* {close_date}\n"
        msg += f"\n<https://app.hubspot.com/contacts/46061347/record/0-3/{deal_id}|View Deal in HubSpot>"

        client.chat_postMessage(channel=user_id, text=msg)

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
