"""
Integration test for Enterprise API endpoints:
- Directory discovery
- Campaign ignition
- Inbound Email Webhook
- Operator Deal Authorization Gateway
"""
import asyncio
from httpx import AsyncClient, ASGITransport
from app.main import app

async def run_tests():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        print("--- 1. Testing Directory Endpoints ---")
        res = await client.get("/api/directory")
        assert res.status_code == 200, f"Failed GET /api/directory: {res.text}"
        data = res.json()
        print(f"Buyers: {len(data['buyers'])}, Suppliers: {len(data['suppliers'])}")
        assert len(data['buyers']) >= 3
        assert len(data['suppliers']) >= 3

        print("\n--- 2. Testing Campaign Creation ---")
        campaign_payload = {
            "commodity_variety": "Basmati 1121",
            "target_volume_mt": 500.0,
            "target_margin_pct": 10.0,
            "max_variance_from_benchmark_pct": 8.0,
            "broken_pct": 2.0,
            "destination_port": "Jebel Ali",
            "payment_terms": "LC"
        }
        res = await client.post("/api/campaigns", json=campaign_payload)
        assert res.status_code == 200, f"Failed POST /api/campaigns: {res.text}"
        campaign_data = res.json()
        campaign_id = campaign_data["campaign_id"]
        print(f"Campaign created: ID={campaign_id}, buyers={campaign_data['discovered_buyers_count']}, suppliers={campaign_data['discovered_suppliers_count']}")
        assert campaign_data["discovered_buyers_count"] >= 1
        assert campaign_data["discovered_suppliers_count"] >= 1

        print("\n--- 3. Testing Inbound Email Webhook ---")
        # Simulate inbound email from buyer
        webhook_payload = {
            "sender": "procurement@gulffood.ae",
            "recipient": "trading@arbitrage-desk.com",
            "subject": f"Offer for Basmati 1121 [Ref: {campaign_id} | Thread: thread-buyer-101]",
            "body": "We confirm interest in Basmati 1121. Quantity: 500 MT, Price: USD 1150/MT CIF Jebel Ali, LC terms.",
            "provider": "resend"
        }
        res = await client.post("/api/webhooks/inbound-email", json=webhook_payload)
        assert res.status_code == 200, f"Failed webhook: {res.text}"
        webhook_res = res.json()
        print(f"Webhook (buyer) response: status={webhook_res['status']}, campaign_id={webhook_res['campaign_id']}, round={webhook_res['negotiation_round']}")
        assert webhook_res["campaign_id"] == campaign_id
        assert webhook_res["status"] == "processed"

        # Simulate inbound email from supplier with competitive FOB price
        supplier_webhook_payload = {
            "sender": "export@indusrice.pk",
            "recipient": "trading@arbitrage-desk.com",
            "subject": f"FOB Quote for Basmati 1121 [Ref: {campaign_id} | Thread: thread-supplier-102]",
            "body": "We quote FOB Karachi. Quantity: 500 MT, Price: USD 900/MT, payment by 100% LC.",
            "provider": "resend"
        }
        res_supp = await client.post("/api/webhooks/inbound-email", json=supplier_webhook_payload)
        assert res_supp.status_code == 200
        supp_res = res_supp.json()
        print(f"Webhook (supplier) response: viable={supp_res.get('is_deal_viable')}, deal_status={supp_res.get('deal_status')}, margin={supp_res.get('net_margin_pct')}%")

        print("\n--- 4. Testing Deal Authorization Gateway ---")
        # Check pending deals
        res = await client.get("/api/deals/pending")
        assert res.status_code == 200
        pending = res.json()["pending_deals"]
        print(f"Pending deals awaiting operator: {len(pending)}")

        # Authorize a mock deal
        res = await client.post(f"/api/deals/{campaign_id}/authorize?operator_name=ChiefTrader&notes=ApprovedForExecution")
        assert res.status_code == 200
        auth_data = res.json()
        print(f"Deal authorization response: {auth_data}")
        assert auth_data["campaign_id"] == campaign_id
        assert auth_data["status"] == "authorized"

        # Reject a deal
        res = await client.post(f"/api/deals/{campaign_id}/reject?operator_name=ChiefTrader&reason=MarginVariance")
        assert res.status_code == 200
        rej_data = res.json()
        print(f"Deal rejection response: {rej_data}")
        assert rej_data["status"] == "rejected"
        assert rej_data["campaign_id"] == campaign_id

        print("\n[SUCCESS] All Enterprise API endpoints tested and verified!")

if __name__ == "__main__":
    asyncio.run(run_tests())
