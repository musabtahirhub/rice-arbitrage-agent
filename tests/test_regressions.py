"""Regression tests for the Phase 1-2 deal-integrity and negotiation-logic fixes.

All LLM, market-data and email I/O is patched out, so these run offline regardless of .env.
"""
import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.database
import app.workflow
from app.agents.buyer_agent import counter_buyer_node
from app.agents.risk_worker import _process_parsed_buyer_terms, route_after_evaluation
from app.config import settings
from app.db_models import Base, CampaignModel, ProcessedInboundEmailModel, TradeAuditModel
from app.math_engine import evaluate_deal
from app.models import ApprovalPayload, Campaign, ParsedEmail, SimulateTurnRequest
from app.routes.negotiation import approve_campaign_deal, negotiate_turn
from app.services import gmail_worker
from app.services.llm_service import _extract_terms_via_regex_fallback

BENCH_FOB = 900.0
FREIGHT = 45.0


def _campaign(**overrides) -> Campaign:
    params = dict(campaign_id="CAMP-REG", commodity="Basmati 1121", target_volume_mt=500.0,
                  max_negotiation_rounds=3)
    params.update(overrides)
    return Campaign(**params)


def _terms(role: str, price: float, qty: float = 500.0) -> ParsedEmail:
    return ParsedEmail(sender_role=role, commodity="Basmati 1121", quantity_mt=qty, price_usd_per_mt=price,
                       incoterm="CIF" if role == "buyer" else "FOB")


def _offline_patches(stack: ExitStack) -> None:
    """Patch every network-touching dependency used by graph nodes."""
    stack.enter_context(patch.object(settings, "gemini_api_key", ""))
    stack.enter_context(patch("app.services.llm_service._get_llm", return_value=None))
    for target in ("app.agents.risk_worker", "app.agents.buyer_agent", "app.services.gmail_worker"):
        stack.enter_context(patch(f"{target}.get_benchmark_rate", return_value=BENCH_FOB))
    for target in ("app.agents.risk_worker", "app.agents.buyer_agent"):
        stack.enter_context(patch(f"{target}.estimate_freight", return_value=FREIGHT))
    stack.enter_context(patch("app.agents.buyer_agent.send_email", return_value=True))


class TestRegexFallbackParser(unittest.TestCase):
    def test_generic_trade_words_are_not_acceptance(self):
        text = "Please confirm your best price for this order. Specification attached. We quote USD 950/MT FOB Karachi."
        parsed = _extract_terms_via_regex_fallback(text, "supplier", 1000.0, "Basmati 1121", 500.0)
        self.assertFalse(parsed.is_acceptance)
        self.assertEqual(parsed.intent, "COUNTER_OFFER")

    def test_specification_does_not_imply_cif(self):
        text = "Per the specification sheet we offer USD 950 per MT, 500 MT."
        parsed = _extract_terms_via_regex_fallback(text, "supplier", 1000.0, "Basmati 1121", 500.0)
        self.assertEqual(parsed.incoterm, "FOB")

    def test_explicit_acceptance_detected(self):
        parsed = _extract_terms_via_regex_fallback(
            "We accept your offer of USD 1,150/MT CIF Jebel Ali.", "buyer", 1150.0, "Basmati 1121", 500.0
        )
        self.assertTrue(parsed.is_acceptance)
        self.assertEqual(parsed.price_usd_per_mt, 1150.0)

    def test_negated_acceptance_not_detected(self):
        parsed = _extract_terms_via_regex_fallback(
            "We cannot accept USD 1,150/MT CIF. Our bid is USD 1,080/MT CIF.", "buyer", 1150.0, "Basmati 1121", 500.0
        )
        self.assertFalse(parsed.is_acceptance)


class TestBuyerAcceptanceIsNotSticky(unittest.TestCase):
    def test_counter_offer_writes_explicit_false(self):
        state = {"supplier_terms": _terms("supplier", 900.0), "buyer_accepted": True}
        update = _process_parsed_buyer_terms(_terms("buyer", 1050.0), state, _campaign(), 1100.0)
        self.assertIs(update["buyer_accepted"], False)


class TestMathEngineNegotiableViolations(unittest.TestCase):
    def test_supplier_above_ceiling_is_countered_not_rejected(self):
        res = evaluate_deal(_campaign(), _terms("buyer", 1200.0), _terms("supplier", 960.0), BENCH_FOB, FREIGHT, round_num=1)
        self.assertFalse(res.viable)
        self.assertIsNone(res.action)
        self.assertIn("exceeds dynamic ceiling", res.reason)
        route = route_after_evaluation({"action": res.action, "is_deal_viable": res.viable,
                                        "supplier_terms": _terms("supplier", 960.0), "active_role": "supplier"})
        self.assertEqual(route, "counter_supplier")

    def test_supplier_above_ceiling_rejected_once_rounds_exhausted(self):
        res = evaluate_deal(_campaign(), _terms("buyer", 1200.0), _terms("supplier", 960.0), BENCH_FOB, FREIGHT, round_num=3)
        self.assertEqual(res.action, "REJECT_HARD")

    def test_quantity_within_tolerance_is_evaluated(self):
        res = evaluate_deal(_campaign(), _terms("buyer", 1200.0, qty=500.0), _terms("supplier", 900.0, qty=495.0),
                            BENCH_FOB, FREIGHT, round_num=1)
        self.assertNotIn("Quantity mismatch", res.reason)
        self.assertTrue(res.viable)

    def test_quantity_mismatch_is_countered(self):
        res = evaluate_deal(_campaign(), _terms("buyer", 1200.0, qty=500.0), _terms("supplier", 900.0, qty=300.0),
                            BENCH_FOB, FREIGHT, round_num=1)
        self.assertIsNone(res.action)
        self.assertIn("Quantity mismatch", res.reason)


class TestCounterBuyerPricing(unittest.TestCase):
    def _state(self, **extra) -> dict:
        state = {
            "campaign": _campaign(),
            "dynamic_cif_floor": 1100.0,
            "buyer_terms": _terms("buyer", 1080.0),
            "action": "COUNTER_TO_MAXIMIZE",
            "negotiation_round": 2,
            "evaluation_reason": "test",
            "last_counter_cif_usd": 1150.0,
        }
        state.update(extra)
        return state

    def test_counter_moves_with_buyer_bid_despite_previous_counter(self):
        with ExitStack() as stack:
            _offline_patches(stack)
            update = counter_buyer_node(self._state())
        self.assertEqual(update["last_counter_cif_usd"], round(1080.0 + settings.buyer_counter_step_usd_per_mt, 2))

    def test_override_applies_once_and_is_consumed(self):
        with ExitStack() as stack:
            _offline_patches(stack)
            update = counter_buyer_node(self._state(override_cif_price=1195.0))
        self.assertEqual(update["last_counter_cif_usd"], 1195.0)
        self.assertIn("override_cif_price", update)
        self.assertIsNone(update["override_cif_price"])


class _DbTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        cls.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)
        Base.metadata.create_all(bind=cls.engine)

    def setUp(self):
        self.db = self.SessionLocal()
        self.db_camp = CampaignModel(id="CAMP-REG", commodity="Basmati 1121", target_volume_mt=500.0,
                                     destination_port="Jebel Ali", origin_port_default="Karachi",
                                     deal_status="prospecting")
        self.db.merge(self.db_camp)
        self.db.commit()
        self.db_camp = self.db.query(CampaignModel).filter(CampaignModel.id == "CAMP-REG").first()

    def tearDown(self):
        self.db.query(ProcessedInboundEmailModel).delete()
        self.db.query(TradeAuditModel).delete()
        self.db.query(CampaignModel).delete()
        self.db.commit()
        self.db.close()


def _mock_graph(awaiting: bool, invoke_result: dict | None = None) -> MagicMock:
    graph = MagicMock()
    snapshot = MagicMock()
    snapshot.values = {"deal_status": "pending_approval" if awaiting else "counter_sent", "campaign": _campaign()}
    snapshot.next = ("approval_gate",) if awaiting else ()
    graph.get_state.return_value = snapshot
    graph.invoke.return_value = invoke_result or {}
    return graph


class TestApprovalRoute(_DbTestCase):
    def test_approve_without_pending_interrupt_returns_409(self):
        graph = _mock_graph(awaiting=False)
        with patch("app.workflow.get_trade_graph", return_value=graph):
            with self.assertRaises(HTTPException) as ctx:
                approve_campaign_deal("CAMP-REG", ApprovalPayload(approved=True), db=self.db)
        self.assertEqual(ctx.exception.status_code, 409)
        graph.invoke.assert_not_called()

    def test_buyer_not_confirmed_when_supplier_lock_fails(self):
        graph = _mock_graph(awaiting=True, invoke_result={
            "deal_status": "closed", "action": "ACCEPT_AND_CLOSE",
            "buyer_draft": "Subject: Acceptance\n\nConfirmed", "supplier_draft": "Subject: Lock\n\nLocked",
            "audit_transcript": [],
        })
        with patch("app.workflow.get_trade_graph", return_value=graph), \
             patch("app.routes.negotiation._dispatch_supplier_approval_email", return_value=False) as supp, \
             patch("app.routes.negotiation._dispatch_buyer_approval_email", return_value=True) as buyer:
            res = approve_campaign_deal("CAMP-REG", ApprovalPayload(approved=True), db=self.db)
        supp.assert_called_once()
        buyer.assert_not_called()
        self.assertEqual(res["deal_status"], "supplier_lock_failed")
        self.assertEqual(self.db_camp.deal_status, "supplier_lock_failed")

    def test_supplier_locked_before_buyer_confirmed(self):
        calls = []
        graph = _mock_graph(awaiting=True, invoke_result={
            "deal_status": "closed", "buyer_draft": "b", "supplier_draft": "s", "audit_transcript": [],
        })
        with patch("app.workflow.get_trade_graph", return_value=graph), \
             patch("app.routes.negotiation._dispatch_supplier_approval_email",
                   side_effect=lambda *a: calls.append("supplier") or True), \
             patch("app.routes.negotiation._dispatch_buyer_approval_email",
                   side_effect=lambda *a: calls.append("buyer") or True):
            approve_campaign_deal("CAMP-REG", ApprovalPayload(approved=True), db=self.db)
        self.assertEqual(calls, ["supplier", "buyer"])

    def test_negotiate_turn_blocked_while_awaiting_approval(self):
        graph = _mock_graph(awaiting=True)
        req = SimulateTurnRequest(campaign_id="CAMP-REG", sender_role="buyer", raw_email="We bid USD 1,000/MT CIF")
        with patch("app.workflow.get_trade_graph", return_value=graph):
            with self.assertRaises(HTTPException) as ctx:
                negotiate_turn(req, db=self.db)
        self.assertEqual(ctx.exception.status_code, 409)
        graph.invoke.assert_not_called()


class TestGmailWorkerInbound(_DbTestCase):
    def setUp(self):
        super().setUp()
        self._orig_graph = app.workflow.trade_graph
        app.workflow.trade_graph = app.workflow.build_trade_graph(checkpointer=MemorySaver())

    def tearDown(self):
        app.workflow.trade_graph = self._orig_graph
        super().tearDown()

    def _config(self) -> dict:
        return {"configurable": {"thread_id": "CAMP-REG"}}

    def _launch_campaign(self, stack: ExitStack) -> None:
        app.workflow.get_trade_graph().invoke({
            "campaign": _campaign(), "negotiation_round": 0, "deal_status": "initiating", "pipeline_step": 0,
            "latest_email": "", "active_role": "buyer", "audit_transcript": [], "skip_email_dispatch": True,
        }, config=self._config())

    def _process(self, sender: str, body: str, thread_id: str = "gmail-thread-1",
                 msg_id: str | None = None, use_gmail_api: bool = False) -> None:
        import uuid

        gmail_worker._process_inbound_message_transaction(
            self.db_camp, body, sender, "Re: SCO", msg_id or f"<{uuid.uuid4().hex}@test>", "", "", thread_id,
            {"desk@arbitrage.com"}, self.db, use_gmail_api=use_gmail_api,
        )

    def test_supplier_sender_classified_as_supplier(self):
        role = gmail_worker._classify_sender_role("Indus Rice <export@indusrice.pk>", {}, "Basmati 1121")
        self.assertEqual(role, "supplier")
        self.assertEqual(gmail_worker._classify_sender_role("buyer@dubai.com", {}, "Basmati 1121"), "buyer")

    def test_first_buyer_email_does_not_fabricate_supplier(self):
        with ExitStack() as stack:
            _offline_patches(stack)
            stack.enter_context(patch.object(settings, "my_test_supplier_email", ""))
            transport = stack.enter_context(patch("app.services.gmail_worker._dispatch_outbound_reply_transport"))
            self._launch_campaign(stack)
            self._process("Buyer <buyer@dubai.com>", "We bid USD 1,000/MT CIF Jebel Ali for 500 MT.")

        state = app.workflow.get_trade_graph().get_state(self._config()).values
        self.assertIsNone(state.get("supplier_terms"))
        self.assertFalse(app.workflow.is_awaiting_approval(self._config()))
        self.assertEqual(state.get("gmail_thread_id"), "gmail-thread-1")  # declared key survives checkpointing
        recipients = [c.args[0] for c in transport.call_args_list]
        self.assertIn("export@indusrice.pk", recipients)  # RFQ sent to the real supplier

    def test_supplier_reply_parsed_as_supplier_terms(self):
        with ExitStack() as stack:
            _offline_patches(stack)
            stack.enter_context(patch("app.services.gmail_worker._dispatch_outbound_reply_transport"))
            self._launch_campaign(stack)
            self._process("Buyer <buyer@dubai.com>", "We bid USD 1,000/MT CIF Jebel Ali for 500 MT.")
            self._process("Indus Rice <export@indusrice.pk>", "We quote USD 880/MT FOB Karachi for 500 MT.",
                          thread_id="gmail-thread-supplier")

        state = app.workflow.get_trade_graph().get_state(self._config()).values
        self.assertIsNotNone(state.get("supplier_terms"))
        self.assertEqual(state["supplier_terms"].price_usd_per_mt, 880.0)
        self.assertEqual(state["buyer_terms"].price_usd_per_mt, 1000.0)
        self.assertEqual(state.get("gmail_thread_id"), "gmail-thread-1")  # buyer thread untouched

    def test_inbound_while_awaiting_approval_skips_graph(self):
        graph = _mock_graph(awaiting=True)
        with patch("app.workflow.get_trade_graph", return_value=graph):
            self._process("Buyer <buyer@dubai.com>", "Any update? We bid USD 1,000/MT CIF.")
        graph.invoke.assert_not_called()
        audits = self.db.query(TradeAuditModel).filter(TradeAuditModel.campaign_id == "CAMP-REG").all()
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0].direction, "INBOUND")

    def test_supplier_test_inbox_receives_rfq_and_is_recognized(self):
        supplier_inbox = "desk-tester+supplier@example.org"
        with ExitStack() as stack:
            _offline_patches(stack)
            stack.enter_context(patch.object(settings, "my_test_supplier_email", supplier_inbox))
            transport = stack.enter_context(patch("app.services.gmail_worker._dispatch_outbound_reply_transport"))
            self._launch_campaign(stack)
            self._process("Buyer <buyer@dubai.com>", "We bid USD 1,000/MT CIF Jebel Ali for 500 MT.")
            recipients = [c.args[0] for c in transport.call_args_list]
            self.assertIn(supplier_inbox, recipients)
            self.assertNotIn("export@indusrice.pk", recipients)

            self._process(f"Test Supplier <{supplier_inbox}>", "We quote USD 880/MT FOB Karachi for 500 MT.",
                          thread_id="gmail-thread-supplier")

        state = app.workflow.get_trade_graph().get_state(self._config()).values
        self.assertEqual(state["supplier_terms"].price_usd_per_mt, 880.0)
        self.assertEqual(state["buyer_terms"].price_usd_per_mt, 1000.0)


class TestSupplierTestInbox(_DbTestCase):
    INBOX = "desk-tester+supplier@example.org"

    def test_resolver_prefers_test_inbox_over_state_and_directory(self):
        from app.directory import resolve_supplier_contact

        with patch.object(settings, "my_test_supplier_email", self.INBOX):
            self.assertEqual(resolve_supplier_contact("Basmati 1121", "old@indusrice.pk")[1], self.INBOX)
        with patch.object(settings, "my_test_supplier_email", ""):
            self.assertEqual(resolve_supplier_contact("Basmati 1121", "old@indusrice.pk")[1], "old@indusrice.pk")
            self.assertEqual(resolve_supplier_contact("Basmati 1121")[1], "export@indusrice.pk")

    def test_classifier_and_polling_allowlist_include_test_inbox(self):
        with patch.object(settings, "my_test_supplier_email", self.INBOX):
            self.assertEqual(gmail_worker._classify_sender_role(f"S <{self.INBOX}>", {}, "Basmati 1121"), "supplier")
            self.assertIn(self.INBOX, gmail_worker._collect_allowed_senders(self.db))

    def test_test_inbox_is_not_treated_as_simulated(self):
        from app.email_service import is_simulated_email

        with patch.object(settings, "my_test_supplier_email", self.INBOX):
            self.assertFalse(is_simulated_email(self.INBOX))

    def test_volume_lock_goes_to_supplier_inbox_not_buyer_inbox(self):
        from app.routes.negotiation import _dispatch_supplier_approval_email

        state = {"supplier_draft": "Subject: Lock\n\nLocked", "deal_status": "closed", "campaign": _campaign()}
        with patch.object(settings, "my_test_email", "desk-tester+buyer@example.org"), \
             patch.object(settings, "my_test_supplier_email", self.INBOX), \
             patch("app.routes.negotiation._send_via_configured_transport", return_value=True) as send:
            self.assertTrue(_dispatch_supplier_approval_email(state, self.db_camp))
        self.assertEqual(send.call_args.args[0], self.INBOX)


class TestHeadlessGmailAuth(unittest.TestCase):
    def setUp(self):
        import app.gmail_client as gc

        self.gc = gc
        self._cached = gc._cached_credentials
        gc._cached_credentials = None

    def tearDown(self):
        self.gc._cached_credentials = self._cached

    def _settings(self, **values):
        stack = ExitStack()
        defaults = dict(gmail_client_id="", gmail_client_secret="", gmail_refresh_token="",
                        google_token_json_path="__missing_token__.json",
                        google_credentials_json_path="__missing_credentials__.json")
        defaults.update(values)
        for key, val in defaults.items():
            stack.enter_context(patch.object(settings, key, val))
        return stack

    def test_refresh_token_from_settings_is_used_without_browser(self):
        with self._settings(gmail_client_id="cid", gmail_client_secret="csecret", gmail_refresh_token="rtok"), \
             patch("google.oauth2.credentials.Credentials.refresh") as refresh:
            creds = self.gc._load_credentials()
        refresh.assert_called_once()
        self.assertEqual((creds.refresh_token, creds.client_id, creds.client_secret), ("rtok", "cid", "csecret"))
        self.assertFalse(hasattr(self.gc, "InstalledAppFlow"))  # no interactive flow left in the server

    def test_client_id_and_secret_fall_back_to_credentials_json(self):
        import json
        import os
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump({"installed": {"client_id": "file-cid", "client_secret": "file-secret"}}, f)
        try:
            with self._settings(gmail_refresh_token="rtok", google_credentials_json_path=path), \
                 patch("google.oauth2.credentials.Credentials.refresh"):
                creds = self.gc._load_credentials()
        finally:
            os.remove(path)
        self.assertEqual((creds.client_id, creds.client_secret), ("file-cid", "file-secret"))

    def test_rejected_refresh_token_raises_actionable_error(self):
        with self._settings(gmail_client_id="cid", gmail_client_secret="csecret", gmail_refresh_token="revoked"), \
             patch("google.oauth2.credentials.Credentials.refresh", side_effect=Exception("invalid_grant")):
            with self.assertRaises(RuntimeError) as ctx:
                self.gc._load_credentials()
        self.assertIn("scripts/gmail_auth.py", str(ctx.exception))
        self.assertIsNone(self.gc._cached_credentials)

    def test_unconfigured_gmail_fails_fast(self):
        with self._settings():
            with self.assertRaises(FileNotFoundError):
                self.gc._load_credentials()


class TestLoggingAndPricingAndHistory(_DbTestCase):
    def test_email_and_gmail_loggers_reach_app_log(self):
        import app.email_service
        import app.gmail_client
        import app.market

        for module in (app.email_service, app.gmail_client, app.market):
            self.assertTrue(module.logger.name.startswith("arbitrage_desk."), module.logger.name)

    def test_opening_anchor_never_below_cif_floor(self):
        from app.agents.buyer_agent import _calculate_pricing_parameters

        campaign = _campaign()
        for benchmark in (900.0, 999.0, 1200.0):
            state = {"benchmark_fob_usd": benchmark, "freight_cost_usd": FREIGHT}
            _, _, bounds, anchor = _calculate_pricing_parameters(campaign, state, "Karachi", "Jebel Ali")
            self.assertGreaterEqual(anchor, bounds.dynamic_cif_floor, f"benchmark {benchmark}")

    def test_status_history_includes_database_turns(self):
        from app.routes.campaigns import _load_or_reconstruct_transcript

        for direction, role, msg in [("OUTBOUND", "agent", "Subject: Offer\n\nSCO"),
                                     ("INBOUND", "buyer", "We bid USD 1,200/MT CIF"),
                                     ("INBOUND", "supplier", "We quote USD 1,000/MT FOB"),
                                     ("OUTBOUND", "agent", "Subject: TRADE CONFIRMATION\n\nLocked")]:
            self.db.add(TradeAuditModel(campaign_id="CAMP-REG", thread_id="CAMP-REG", role=role,
                                        raw_message=msg, direction=direction))
        self.db.commit()
        state = {"audit_transcript": [{"turn": 0, "action": "OUTBOUND_SCO", "message": "SCO"}]}
        history = _load_or_reconstruct_transcript(state, "CAMP-REG", self.db)
        self.assertEqual(len(history), 4)
        self.assertEqual([h["role"] for h in history], ["agent", "buyer", "supplier", "agent"])
        self.assertEqual(history[3]["subject"], "TRADE CONFIRMATION")


class TestCheckpointSerde(unittest.TestCase):
    def test_domain_models_round_trip_through_allow_listed_serde(self):
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

        from app.graph import build_checkpoint_serde

        # Control: future strict LangGraph behaviour turns unregistered models into plain dicts.
        strict = JsonPlusSerializer(allowed_msgpack_modules=None)
        self.assertNotIsInstance(strict.loads_typed(strict.dumps_typed(_campaign())), Campaign)

        serde = build_checkpoint_serde()
        terms = _terms("supplier", 880.0)
        restored_campaign = serde.loads_typed(serde.dumps_typed(_campaign()))
        restored_terms = serde.loads_typed(serde.dumps_typed(terms))
        self.assertIsInstance(restored_campaign, Campaign)
        self.assertIsInstance(restored_terms, ParsedEmail)
        self.assertEqual(restored_terms.price_usd_per_mt, 880.0)


class TestExactlyOnceInbound(TestGmailWorkerInbound):
    """Duplicate deliveries (push + poll, two workers, Pub/Sub redelivery) produce one desk reply."""

    def test_same_email_via_push_and_poll_is_answered_once(self):
        with ExitStack() as stack:
            _offline_patches(stack)
            stack.enter_context(patch.object(settings, "my_test_supplier_email", ""))
            transport = stack.enter_context(patch("app.services.gmail_worker._dispatch_outbound_reply_transport"))
            self._launch_campaign(stack)
            body = "We bid USD 1,000/MT CIF Jebel Ali for 500 MT."
            self._process("Buyer <buyer@dubai.com>", body, msg_id="<dup-1@dubai.com>", use_gmail_api=True)
            sends_after_first = transport.call_count
            self._process("Buyer <buyer@dubai.com>", body, msg_id="<dup-1@dubai.com>", use_gmail_api=False)
            self._process("Buyer <buyer@dubai.com>", body, msg_id="<DUP-1@dubai.com>", use_gmail_api=True)

        self.assertGreater(sends_after_first, 0)
        self.assertEqual(transport.call_count, sends_after_first)  # no second set of replies
        state = app.workflow.get_trade_graph().get_state(self._config()).values
        self.assertEqual(state["negotiation_round"], 1)  # graph ran once
        row = self.db.query(ProcessedInboundEmailModel).one()
        self.assertEqual((row.message_key, row.source, row.status), ("dup-1@dubai.com", "push", "done"))

    def test_second_worker_cannot_claim_same_email(self):
        from app.services.inbound_dedupe import claim_message

        other_worker_db = self.SessionLocal()
        try:
            self.assertTrue(claim_message(self.db, "m-1@x", "push"))
            self.assertFalse(claim_message(other_worker_db, "m-1@x", "push"))
            self.assertFalse(claim_message(other_worker_db, "m-1@x", "poll"))
        finally:
            other_worker_db.close()

    def test_failed_processing_releases_claim_for_retry(self):
        with patch("app.services.gmail_worker._run_inbound_message_transaction", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self._process("Buyer <buyer@dubai.com>", "bid", msg_id="<retry-1@x>")
        self.assertEqual(self.db.query(ProcessedInboundEmailModel).count(), 0)
        with patch("app.services.gmail_worker._run_inbound_message_transaction") as run:
            self._process("Buyer <buyer@dubai.com>", "bid", msg_id="<retry-1@x>")
        run.assert_called_once()

    def test_email_without_message_id_is_keyed_by_content(self):
        with patch("app.services.gmail_worker._run_inbound_message_transaction") as run:
            for _ in range(2):
                gmail_worker._process_inbound_message_transaction(
                    self.db_camp, "same body", "b@x.com", "Re: SCO", "", "", "", "", set(), self.db, use_gmail_api=False
                )
        run.assert_called_once()


class TestPushIngestion(unittest.TestCase):
    def setUp(self):
        self._cursor = gmail_worker.LAST_SEEN_HISTORY_ID

    def tearDown(self):
        gmail_worker.LAST_SEEN_HISTORY_ID = self._cursor

    def _run(self, history_id: str):
        import asyncio

        asyncio.run(gmail_worker.process_incoming_gmail_event(history_id))

    def test_stale_notification_skipped_and_cursor_never_moves_back(self):
        gmail_worker.LAST_SEEN_HISTORY_ID = "2271236"
        with patch("app.services.gmail_worker.fetch_latest_messages_by_history", return_value=[]) as fetch:
            self._run("2271028")  # older backlog notification
            fetch.assert_not_called()
            self.assertEqual(gmail_worker.LAST_SEEN_HISTORY_ID, "2271236")
            self._run("2271300")
            fetch.assert_called_once_with("2271236")
            self.assertEqual(gmail_worker.LAST_SEEN_HISTORY_ID, "2271300")

    def test_message_marked_processed_only_after_success(self):
        gmail_worker.LAST_SEEN_HISTORY_ID = "100"
        with patch("app.services.gmail_worker.fetch_latest_messages_by_history", return_value=[{"id": "g-ok"}, {"id": "g-bad"}]), \
             patch("app.services.gmail_worker._handle_single_push_message",
                   side_effect=lambda m, d: (_ for _ in ()).throw(RuntimeError("x")) if m["id"] == "g-bad" else None), \
             patch("app.services.gmail_worker.mark_gmail_message_processed") as mark:
            self._run("101")
        mark.assert_called_once_with("g-ok")

    def test_webhook_ignored_in_polling_mode(self):
        import base64
        import json

        from starlette.testclient import TestClient

        import app.main

        payload = {"message": {"data": base64.b64encode(json.dumps({"historyId": 555}).encode()).decode()}}
        with patch.object(settings, "use_push_webhooks", False), \
             patch("app.routes.webhooks.process_incoming_gmail_event") as process:
            res = TestClient(app.main.app).post("/api/webhooks/gmail", json=payload)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "ignored")
        process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
