"""Inbound email parsing and intent capture: quote stripping, the regex fallback, Gemini JSON handling,
the deterministic intent reconcile pass, and intent routing through the graph. Runs offline."""
import unittest
from contextlib import ExitStack

from langgraph.checkpoint.memory import MemorySaver

from app.config import settings
from app.graph.graph import build_checkpoint_serde, build_trade_graph
from app.models import Campaign, ParsedEmail
from app.services.llm_service import (
    _parse_terms_json,
    extract_email_terms,
    extract_terms_with_regex,
    reconcile_intent,
)
from app.services.mail.email_utils import strip_quoted_reply
from app.services.mail.gmail_client import html_to_text
from tests.test_regressions import _offline_patches

DESK_CIF = 1100.0


def _regex(text: str, role: str = "buyer", last: float = DESK_CIF) -> ParsedEmail:
    return reconcile_intent(extract_terms_with_regex(text, role, last, "Basmati 1121", 500.0), last)


def _terms(role: str, price: float, intent: str = "COUNTER_OFFER", accepted: bool = False) -> ParsedEmail:
    return ParsedEmail(sender_role=role, price_usd_per_mt=price, intent=intent, is_acceptance=accepted)


class TestQuoteStripping(unittest.TestCase):
    def test_gmail_attribution_wrapped_over_two_lines(self):
        body = (
            "We can do 1050.\n\n"
            "On Fri, Oct 17, 2026 at 9:31 AM Trading Desk <\n"
            "desk@example.com> wrote:\n"
            "Our offer is USD 1196.76/MT CIF Jebel Ali."
        )
        self.assertEqual(strip_quoted_reply(body), "We can do 1050.")

    def test_single_line_attribution_and_quote_markers(self):
        body = "Accepted.\n> USD 1196.76/MT\nOn Fri, Oct 17, 2026 Trading Desk wrote:\n> more"
        self.assertEqual(strip_quoted_reply(body), "Accepted.")

    def test_outlook_header_block(self):
        body = "Not interested, thanks.\n\nFrom: Trading Desk <desk@example.com>\nSent: Friday\nSubject: Offer\nUSD 999"
        self.assertEqual(strip_quoted_reply(body), "Not interested, thanks.")

    def test_outlook_separator(self):
        body = "Fine by us.\n________________________________\nFrom: Desk\nUSD 1196.76"
        self.assertEqual(strip_quoted_reply(body), "Fine by us.")

    def test_from_line_without_header_block_is_kept(self):
        body = "From: our side we accept.\nRegards"
        self.assertEqual(strip_quoted_reply(body), body)

    def test_html_body_drops_gmail_quote(self):
        html = '<div>We accept.</div><div class="gmail_quote">On Fri ... wrote:<blockquote>USD 1196</blockquote></div>'
        text = html_to_text(html)
        self.assertIn("We accept.", text)
        self.assertNotIn("1196", text)

    def test_quoted_price_does_not_leak_into_regex_price(self):
        body = "We can only do 1050 per tonne.\n\nOn Fri, Oct 17, 2026 Desk wrote:\n> USD 1196.76/MT"
        parsed = _regex(strip_quoted_reply(body))
        self.assertNotEqual(parsed.price_usd_per_mt, 1196.76)


class TestRegexIntent(unittest.TestCase):
    def test_decline_is_rejection(self):
        for text in ("Sorry, we are not interested at this time.", "We regretfully decline.",
                     "We cannot supply this volume.", "We will pass on this one.",
                     "Unable to proceed, thank you."):
            with self.subTest(text=text):
                parsed = _regex(text, role="supplier", last=1000.0)
                self.assertEqual(parsed.intent, "REJECTION")
                self.assertFalse(parsed.is_acceptance)
                self.assertEqual(parsed.price_usd_per_mt, 0.0)

    def test_bare_acceptance_takes_desk_rate_for_that_side(self):
        self.assertEqual(_regex("Accepted.", "buyer", 1196.76).price_usd_per_mt, 1196.76)
        self.assertEqual(_regex("Accepted.", "supplier", 1011.76).price_usd_per_mt, 1011.76)

    def test_standalone_acceptance_words(self):
        for text in ("Confirmed.", "OK, go ahead", "Deal done", "Ok proceed."):
            with self.subTest(text=text):
                self.assertEqual(_regex(text).intent, "ACCEPTANCE")

    def test_negated_acceptance_is_not_acceptance(self):
        self.assertNotEqual(_regex("We cannot accept USD 1100/MT.").intent, "ACCEPTANCE")

    def test_accept_at_different_price_is_counter(self):
        parsed = _regex("We accept at USD 1050/MT.")
        self.assertEqual(parsed.intent, "COUNTER_OFFER")
        self.assertFalse(parsed.is_acceptance)
        self.assertEqual(parsed.price_usd_per_mt, 1050.0)

    def test_implausible_price_is_dropped(self):
        parsed = _regex("Can you reduce by $10?")
        self.assertEqual(parsed.intent, "INQUIRY")
        self.assertEqual(parsed.price_usd_per_mt, 0.0)

    def test_question_without_price_is_inquiry(self):
        parsed = _regex("What is the earliest shipment date?")
        self.assertEqual(parsed.intent, "INQUIRY")
        self.assertEqual(parsed.price_usd_per_mt, 0.0)

    def test_quantity_decimals_and_units(self):
        self.assertEqual(_regex("USD 1050/MT for 500.5 MT").quantity_mt, 500.5)
        self.assertEqual(_regex("USD 1050/MT for 1,000 tonnes").quantity_mt, 1000.0)
        self.assertEqual(_regex("USD 1050/MT for 750 MTs").quantity_mt, 750.0)


class TestGeminiJsonParsing(unittest.TestCase):
    def test_nulls_and_prose_around_fenced_json(self):
        text = 'Here you go:\n```json\n{"intent": "rejection", "price_usd_per_mt": null, "quantity_mt": null}\n```'
        parsed = _parse_terms_json(text, "supplier", 500.0)
        self.assertEqual(parsed.intent, "REJECTION")
        self.assertEqual(parsed.price_usd_per_mt, 0.0)
        self.assertEqual(parsed.quantity_mt, 500.0)
        self.assertEqual(parsed.sender_role, "supplier")

    def test_numeric_strings(self):
        parsed = _parse_terms_json('{"intent": "COUNTER_OFFER", "price_usd_per_mt": "USD 1,050.50"}', "buyer", 500.0)
        self.assertEqual(parsed.price_usd_per_mt, 1050.5)

    def test_unknown_intent_is_inferred(self):
        parsed = _parse_terms_json('{"intent": "maybe", "price_usd_per_mt": 0}', "buyer", 500.0)
        self.assertEqual(parsed.intent, "INQUIRY")

    def test_non_object_raises(self):
        with self.assertRaises(Exception):
            _parse_terms_json("no json here", "buyer", 500.0)


class TestReconcileIntent(unittest.TestCase):
    def test_flags_agree_with_intent(self):
        parsed = reconcile_intent(_terms("buyer", 1100.0, "COUNTER_OFFER", accepted=True), DESK_CIF)
        self.assertEqual(parsed.intent, "ACCEPTANCE")
        self.assertTrue(parsed.is_acceptance)

    def test_acceptance_within_tolerance_keeps_acceptance(self):
        self.assertEqual(reconcile_intent(_terms("buyer", 1100.6, "ACCEPTANCE"), DESK_CIF).intent, "ACCEPTANCE")

    def test_rejection_never_carries_price(self):
        parsed = reconcile_intent(_terms("buyer", 1100.0, "REJECTION", accepted=True), DESK_CIF)
        self.assertEqual((parsed.intent, parsed.is_acceptance, parsed.price_usd_per_mt), ("REJECTION", False, 0.0))

    def test_extract_email_terms_reconciles_without_llm(self):
        with ExitStack() as stack:
            _offline_patches(stack)
            parsed = extract_email_terms("Accepted", "supplier", {"last_proposed_price": 1011.76})
        self.assertEqual((parsed.intent, parsed.price_usd_per_mt), ("ACCEPTANCE", 1011.76))


class TestIntentRouting(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        _offline_patches(self.stack)
        self.graph = build_trade_graph(checkpointer=MemorySaver(serde=build_checkpoint_serde()))
        self.config = {"configurable": {"thread_id": "CAMP-PARSE"}}
        campaign = Campaign(campaign_id="CAMP-PARSE", commodity="Basmati 1121", target_volume_mt=500.0)
        self.graph.update_state(self.config, {
            "campaign": campaign, "anchor_cif_usd": DESK_CIF, "deal_status": "prospecting", "negotiation_round": 0,
        })

    def tearDown(self):
        self.stack.close()

    def _turn(self, role: str, text: str) -> dict:
        return self.graph.invoke({"latest_email": text, "active_role": role}, config=self.config)

    def test_buyer_question_is_answered_without_round_or_terms(self):
        result = self._turn("buyer", "What is the earliest shipment date?")
        answer = result.get("buyer_draft")
        self.assertTrue(answer)
        self.assertIn(settings.default_shipment_window, answer)
        self.assertIn(f"USD {DESK_CIF:.2f}/MT CIF", answer)  # standing offer restated, unchanged
        self.assertFalse(result.get("supplier_draft"))
        self.assertEqual(result["deal_status"], "prospecting")
        self.assertIsNone(result.get("buyer_terms"))
        self.assertEqual(result.get("negotiation_round"), 0)

    def test_question_after_bid_keeps_terms_round_and_status(self):
        self._turn("buyer", "We accept your offer.")
        before = self.graph.get_state(self.config).values
        result = self._turn("buyer", "Is LC at sight acceptable to you?")
        self.assertIn(settings.default_payment_terms, result["buyer_draft"])
        self.assertEqual(result["deal_status"], before["deal_status"])
        self.assertEqual(result["buyer_terms"].price_usd_per_mt, DESK_CIF)
        self.assertEqual(result["negotiation_round"], before["negotiation_round"])

    def test_supplier_question_never_sees_buyer_cif(self):
        first = self._turn("buyer", "We accept your offer.")
        result = self._turn("supplier", "What packing do you need?")
        answer = result["supplier_draft"]
        self.assertIn(settings.default_packing_spec, answer)
        self.assertIn(f"USD {first['target_fob_ceiling']:.2f}/MT FOB", answer)
        self.assertNotIn(f"{DESK_CIF:.2f}", answer)
        self.assertFalse(result.get("buyer_draft"))

    def test_buyer_question_sends_no_rfq(self):
        result = self._turn("buyer", "Which mill is this from?")
        self.assertFalse(result.get("target_fob_ceiling"))

    def test_supplier_decline_closes_deal_and_tells_buyer(self):
        self._turn("buyer", "We accept your offer.")
        result = self._turn("supplier", "Sorry, we cannot supply this volume.")
        self.assertEqual(result["deal_status"], "rejected")
        self.assertTrue(result.get("supplier_draft"))
        self.assertTrue(result.get("buyer_draft"))

    def test_buyer_decline_before_rfq_only_answers_buyer(self):
        result = self._turn("buyer", "Not interested, thank you.")
        self.assertEqual(result["deal_status"], "rejected")
        self.assertTrue(result.get("buyer_draft"))
        self.assertFalse(result.get("supplier_draft"))

    def test_buyer_accept_then_supplier_accept_reaches_approval(self):
        first = self._turn("buyer", "We accept your offer, please proceed.")
        self.assertIn("Acceptance Noted", first["buyer_draft"])
        result = self._turn("supplier", "Accepted. We confirm allocation.")
        self.assertEqual(result["supplier_terms"].price_usd_per_mt, first["target_fob_ceiling"])
        self.assertEqual(self.graph.get_state(self.config).next, ("approval_gate",))


if __name__ == "__main__":
    unittest.main()


class TestCampaignSubjects(unittest.TestCase):
    def test_subject_tagging_is_idempotent(self):
        from app.services.mail.email_utils import rfq_subject, tag_campaign_subject
        self.assertEqual(tag_campaign_subject("OFFER: 500 MT", "CAMP-1"), "OFFER: 500 MT [CAMP-1]")
        self.assertEqual(tag_campaign_subject("OFFER [CAMP-1]", "CAMP-1"), "OFFER [CAMP-1]")
        self.assertEqual(rfq_subject("CAMP-1", "Basmati 1121", "Karachi"), "RFQ — Basmati 1121 FOB Karachi [CAMP-1]")

    def test_with_draft_subject_replaces_only_subject(self):
        from app.services.mail.email_utils import with_draft_subject
        self.assertEqual(with_draft_subject("Subject: Old\n\nBody text", "New"), "Subject: New\n\nBody text")

    def test_first_rfq_sets_campaign_supplier_subject(self):
        routing = TestIntentRouting("test_buyer_decline_before_rfq_only_answers_buyer")
        routing.setUp()
        try:
            result = routing._turn("buyer", "We accept your offer.")
        finally:
            routing.tearDown()
        self.assertEqual(result["supplier_thread_subject"], "RFQ — Basmati 1121 FOB Karachi [CAMP-PARSE]")
