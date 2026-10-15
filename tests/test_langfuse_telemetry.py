import os
import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.config import Settings, settings
from app.db_models import Base, CampaignModel
from app.models import ApprovalPayload, Campaign, CreateCampaignRequest, ParsedEmail
import app.database
import app.main
import app.workflow
from app.telemetry import get_langfuse_handler, CallbackHandler
from app.routes.campaigns import create_campaign
from app.routes.negotiation import approve_campaign_deal
from app.services.llm_service import generate_dynamic_llm_draft, _extract_terms_via_gemini_llm


class TestLangfuseTelemetry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        cls.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)
        Base.metadata.create_all(bind=cls.engine)

        app.database.engine = cls.engine
        app.database.SessionLocal = cls.SessionLocal
        app.main.SessionLocal = cls.SessionLocal

        def _override_get_db():
            db = cls.SessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.main.app.dependency_overrides[app.database.get_db] = _override_get_db
        cls.client = TestClient(app.main.app)

    def setUp(self):
        self.db = self.SessionLocal()

    def tearDown(self):
        self.db.close()

    def test_settings_langfuse_fields(self):
        """1. Verify Langfuse settings fields exist with proper defaults and env loading."""
        with patch.dict(os.environ, {}, clear=True):
            s = Settings(_env_file=None)
            self.assertEqual(s.langfuse_public_key, "")
            self.assertEqual(s.langfuse_secret_key, "")
            self.assertEqual(s.langfuse_host, "https://cloud.langfuse.com")

        with patch.dict(
            os.environ,
            {
                "LANGFUSE_PUBLIC_KEY": "pk-test-123",
                "LANGFUSE_SECRET_KEY": "sk-test-456",
                "LANGFUSE_HOST": "https://custom.langfuse.com",
            },
            clear=True,
        ):
            s = Settings(_env_file=None)
            self.assertEqual(s.langfuse_public_key, "pk-test-123")
            self.assertEqual(s.langfuse_secret_key, "sk-test-456")
            self.assertEqual(s.langfuse_host, "https://custom.langfuse.com")

    def test_telemetry_get_langfuse_handler_missing_keys(self):
        """2. Verify get_langfuse_handler returns None if keys are empty."""
        with patch.object(settings, "langfuse_public_key", ""), patch.object(settings, "langfuse_secret_key", ""):
            handler = get_langfuse_handler()
            self.assertIsNone(handler)

        with patch.object(settings, "langfuse_public_key", "pk-test"), patch.object(settings, "langfuse_secret_key", ""):
            handler = get_langfuse_handler()
            self.assertIsNone(handler)

    def test_telemetry_get_langfuse_handler_present_keys(self):
        """2. Verify get_langfuse_handler returns CallbackHandler if keys are configured."""
        import app.telemetry as telemetry

        # A mock client keeps the test offline (a real client would try to export to Langfuse cloud).
        with patch.object(settings, "langfuse_public_key", "pk-custom-key"), \
             patch.object(settings, "langfuse_secret_key", "sk-custom-secret"), \
             patch.object(settings, "langfuse_host", "https://cloud.langfuse.com"), \
             patch.object(telemetry, "_langfuse_client", MagicMock()):
            handler = get_langfuse_handler()
            self.assertIsNotNone(handler)
            if CallbackHandler is not None:
                self.assertIsInstance(handler, CallbackHandler)

    def test_campaign_create_attaches_callbacks_metadata_and_flushes(self):
        """3. In POST /api/campaigns, verify Langfuse callback and execution config are passed to trade_graph.invoke and flushed."""
        mock_handler = MagicMock()
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {
            "benchmark_fob_usd": 900.0,
            "freight_cost_usd": 45.0,
            "dynamic_fob_ceiling": 945.0,
            "dynamic_cif_floor": 1111.0,
            "anchor_cif_usd": 1150.0,
            "buyer_draft": "SCO draft",
            "deal_status": "prospecting",
            "pipeline_step": 1,
            "discovered_buyers": [],
            "discovered_suppliers": [],
            "audit_transcript": [],
        }

        with patch("app.routes.campaigns.get_langfuse_handler", return_value=mock_handler), \
             patch("app.routes.campaigns.flush_telemetry") as mock_flush, \
             patch("app.workflow.get_trade_graph", return_value=mock_graph):
            req = CreateCampaignRequest(
                commodity="Basmati 1121",
                target_volume_mt=500.0,
                target_margin_pct=10.0,
                destination_port="Jebel Ali",
                auto_run=False,
            )
            res = create_campaign(req, db=self.db)
            self.assertEqual(res["commodity"], "Basmati 1121")

            mock_graph.invoke.assert_called_once()
            called_state, called_kwargs = mock_graph.invoke.call_args
            config = called_kwargs.get("config", {})

            self.assertIn("configurable", config)
            self.assertTrue(config["configurable"]["thread_id"].startswith("CAMP-"))
            self.assertEqual(config["callbacks"], [mock_handler])
            self.assertEqual(config["metadata"]["commodity"], "Basmati 1121")
            self.assertEqual(config["metadata"]["target_volume_mt"], 500.0)
            self.assertIn("commodity-arbitrage", config["tags"])
            self.assertIn("Basmati 1121", config["tags"])

            mock_flush.assert_called_once()

    def test_hitl_approval_attaches_callbacks_metadata_and_flushes(self):
        """3. In POST /api/campaigns/{id}/approve, verify Langfuse callbacks and metadata are attached and flushed."""
        campaign_id = "CAMP-HITL01"
        db_camp = CampaignModel(
            id=campaign_id,
            commodity="Basmati 1121",
            target_volume_mt=500.0,
            destination_port="Jebel Ali",
            origin_port_default="Karachi",
            deal_status="pending_approval",
        )
        self.db.merge(db_camp)
        self.db.commit()

        mock_handler = MagicMock()
        mock_graph = MagicMock()
        mock_snapshot = MagicMock()
        mock_snapshot.values = {"deal_status": "pending_approval"}
        mock_snapshot.next = ("approval_gate",)
        mock_graph.get_state.return_value = mock_snapshot
        mock_graph.invoke.return_value = {
            "deal_status": "closed",
            "action": "ACCEPT_AND_CLOSE",
            "deal_approved_by_human": True,
            "reviewer_notes": "All looks good",
            "override_cif_price": None,
            "last_counter_cif_usd": 1150.0,
            "evaluation_reason": "Viable deal",
            "is_deal_viable": True,
            "net_spread_usd": 185.0,
            "net_margin_pct": 19.17,
            "pipeline_step": 4,
            "negotiation_round": 3,
            "buyer_terms": None,
            "supplier_terms": None,
            "buyer_draft": "Confirmed",
            "supplier_draft": "Locked",
            "audit_transcript": [],
        }

        with patch("app.routes.negotiation.get_langfuse_handler", return_value=mock_handler), \
             patch("app.routes.negotiation.flush_telemetry") as mock_flush, \
             patch("app.workflow.get_trade_graph", return_value=mock_graph), \
             patch("app.routes.negotiation._dispatch_buyer_approval_email"), \
             patch("app.routes.negotiation._dispatch_supplier_approval_email"):
            payload = ApprovalPayload(approved=True, reviewer_notes="Approved by trader")
            res = approve_campaign_deal(campaign_id, payload, db=self.db)
            self.assertEqual(res["deal_status"], "closed")

            mock_graph.invoke.assert_called_once()
            called_args, called_kwargs = mock_graph.invoke.call_args
            config = called_kwargs.get("config", {})

            self.assertEqual(config["configurable"]["thread_id"], campaign_id)
            self.assertEqual(config["callbacks"], [mock_handler])
            self.assertEqual(config["metadata"]["action"], "HITL_DECISION")
            self.assertEqual(config["metadata"]["approved"], True)

            mock_flush.assert_called_once()

    def test_gmail_worker_attaches_callbacks_to_runtime_config(self):
        """3. In Gmail worker background turns, verify callbacks: [langfuse_handler] and thread_id are included."""
        from app.services.gmail_worker import _build_inbound_config, _process_inbound_message_transaction

        db_camp = CampaignModel(
            id="CAMP-GMAIL01",
            commodity="Basmati 1121",
            target_volume_mt=500.0,
            destination_port="Jebel Ali",
            origin_port_default="Karachi",
            deal_status="prospecting",
        )
        mock_handler = MagicMock()

        with patch("app.services.gmail_worker.get_langfuse_handler", return_value=mock_handler), \
             patch("app.services.gmail_worker.flush_telemetry") as mock_flush, \
             patch("app.workflow.get_trade_graph") as mock_get_graph, \
             patch("app.services.gmail_worker._dispatch_outbound_reply_transport"):
            mock_graph = MagicMock()
            mock_graph.get_state.return_value = None
            mock_graph.invoke.return_value = {
                "negotiation_round": 1,
                "deal_status": "counter_sent",
                "action": "COUNTER_OFFER",
                "campaign": Campaign(campaign_id="CAMP-GMAIL01"),
            }
            mock_get_graph.return_value = mock_graph

            config, _ = _build_inbound_config(db_camp)

            self.assertEqual(config["configurable"]["thread_id"], "CAMP-GMAIL01")
            self.assertEqual(config["callbacks"], [mock_handler])

            _process_inbound_message_transaction(
                db_camp, "We bid $950 CIF", "buyer@dubai.com", "Re: SCO", "<msg1@test.com>",
                "", "", "thread1", set(), self.db, use_gmail_api=False
            )

            mock_flush.assert_called_once()

    def test_direct_gemini_llm_calls_pass_config(self):
        """4. Where nodes call LangChain chat models, verify config dictionary is passed to llm.invoke(prompt, config=config)."""
        mock_llm = MagicMock()
        mock_resp = MagicMock()
        mock_resp.content = "Subject: Counter Offer\n\nWe offer $1120/MT CIF."
        mock_llm.invoke.return_value = mock_resp

        test_config = {
            "configurable": {"thread_id": "CAMP-TEST-CONFIG"},
            "callbacks": ["mock_callback"],
            "metadata": {"test": "val"},
        }

        with patch("app.services.llm_service._get_llm", return_value=mock_llm):
            draft = generate_dynamic_llm_draft(
                prompt="Draft counter offer",
                fallback_subject="Counter",
                fallback_body="Counter body",
                config=test_config,
            )
            mock_llm.invoke.assert_called_once_with("Draft counter offer", config=test_config)
            self.assertIn("Subject: Counter Offer", draft)

        mock_llm.reset_mock()
        mock_resp.content = '{"intent": "COUNTER_OFFER", "is_acceptance": false, "price_usd_per_mt": 950.0, "quantity_mt": 500.0, "port": "Jebel Ali", "summary": "Counter bid"}'

        # The extractor returns early without an API key; set a dummy one so the test doesn't depend on .env.
        with patch("app.services.llm_service._get_llm", return_value=mock_llm), \
             patch.object(settings, "gemini_api_key", "test-key"):
            parsed = _extract_terms_via_gemini_llm(
                raw_text="We bid 950 USD/MT",
                role="buyer",
                last_proposed_price=1100.0,
                commodity="Basmati 1121",
                default_volume_mt=500.0,
                config=test_config,
            )
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed.price_usd_per_mt, 950.0)
            mock_llm.invoke.assert_called_once()
            _, called_kwargs = mock_llm.invoke.call_args
            self.assertEqual(called_kwargs.get("config"), test_config)

    def test_requirements_file_contains_langfuse(self):
        """5. Verify requirements.txt and pyproject.toml pin the Langfuse v5 SDK."""
        root = os.path.dirname(os.path.dirname(__file__))
        for name in ("requirements.txt", "pyproject.toml"):
            with open(os.path.join(root, name), "r", encoding="utf-8") as f:
                self.assertIn("langfuse>=5.0.0,<6", f.read(), name)

    def test_flush_telemetry_flushes_native_client(self):
        """6. flush_telemetry() flushes the native Langfuse client and never raises."""
        import app.telemetry as telemetry

        mock_client = MagicMock()
        with patch.object(telemetry, "get_langfuse_client", return_value=mock_client):
            telemetry.flush_telemetry()
            mock_client.flush.assert_called_once()

            mock_client.flush.side_effect = RuntimeError("network down")
            telemetry.flush_telemetry()  # logged, not raised

        with patch.object(telemetry, "get_langfuse_client", return_value=None):
            telemetry.flush_telemetry()  # tracing disabled: no-op

    def test_langfuse_client_is_singleton_and_disabled_without_keys(self):
        """6b. One native client per process; no client or handler when keys are missing."""
        import app.telemetry as telemetry

        if telemetry.Langfuse is None:
            self.skipTest("langfuse not installed")
        with patch.object(telemetry, "_langfuse_client", None), \
             patch.object(settings, "langfuse_public_key", ""), \
             patch.object(settings, "langfuse_secret_key", ""):
            self.assertIsNone(telemetry.get_langfuse_client())
            self.assertIsNone(telemetry.get_langfuse_handler())

        with patch.object(telemetry, "_langfuse_client", None), \
             patch.object(settings, "langfuse_public_key", "pk-lf-test"), \
             patch.object(settings, "langfuse_secret_key", "sk-lf-test"), \
             patch.object(telemetry, "Langfuse") as mock_cls:
            first = telemetry.get_langfuse_client()
            second = telemetry.get_langfuse_client()
            self.assertIs(first, second)
            mock_cls.assert_called_once()
            self.assertIsInstance(telemetry.get_langfuse_handler(), CallbackHandler)

    def test_trace_config_sets_langfuse_session_and_trace_name(self):
        """7. Every graph turn is grouped into a Langfuse session keyed by campaign id."""
        from app.telemetry import build_trace_config

        config = build_trace_config("CAMP-SESS01", "negotiation-turn", "handler",
                                    metadata={"sender_role": "buyer"}, tags=["negotiation-turn"])
        self.assertEqual(config["configurable"]["thread_id"], "CAMP-SESS01")
        self.assertEqual(config["callbacks"], ["handler"])
        self.assertEqual(config["metadata"]["langfuse_session_id"], "CAMP-SESS01")
        self.assertEqual(config["metadata"]["langfuse_trace_name"], "negotiation-turn")
        self.assertEqual(config["metadata"]["sender_role"], "buyer")
        self.assertEqual(config["tags"], ["commodity-arbitrage", "negotiation-turn"])
        self.assertEqual(build_trace_config("CAMP-X", "t")["callbacks"], [])


if __name__ == "__main__":
    unittest.main()
