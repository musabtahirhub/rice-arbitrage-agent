"""Desk-managed contact book (/api/counterparties) and launching a campaign at picked contacts.

A picked buyer/supplier wins over the MY_TEST_* inboxes; without a pick the test inboxes still apply.
Runs offline: SQLite + MemorySaver, all LLM/market/email I/O patched.
"""
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

import app.graph.workflow
import app.main
from app.config import settings
from app.db.database import get_db
from app.db.db_models import Base, CampaignModel, CounterpartyModel, TradeAuditModel
from app.directory import resolve_supplier_contact
from app.graph.graph import build_checkpoint_serde, build_trade_graph
from app.models import Campaign
from app.services.mail.inbound_state import classify_sender_role, resolve_deal_contacts
from tests.test_regressions import _offline_patches

TEST_BUYER = "test-buyer@example.com"
TEST_SUPPLIER = "test-supplier@example.com"


class TestCounterpartyApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(bind=engine)
        cls.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def setUp(self):
        self.stack = ExitStack()
        _offline_patches(self.stack)
        self.sent = self.stack.enter_context(patch("app.graph.agents.buyer_agent.send_email", return_value=True))
        self.stack.enter_context(patch.object(settings, "my_test_email", TEST_BUYER))
        self.stack.enter_context(patch.object(settings, "my_test_supplier_email", TEST_SUPPLIER))
        self.stack.enter_context(patch.object(settings, "email_user", "desk@example.com"))
        # No Langfuse: a real client would export to the cloud and stay cached for later tests.
        self.stack.enter_context(patch.object(settings, "langfuse_public_key", ""))
        self.stack.enter_context(patch.object(settings, "langfuse_secret_key", ""))
        graph = build_trade_graph(checkpointer=MemorySaver(serde=build_checkpoint_serde()))
        self.stack.enter_context(patch.object(app.graph.workflow, "trade_graph", graph))

        def _db():
            db = self.SessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.main.app.dependency_overrides[get_db] = _db
        self.client = TestClient(app.main.app)

    def tearDown(self):
        app.main.app.dependency_overrides.pop(get_db, None)
        self.stack.close()
        db = self.SessionLocal()
        for model in (TradeAuditModel, CampaignModel, CounterpartyModel):
            db.query(model).delete()
        db.commit()
        db.close()

    def _add(self, role: str, name: str, email: str, expect: int = 201) -> dict:
        res = self.client.post("/api/counterparties", json={"role": role, "name": name, "email": email})
        self.assertEqual(res.status_code, expect, res.text)
        return res.json()

    def _launch(self, expect: int = 200, **picks) -> dict:
        res = self.client.post("/api/campaigns", json={"commodity": "Basmati 1121", **picks})
        self.assertEqual(res.status_code, expect, res.text)
        return res.json()

    def _state(self, campaign_id: str) -> dict:
        return app.graph.workflow.get_state_values({"configurable": {"thread_id": campaign_id}})

    def test_create_list_delete(self):
        buyer = self._add("buyer", "  Gulf Foods  ", " Buy@Gulf.ae ")
        self.assertEqual((buyer["name"], buyer["email"]), ("Gulf Foods", "buy@gulf.ae"))
        self._add("supplier", "Indus Mills", "sales@indus.pk")
        book = self.client.get("/api/counterparties").json()
        self.assertEqual([b["email"] for b in book["buyers"]], ["buy@gulf.ae"])
        self.assertEqual([s["email"] for s in book["suppliers"]], ["sales@indus.pk"])

        self.assertEqual(self.client.delete(f"/api/counterparties/{buyer['id']}").status_code, 200)
        self.assertEqual(self.client.get("/api/counterparties").json()["buyers"], [])
        self.assertEqual(self.client.delete(f"/api/counterparties/{buyer['id']}").status_code, 404)

    def test_duplicate_invalid_and_desk_address_rejected(self):
        self._add("buyer", "Gulf Foods", "buy@gulf.ae")
        self._add("buyer", "Gulf Foods again", "BUY@gulf.ae", expect=409)
        clash = self.client.post("/api/counterparties",
                                 json={"role": "supplier", "name": "Same address", "email": "BUY@gulf.ae"})
        self.assertEqual(clash.status_code, 409)  # a buyer and a supplier cannot share an address
        self.assertIn("already saved as a buyer", clash.json()["detail"])
        self._add("buyer", "Bad", "not-an-email", expect=422)
        self._add("buyer", "Desk", "desk@example.com", expect=422)

    def test_launch_with_picks_mails_picked_buyer_and_stores_both(self):
        buyer = self._add("buyer", "Gulf Foods", "buy@gulf.ae")
        supplier = self._add("supplier", "Indus Mills", "sales@indus.pk")
        launched = self._launch(buyer_id=buyer["id"], supplier_id=supplier["id"])

        self.assertEqual(self.sent.call_args.kwargs["to_email"], "buy@gulf.ae")  # not MY_TEST_EMAIL
        state = self._state(launched["campaign_id"])
        self.assertEqual((state["target_buyer_name"], state["target_buyer_email"]), ("Gulf Foods", "buy@gulf.ae"))
        self.assertEqual(state["target_supplier_email"], "sales@indus.pk")

        status = self.client.get(f"/api/campaigns/{launched['campaign_id']}").json()
        self.assertEqual(status["target_supplier_name"], "Indus Mills")
        self.assertEqual(status["target_buyer_email"], "buy@gulf.ae")

    def test_launch_without_picks_uses_test_inboxes(self):
        launched = self._launch()
        self.assertEqual(self.sent.call_args.kwargs["to_email"], TEST_BUYER)
        self.assertIsNone(self._state(launched["campaign_id"]).get("target_supplier_email"))

    def test_picks_that_swap_the_test_inboxes_classify_by_campaign(self):
        """Buyer picked = MY_TEST_SUPPLIER_EMAIL and supplier picked = MY_TEST_EMAIL (the live bug)."""
        buyer = self._add("buyer", "aa", TEST_SUPPLIER)
        supplier = self._add("supplier", "bb", TEST_BUYER)
        launched = self._launch(buyer_id=buyer["id"], supplier_id=supplier["id"])
        self.assertEqual(self.sent.call_args.kwargs["to_email"], TEST_SUPPLIER)
        state = self._state(launched["campaign_id"])
        self.assertEqual(classify_sender_role(f"aa <{TEST_SUPPLIER}>", state, "Basmati 1121"), "buyer")
        self.assertEqual(classify_sender_role(f"bb <{TEST_BUYER}>", state, "Basmati 1121"), "supplier")

    def test_polling_allowlist_includes_saved_and_campaign_contacts(self):
        from app.services.mail.gmail_worker import _collect_allowed_senders

        buyer = self._add("buyer", "Gulf Foods", "buy@gulf.ae")
        supplier = self._add("supplier", "Indus Mills", "sales@indus.pk")
        self._launch(buyer_id=buyer["id"], supplier_id=supplier["id"])
        self.client.delete(f"/api/counterparties/{supplier['id']}")  # still on the running campaign
        db = self.SessionLocal()
        try:
            allowed = _collect_allowed_senders(db)
        finally:
            db.close()
        self.assertTrue({"buy@gulf.ae", "sales@indus.pk"} <= allowed)

    def test_launch_rejects_unknown_wrong_role_and_shared_address(self):
        buyer = self._add("buyer", "Gulf Foods", "buy@gulf.ae")
        self._launch(expect=422, buyer_id="CP-NOPE")
        self._launch(expect=422, supplier_id=buyer["id"])  # a buyer is not a supplier
        # A clashing pair saved before the one-address-one-role rule is still refused at launch.
        db = self.SessionLocal()
        db.add(CounterpartyModel(id="CP-OLD", role="supplier", name="Clash", email="buy@gulf.ae"))
        db.commit()
        db.close()
        self._launch(expect=422, buyer_id=buyer["id"], supplier_id="CP-OLD")
        test_inbox_buyer = self._add("buyer", "Uses supplier test inbox", TEST_SUPPLIER)
        self._launch(expect=422, buyer_id=test_inbox_buyer["id"])
        self.sent.assert_not_called()


class TestContactPrecedence(unittest.TestCase):
    def test_supplier_contact_state_then_test_inbox_then_directory(self):
        with patch.object(settings, "my_test_supplier_email", TEST_SUPPLIER):
            self.assertEqual(resolve_supplier_contact("Basmati 1121", "picked@mill.pk", "Picked")[1], "picked@mill.pk")
            self.assertEqual(resolve_supplier_contact("Basmati 1121")[1], TEST_SUPPLIER)
            self.assertEqual(resolve_supplier_contact("Basmati 1121", "export@indusrice.pk")[1], TEST_SUPPLIER)
        with patch.object(settings, "my_test_supplier_email", ""):
            self.assertEqual(resolve_supplier_contact("Basmati 1121")[1], "export@indusrice.pk")

    def test_supplier_turn_answers_picked_buyer(self):
        state = {"target_buyer_name": "Gulf Foods", "target_buyer_email": "buy@gulf.ae",
                 "target_supplier_email": "sales@indus.pk", "target_supplier_name": "Indus Mills"}
        with patch.object(settings, "my_test_email", TEST_BUYER), \
                patch.object(settings, "my_test_supplier_email", TEST_SUPPLIER):
            contacts = resolve_deal_contacts(
                Campaign(campaign_id="C1"), "Indus <sales@indus.pk>", {"desk@example.com"}, state, "supplier"
            )
        self.assertEqual((contacts.buyer_name, contacts.buyer_email), ("Gulf Foods", "buy@gulf.ae"))
        self.assertEqual((contacts.supplier_name, contacts.supplier_email), ("Indus Mills", "sales@indus.pk"))


if __name__ == "__main__":
    unittest.main()
