"""Only Gmail Primary-inbox mail reaches the graph, on both ingestion paths (push and IMAP polling)."""
import asyncio
import unittest
from unittest.mock import MagicMock, patch

from app.config import settings
from app.services import imap, inbound_push
from app.services.gmail_inbox import PRIMARY_INBOX_QUERY, fetch_latest_messages_by_history, is_primary_inbox_message
from app.services.gmail_processed_cache import PROCESSED_GMAIL_MESSAGE_IDS
from app.utils.gmail_payload import parse_gmail_message

PRIMARY = ["INBOX", "CATEGORY_PERSONAL", "UNREAD"]


def _push_message(msg_id: str, label_ids: list[str]) -> dict:
    return {
        "id": msg_id,
        "threadId": f"thread-{msg_id}",
        "labelIds": label_ids,
        "Message-ID": f"<{msg_id}@buyer.example>",
        "Subject": "Re: Soft Corporate Offer (SCO) — Basmati 1121",
        "From": "Buyer <procurement@buyer.example>",
        "body": "We bid USD 1,000/MT CIF Jebel Ali.",
    }


class TestPrimaryInboxLabels(unittest.TestCase):
    def test_primary_inbox_messages_pass(self):
        self.assertTrue(is_primary_inbox_message(PRIMARY))
        self.assertTrue(is_primary_inbox_message(["INBOX"]))  # inbox tabs disabled: no category label at all

    def test_non_primary_messages_are_rejected(self):
        for labels in (
            ["INBOX", "CATEGORY_PROMOTIONS"],
            ["INBOX", "CATEGORY_SOCIAL"],
            ["INBOX", "CATEGORY_UPDATES"],
            ["INBOX", "CATEGORY_FORUMS"],
            ["SPAM"],
            ["INBOX", "SPAM"],
            ["TRASH", "CATEGORY_PERSONAL"],
            ["SENT"],
            [],
            None,
        ):
            with self.subTest(labels=labels):
                self.assertFalse(is_primary_inbox_message(labels))

    def test_query_targets_primary_and_excludes_spam_and_trash(self):
        self.assertEqual(PRIMARY_INBOX_QUERY, "label:INBOX category:primary -label:SPAM -label:TRASH")

    def test_parsed_gmail_message_keeps_label_ids(self):
        parsed = parse_gmail_message({
            "id": "g-1", "threadId": "t-1", "labelIds": PRIMARY, "snippet": "short preview",
            "payload": {"mimeType": "multipart/alternative", "headers": [{"name": "From", "value": "a@b.c"}]},
        })
        self.assertEqual(parsed["labelIds"], PRIMARY)
        self.assertEqual(parsed["body"], "short preview")  # no decodable part: falls back to the message snippet


class TestPushPathFiltering(unittest.TestCase):
    def setUp(self):
        self._cursor = inbound_push.LAST_SEEN_HISTORY_ID

    def tearDown(self):
        inbound_push.LAST_SEEN_HISTORY_ID = self._cursor

    def test_history_listing_is_limited_to_inbox(self):
        PROCESSED_GMAIL_MESSAGE_IDS.pop("g-hist", None)
        service = MagicMock()
        service.users().history().list.return_value.execute.return_value = {
            "history": [{"messagesAdded": [{"message": {"id": "g-hist"}}]}]
        }
        service.users().messages().get.return_value.execute.return_value = {"id": "g-hist", "labelIds": PRIMARY}
        with patch("app.services.gmail_inbox.get_gmail_service", return_value=service):
            messages = fetch_latest_messages_by_history("500")
        self.assertEqual(service.users().history().list.call_args.kwargs["labelId"], "INBOX")
        self.assertEqual(messages[0]["labelIds"], PRIMARY)

    def test_non_primary_message_is_skipped_before_the_graph(self):
        for labels in (["INBOX", "CATEGORY_PROMOTIONS"], ["SPAM"], ["INBOX", "TRASH"]):
            with self.subTest(labels=labels), \
                 patch("app.services.inbound_push.find_open_campaign") as find, \
                 patch("app.services.inbound_push.process_inbound_message_transaction") as process, \
                 self.assertLogs("arbitrage_desk", level="DEBUG") as logs:
                inbound_push.handle_push_message(_push_message("g-promo", labels), {"desk@arbitrage.com"})
            find.assert_not_called()
            process.assert_not_called()
            self.assertIn("Skipping non-primary inbox message: g-promo", "\n".join(logs.output))

    def test_primary_message_reaches_the_transaction(self):
        db_camp = MagicMock(id="CAMP-PRIMARY")
        with patch("app.services.inbound_push.find_open_campaign", return_value=db_camp), \
             patch("app.services.inbound_push.process_inbound_message_transaction") as process, \
             patch("app.db.SessionLocal"):
            inbound_push.handle_push_message(_push_message("g-primary", PRIMARY), {"desk@arbitrage.com"})
        process.assert_called_once()
        inbound = process.call_args.args[1]
        self.assertEqual((inbound.message_id, inbound.thread_id), ("<g-primary@buyer.example>", "thread-g-primary"))

    def test_skipped_message_is_acknowledged_so_it_is_not_refetched(self):
        inbound_push.LAST_SEEN_HISTORY_ID = "100"
        batch = [_push_message("g-promo", ["INBOX", "CATEGORY_PROMOTIONS"]), _push_message("g-primary", PRIMARY)]
        with patch("app.services.inbound_push.fetch_latest_messages_by_history", return_value=batch), \
             patch("app.services.inbound_push.find_open_campaign", return_value=MagicMock(id="CAMP-PRIMARY")), \
             patch("app.services.inbound_push.process_inbound_message_transaction") as process, \
             patch("app.services.inbound_push.mark_gmail_message_processed") as mark, \
             patch("app.db.SessionLocal"):
            asyncio.run(inbound_push.process_incoming_gmail_event("101"))
        self.assertEqual([c.args[0] for c in mark.call_args_list], ["g-promo", "g-primary"])
        self.assertEqual(process.call_count, 1)
        self.assertEqual(process.call_args.args[1].message_id, "<g-primary@buyer.example>")


class TestImapPathFiltering(unittest.TestCase):
    def test_gmail_imap_search_is_restricted_to_primary(self):
        with patch.object(settings, "imap_server", "imap.gmail.com"):
            criteria = imap._unseen_search_criteria()
        self.assertEqual(criteria, ("UNSEEN", "X-GM-RAW", f'"{PRIMARY_INBOX_QUERY}"'))

    def test_other_imap_servers_use_plain_unseen_search(self):
        with patch.object(settings, "imap_server", "mail.example.com"):
            self.assertEqual(imap._unseen_search_criteria(), ("UNSEEN",))

    def test_unseen_search_sends_primary_criteria_to_the_server(self):
        mail = MagicMock()
        mail.search.return_value = ("OK", [b"3 7"])
        with patch.object(settings, "imap_server", "imap.gmail.com"):
            ids = imap._search_unseen_ids(mail)
        mail.search.assert_called_once_with(None, "UNSEEN", "X-GM-RAW", f'"{PRIMARY_INBOX_QUERY}"')
        self.assertEqual(ids, ["3", "7"])


if __name__ == "__main__":
    unittest.main()
