import asyncio
import base64
import email
import json
import sys
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from langgraph.checkpoint.memory import MemorySaver

import app.database
import app.main
import app.workflow
from app.config import settings
from app.db_models import Base, CampaignModel, TradeAuditModel
from app.models import Campaign

# Setup isolated test database and test checkpointer
test_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
Base.metadata.create_all(bind=test_engine)

app.database.engine = test_engine
app.database.SessionLocal = TestSessionLocal
app.main.SessionLocal = TestSessionLocal


def _override_get_db():
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.main.app.dependency_overrides[app.database.get_db] = _override_get_db

test_checkpointer = MemorySaver()
test_checkpointer.setup = lambda: None
app.workflow.checkpointer = test_checkpointer
app.workflow.trade_graph = app.workflow.build_trade_graph(checkpointer=test_checkpointer)
app.main.trade_graph = app.workflow.trade_graph

from app.gmail_client import (
    fetch_latest_messages_by_history,
    get_gmail_service,
    send_gmail_reply,
    setup_gmail_watch,
)

PASS_COUNT = 0
FAIL_COUNT = 0


def assert_true(condition: bool, msg: str):
    global PASS_COUNT, FAIL_COUNT
    if condition:
        PASS_COUNT += 1
        print(f"  [PASS] {msg}")
    else:
        FAIL_COUNT += 1
        print(f"  [FAIL] {msg}")


def test_configuration_settings():
    print("\n--- 1. Testing Push Webhooks Configuration Settings ---")
    assert_true(hasattr(settings, "google_pubsub_topic"), "settings has google_pubsub_topic")
    assert_true(hasattr(settings, "google_credentials_json_path"), "settings has google_credentials_json_path")
    assert_true(hasattr(settings, "google_token_json_path"), "settings has google_token_json_path")
    assert_true(hasattr(settings, "use_push_webhooks"), "settings has use_push_webhooks")

    assert_true(isinstance(settings.google_pubsub_topic, str), "google_pubsub_topic is str")
    assert_true(settings.google_credentials_json_path == "credentials.json", "default credentials path is credentials.json")
    assert_true(settings.google_token_json_path == "token.json", "default token path is token.json")
    assert_true(isinstance(settings.use_push_webhooks, bool), "use_push_webhooks is bool")


def test_gmail_client_watch():
    print("\n--- 2. Testing Gmail Service setup_gmail_watch ---")
    mock_service = MagicMock()
    mock_watch = MagicMock()
    mock_watch.execute.return_value = {"historyId": "12345", "expiration": "1710000000"}
    mock_service.users().watch.return_value = mock_watch

    with patch("app.gmail_client.get_gmail_service", return_value=mock_service):
        topic = "projects/arbitrage-prod/topics/gmail-inbox-watch"
        res = setup_gmail_watch(topic)
        assert_true(res.get("historyId") == "12345", "setup_gmail_watch returns historyId from Gmail API")
        mock_service.users().watch.assert_called_once_with(
            userId="me",
            body={"topicName": topic, "labelIds": ["INBOX"]},
        )
        assert_true(True, "setup_gmail_watch calls users().watch with topic and INBOX label")


def test_gmail_client_history_fetch():
    from app.gmail_client import PROCESSED_GMAIL_MESSAGE_IDS
    PROCESSED_GMAIL_MESSAGE_IDS.clear()
    print("\n--- 3. Testing Gmail Service fetch_latest_messages_by_history ---")
    mock_service = MagicMock()

    mock_history_list = MagicMock()
    mock_history_list.execute.return_value = {
        "history": [
            {
                "id": "101",
                "messagesAdded": [
                    {"message": {"id": "msg_001", "threadId": "thread_abc"}}
                ],
            }
        ],
        "historyId": "102",
    }
    mock_service.users().history().list.return_value = mock_history_list

    # Mock full message get
    mock_msg_get = MagicMock()
    sample_body_text = "Counter-offer: We accept USD 1,020/MT CIF Jebel Ali for 500 MT Basmati 1121."
    b64_body = base64.urlsafe_b64encode(sample_body_text.encode("utf-8")).decode("ascii")

    mock_msg_get.execute.return_value = {
        "id": "msg_001",
        "threadId": "thread_abc",
        "snippet": "Counter-offer snippet",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Message-ID", "value": "<buyer-msg-001@gulffood.ae>"},
                {"name": "In-Reply-To", "value": "<desk-out-001@arbitrage.com>"},
                {"name": "References", "value": "<desk-out-001@arbitrage.com>"},
                {"name": "Subject", "value": "Re: Soft Corporate Offer (SCO) — Basmati 1121"},
                {"name": "From", "value": "Gulf Food Trading LLC <procurement@gulffood.ae>"},
            ],
            "body": {
                "size": len(sample_body_text),
                "data": b64_body,
            },
        },
    }
    mock_service.users().messages().get.return_value = mock_msg_get

    with patch("app.gmail_client.get_gmail_service", return_value=mock_service):
        messages = fetch_latest_messages_by_history("101")
        assert_true(len(messages) == 1, "fetch_latest_messages_by_history returned 1 message")
        msg = messages[0]
        assert_true(msg["id"] == "msg_001", "Captured message id")
        assert_true(msg["threadId"] == "thread_abc", "Captured threadId")
        assert_true(msg["Message-ID"] == "<buyer-msg-001@gulffood.ae>", "Captured Message-ID header")
        assert_true(msg["In-Reply-To"] == "<desk-out-001@arbitrage.com>", "Captured In-Reply-To header")
        assert_true(msg["References"] == "<desk-out-001@arbitrage.com>", "Captured References header")
        assert_true(msg["Subject"] == "Re: Soft Corporate Offer (SCO) — Basmati 1121", "Captured Subject header")
        assert_true("Gulf Food" in msg["From"], "Captured From header")
        assert_true("1,020" in msg["body"], "Decoded UTF-8 body text correctly")


def test_gmail_client_send_reply():
    print("\n--- 4. Testing Gmail Service send_gmail_reply ---")
    mock_service = MagicMock()
    mock_send = MagicMock()
    mock_send.execute.return_value = {"id": "outbound_123", "threadId": "thread_abc"}
    mock_service.users().messages().send.return_value = mock_send

    with patch("app.gmail_client.get_gmail_service", return_value=mock_service):
        raw_draft = (
            "SUBJECT: Re: Trade Confirmation — Basmati 1121\n\n"
            "BODY: We confirm the negotiated terms of USD 1,020/MT CIF Jebel Ali."
        )
        ok = send_gmail_reply(
            to_email="procurement@livebuyer.com",
            raw_draft=raw_draft,
            in_reply_to="<buyer-msg-001@livebuyer.com>",
            references="<buyer-msg-001@livebuyer.com>",
            thread_subject="Trade Confirmation — Basmati 1121",
            thread_id="thread_abc",
        )
        assert_true(ok is True, "send_gmail_reply returned True")
        mock_service.users().messages().send.assert_called_once()
        call_kwargs = mock_service.users().messages().send.call_args[1]
        assert_true(call_kwargs.get("userId") == "me", "Sent with userId='me'")
        assert_true("raw" in call_kwargs.get("body", {}), "Sent body contains base64 raw message")
        assert_true(call_kwargs.get("body", {}).get("threadId") == "thread_abc", "Sent body contains threadId")

        # Verify decoded MIME headers
        raw_b64 = call_kwargs["body"]["raw"]
        raw_bytes = base64.urlsafe_b64decode(raw_b64.encode("ascii"))
        parsed_email = email.message_from_bytes(raw_bytes)
        assert_true(parsed_email["To"] == "procurement@livebuyer.com", "MIME To matches recipient")
        assert_true(parsed_email["In-Reply-To"] == "<buyer-msg-001@livebuyer.com>", "MIME In-Reply-To set")
        assert_true("<buyer-msg-001@livebuyer.com>" in parsed_email["References"], "MIME References set")


def test_webhook_immediate_response_and_background():
    print("\n--- 5. Testing POST /api/webhooks/gmail Immediate HTTP 200 & Processing ---")
    client = TestClient(app.main.app)

    # 1. Create a campaign in the database
    camp_id = "CAMP-WEBHOOK-01"
    with TestSessionLocal() as db:
        camp_model = CampaignModel(
            id=camp_id,
            commodity="Basmati 1121",
            target_volume_mt=500.0,
            destination_port="Jebel Ali",
            origin_port_default="Karachi",
            deal_status="initiating",
            anchor_cif_usd=1080.0,
        )
        db.add(camp_model)
        db.commit()

    # Pre-populate initial state in trade_graph checkpointer
    initial_campaign = Campaign(
        campaign_id=camp_id,
        commodity="Basmati 1121",
        target_volume_mt=500.0,
        destination_port="Jebel Ali",
        origin_port_default="Karachi",
        min_profit_per_mt_hard=50.0,
        min_profit_per_mt_soft=120.0,
        max_negotiation_rounds=3,
    )
    initial_state = {
        "campaign": initial_campaign,
        "negotiation_round": 0,
        "deal_status": "prospecting",
        "action": None,
        "benchmark_fob_usd": 900.0,
        "freight_cost_usd": 50.0,
        "dynamic_fob_ceiling": 950.0,
        "dynamic_cif_floor": 980.0,
        "anchor_cif_usd": 1080.0,
        "audit_transcript": [],
    }
    app.workflow.trade_graph.invoke(initial_state, config={"configurable": {"thread_id": camp_id}})

    # Mock fetch_latest_messages_by_history returning a buyer counter
    mock_incoming_messages = [
        {
            "id": "msg_hook_01",
            "threadId": "thread_hook_01",
            "Message-ID": "<buyer-hook-01@gulffood.ae>",
            "In-Reply-To": "<sco-initial@arbitrage.com>",
            "References": "<sco-initial@arbitrage.com>",
            "Subject": f"Re: Soft Corporate Offer — Basmati 1121 CIF Jebel Ali ({camp_id})",
            "From": "Gulf Food Trading LLC <procurement@gulffood.ae>",
            "body": f"Dear Trading Desk, we acknowledge receipt for campaign {camp_id}. We submit a counter-bid of USD 1,020.00/MT CIF Jebel Ali.",
        }
    ]

    # Create mock Pub/Sub base64 payload
    pubsub_json_data = json.dumps({"emailAddress": "desk@arbitrage.com", "historyId": "778899"})
    data_b64 = base64.b64encode(pubsub_json_data.encode("utf-8")).decode("utf-8")
    payload = {
        "message": {
            "data": data_b64,
            "messageId": "pubsub-msg-999",
            "publishTime": "2026-09-30T00:00:00Z",
        },
        "subscription": "projects/arbitrage-desk/subscriptions/gmail-sub",
    }

    with patch("app.services.gmail_worker.fetch_latest_messages_by_history", return_value=mock_incoming_messages), \
         patch("app.services.gmail_worker.send_gmail_reply", return_value=True) as mock_send_reply:

        # Send push webhook to POST /api/webhooks/gmail
        res = client.post("/api/webhooks/gmail", json=payload)

        # Verify immediate HTTP 200 acknowledgment
        assert_true(res.status_code == 200, f"Webhook endpoint returned HTTP 200 (got {res.status_code})")
        res_json = res.json()
        assert_true(res_json.get("status") == "ok", "Webhook response status is 'ok'")
        assert_true(res_json.get("historyId") == "778899", f"Webhook acknowledged historyId '778899' (got {res_json.get('historyId')})")

        # In TestClient, BackgroundTasks execute synchronously with the response
        # Verify that background processing triggered without crashing
        with TestSessionLocal() as db:
            camp_after = db.query(CampaignModel).filter(CampaignModel.id == camp_id).first()
            assert_true(camp_after is not None, "Campaign exists in database")

            audits = db.query(TradeAuditModel).filter(TradeAuditModel.campaign_id == camp_id).all()
            assert_true(len(audits) >= 1, f"Audit rows persisted for campaign ({len(audits)} found)")

            inbound_audits = [a for a in audits if a.direction == "INBOUND" and a.role == "buyer"]
            assert_true(len(inbound_audits) >= 1, "Persisted INBOUND audit row for incoming counter-bid")
            assert_true(inbound_audits[0].counterparty_price == 1020.0, "Captured counterparty price $1,020/MT")

            outbound_audits = [a for a in audits if a.direction == "OUTBOUND"]
            assert_true(len(outbound_audits) >= 1, "Persisted OUTBOUND audit row for desk counter response")

        assert_true(mock_send_reply.called, "send_gmail_reply was invoked to dispatch outbound counter response")


def test_webhook_ignores_desk_own_email():
    print("\n--- 6. Testing Webhook Filters Desk's Own Email ---")
    client = TestClient(app.main.app)

    camp_id = "CAMP-SELF-FILTER"
    with TestSessionLocal() as db:
        camp_model = CampaignModel(
            id=camp_id,
            commodity="Basmati 1121",
            target_volume_mt=500.0,
            destination_port="Jebel Ali",
            origin_port_default="Karachi",
            deal_status="initiating",
        )
        db.add(camp_model)
        db.commit()

    # Message sent from desk_email
    desk_msg = [
        {
            "id": "msg_self_01",
            "threadId": "thread_self_01",
            "Message-ID": "<outbound-01@arbitrage.com>",
            "Subject": f"Re: Basmati 1121 ({camp_id})",
            "From": settings.desk_email,
            "body": "This is an outbound message sent by the desk itself.",
        }
    ]

    pubsub_json_data = json.dumps({"historyId": "112233"})
    data_b64 = base64.b64encode(pubsub_json_data.encode("utf-8")).decode("utf-8")

    with patch("app.services.gmail_worker.fetch_latest_messages_by_history", return_value=desk_msg), \
         patch("app.services.gmail_worker.send_gmail_reply") as mock_send:

        res = client.post("/api/webhooks/gmail", json={"message": {"data": data_b64}})
        assert_true(res.status_code == 200, "Desk own message webhook returned 200")

        # Verify no audits created for self message
        with TestSessionLocal() as db:
            audits = db.query(TradeAuditModel).filter(TradeAuditModel.campaign_id == camp_id).all()
            assert_true(len(audits) == 0, "No audit rows created for message from desk's own email")
        assert_true(not mock_send.called, "No outbound reply dispatched for desk's own email")


def test_webhook_ignores_bounce_messages():
    print("\n--- 7. Testing Webhook Filters Bounce / Daemon Messages ---")
    client = TestClient(app.main.app)

    bounce_msg = [
        {
            "id": "msg_bounce_01",
            "threadId": "thread_bounce_01",
            "Message-ID": "<bounce-01@googlemail.com>",
            "Subject": "Delivery Status Notification (Failure)",
            "From": "mailer-daemon@googlemail.com",
            "body": "Address not found: Your message wasn't delivered.",
        }
    ]

    pubsub_json_data = json.dumps({"historyId": "445566"})
    data_b64 = base64.b64encode(pubsub_json_data.encode("utf-8")).decode("utf-8")

    with patch("app.services.gmail_worker.fetch_latest_messages_by_history", return_value=bounce_msg):
        res = client.post("/api/webhooks/gmail", json={"message": {"data": data_b64}})
        assert_true(res.status_code == 200, "Bounce webhook returned 200")


def test_webhook_empty_payload_safe():
    print("\n--- 8. Testing Webhook Safe Handling of Malformed / Empty Payloads ---")
    client = TestClient(app.main.app)

    # Empty body
    res = client.post("/api/webhooks/gmail", json={})
    assert_true(res.status_code == 200, "Empty JSON returns 200 (avoids Pub/Sub retries)")

    # Malformed base64
    res2 = client.post("/api/webhooks/gmail", json={"message": {"data": "not_valid_base64!!!"}})
    assert_true(res2.status_code == 200, "Malformed data returns 200 gracefully")


def test_lifespan_webhook_activation():
    print("\n--- 9. Testing Lifespan Configuration Behavior ---")
    # Test push webhooks enabled invokes setup_gmail_watch
    with patch("app.main.setup_gmail_watch") as mock_watch, \
         patch("app.main.email_polling_worker") as mock_poller:

        original_push = settings.use_push_webhooks
        original_topic = settings.google_pubsub_topic
        try:
            settings.use_push_webhooks = True
            settings.google_pubsub_topic = "projects/test-proj/topics/inbox"

            async def run_lifespan_test():
                async with app.main.lifespan(app.main.app):
                    pass

            asyncio.run(run_lifespan_test())
            assert_true(mock_watch.called, "Lifespan invoked setup_gmail_watch when use_push_webhooks=True and topic set")
            assert_true(not mock_poller.called, "Lifespan kept legacy email_polling_worker inactive when use_push_webhooks=True")

        finally:
            settings.use_push_webhooks = original_push
            settings.google_pubsub_topic = original_topic

    # Test legacy mode keeps polling worker active if credentials configured
    with patch("app.main.setup_gmail_watch") as mock_watch, \
         patch("app.main.email_polling_worker") as mock_poller:

        original_push = settings.use_push_webhooks
        original_user = settings.email_user
        original_pass = settings.email_pass
        try:
            settings.use_push_webhooks = False
            settings.email_user = "desk@test.com"
            settings.email_pass = "app-password"

            async def run_lifespan_legacy_test():
                async with app.main.lifespan(app.main.app):
                    pass

            asyncio.run(run_lifespan_legacy_test())
            assert_true(not mock_watch.called, "setup_gmail_watch not called when use_push_webhooks=False")
            assert_true(mock_poller.called, "Legacy email_polling_worker called when use_push_webhooks=False and credentials configured")

        finally:
            settings.use_push_webhooks = original_push
            settings.email_user = original_user
            settings.email_pass = original_pass


def main():
    print("====================================================================")
    print("  Commodity Arbitrage Desk — Event-Driven Gmail Push Webhooks Tests")
    print("====================================================================")

    test_configuration_settings()
    test_gmail_client_watch()
    test_gmail_client_history_fetch()
    test_gmail_client_send_reply()
    test_webhook_immediate_response_and_background()
    test_webhook_ignores_desk_own_email()
    test_webhook_ignores_bounce_messages()
    test_webhook_empty_payload_safe()
    test_lifespan_webhook_activation()

    print("\n====================================================================")
    print(f"  Webhook Test Results: {PASS_COUNT} passed, {FAIL_COUNT} failed")
    print("====================================================================")

    if FAIL_COUNT > 0:
        print("\n  SOME TESTS FAILED! ***")
        sys.exit(1)
    else:
        print("\n  ALL WEBHOOK TESTS PASSED SUCCESSFULLY! ***")
        sys.exit(0)


if __name__ == "__main__":
    main()
