"""Prepare graph state for an inbound email: who sent it, which thread it continues, who we answer."""
import email.utils
from dataclasses import dataclass
from typing import Optional

from app.config import settings
from app.db.db_models import CampaignModel
from app.directory import (
    get_suppliers_for_commodity,
    resolve_supplier_contact,
    select_primary_buyer,
)
from app.graph import workflow
from app.models import Campaign
from app.services.mail.email_utils import is_from_desk, strip_reply_prefix
from app.telemetry import build_trace_config, get_langfuse_handler


@dataclass(frozen=True)
class InboundEmail:
    """One inbound email, whichever path (Gmail push or IMAP poll) delivered it."""

    body: str
    sender: str
    subject: str = ""
    message_id: str = ""
    in_reply_to: str = ""
    references: str = ""
    thread_id: str = ""  # Gmail thread id; empty for IMAP


@dataclass(frozen=True)
class DealContacts:
    buyer_name: str
    buyer_email: str
    supplier_name: str
    supplier_email: str


def build_inbound_config(db_camp: CampaignModel) -> dict:
    return build_trace_config(
        db_camp.id,
        "gmail-inbound-turn",
        get_langfuse_handler(),
        metadata={
            "commodity": db_camp.commodity,
            "target_volume_mt": db_camp.target_volume_mt,
            "source": "gmail_inbound",
        },
        tags=[db_camp.commodity, "gmail-inbound"],
    )


def load_campaign_state(db_camp: CampaignModel, config: dict) -> dict:
    state = workflow.get_state_values(config)
    if "campaign" not in state:
        state["campaign"] = Campaign(
            campaign_id=db_camp.id,
            commodity=db_camp.commodity,
            target_volume_mt=db_camp.target_volume_mt,
            destination_port=db_camp.destination_port,
            origin_port_default=db_camp.origin_port_default,
        )
    elif isinstance(state["campaign"], dict):
        state["campaign"] = Campaign(**state["campaign"])
    return state


def classify_sender_role(sender: str, state: dict, commodity: str) -> str:
    """The deal's own contacts decide first: a desk-picked buyer may use an address that is a supplier
    elsewhere (even MY_TEST_SUPPLIER_EMAIL), and vice versa. Otherwise supplier if the sender is a known
    supplier contact for this campaign, else buyer."""
    addr = email.utils.parseaddr(sender)[1].strip().lower()
    if not addr:
        return "buyer"
    if addr == str(state.get("target_buyer_email") or "").strip().lower():
        return "buyer"
    if addr == str(state.get("target_supplier_email") or "").strip().lower():
        return "supplier"
    supplier_addrs = {
        s.contact_email.strip().lower() for s in get_suppliers_for_commodity(commodity) if s.contact_email
    }
    for known in (state.get("target_supplier_email"), settings.my_test_supplier_email):
        if known:
            supplier_addrs.add(str(known).strip().lower())
    return "supplier" if addr in supplier_addrs else "buyer"


def _append_reference(state: dict, refs_key: str, msg_id: str, references: str) -> None:
    existing_refs = state.get(refs_key) or references or ""
    if msg_id not in existing_refs:
        state[refs_key] = f"{existing_refs} {msg_id}".strip()


def prepare_graph_state(state: dict, inbound: InboundEmail, role: str) -> dict:
    """Load the email into state, clear last turn's drafts, and extend the counterparty's thread headers."""
    state.update(latest_email=inbound.body, active_role=role, buyer_draft="", supplier_draft="")

    id_key, refs_key = (
        ("last_supplier_message_id", "supplier_references") if role == "supplier"
        else ("last_buyer_message_id", "buyer_references")
    )
    if inbound.message_id:
        state[id_key] = inbound.message_id
        _append_reference(state, refs_key, inbound.message_id, inbound.references)
    elif inbound.references and not state.get(refs_key):
        state[refs_key] = inbound.references

    if role == "buyer":
        if not state.get("thread_subject") and inbound.subject:
            state["thread_subject"] = strip_reply_prefix(inbound.subject)
        if inbound.thread_id:
            state["gmail_thread_id"] = inbound.thread_id
    return state


def resolve_deal_contacts(
    campaign: Campaign, sender: str, desk_emails: set[str], state: dict, role: str
) -> DealContacts:
    """Buyer and supplier to answer. An external sender replaces the stored address for their own side."""
    # A buyer the desk picked at launch (state) wins over the test inbox and the directory.
    primary_buyer = select_primary_buyer(campaign.commodity)
    buyer_name = state.get("target_buyer_name") or (primary_buyer.name if primary_buyer else "Procurement Partner")
    default_buyer_email = state.get("target_buyer_email") or settings.my_test_email or (
        primary_buyer.contact_email if primary_buyer else "procurement@domain.com"
    )
    supp_name, supp_email = resolve_supplier_contact(
        campaign.commodity, state.get("target_supplier_email"), state.get("target_supplier_name")
    )

    sender_addr = email.utils.parseaddr(sender)[1]
    sender_is_external = bool(sender_addr) and not is_from_desk(sender_addr, desk_emails)
    if role == "supplier":
        buyer_email = state.get("target_buyer_email") or default_buyer_email
        supp_email = sender_addr if sender_is_external else supp_email
    else:
        buyer_email = sender_addr if sender_is_external else default_buyer_email
    return DealContacts(buyer_name, buyer_email, supp_name, supp_email)


def record_contacts_in_state(state: dict, contacts: DealContacts) -> None:
    state.update(
        target_buyer_name=contacts.buyer_name,
        target_buyer_email=contacts.buyer_email,
        target_supplier_email=contacts.supplier_email,
        target_supplier_name=contacts.supplier_name,
    )


def counterparty_label(contacts: DealContacts, role: str, sender: Optional[str]) -> str:
    name = contacts.supplier_name if role == "supplier" else contacts.buyer_name
    return f"{name} <{sender or contacts.buyer_email}>"
