"""
Realistic raw-email fixtures for testing the arbitrage workflow.

These simulate actual negotiation emails a rice trading desk would
receive.  They are consumed by the LLM extraction node (parse_email)
and by the deterministic unit tests (test_runner.py).
"""

# ---------------------------------------------------------------------------
# Fixture 1 — Viable buyer CIF inquiry (Middle East importer)
# ---------------------------------------------------------------------------

VIABLE_BUYER_EMAIL = """\
Subject: Inquiry — Basmati 1121 Sella Rice 5% Broken — CIF Jebel Ali

Dear Sir / Madam,

We are Al Rashed Trading LLC, based in Dubai, UAE, and we are looking
to purchase Basmati 1121 Sella Rice with a maximum 5% broken ratio,
2025 crop year.

Our requirement is as follows:
  - Commodity : Basmati 1121 Sella Rice, 5% broken max
  - Quantity  : 500 MT (five hundred metric tons)
  - Price     : USD 1,150 per MT, CIF Jebel Ali
  - Packing   : 25 kg PP bags, palletised
  - Shipment  : Within 30 days of LC opening

Please confirm availability and send us your Soft Corporate Offer at
your earliest convenience.  Payment will be via Irrevocable Letter of
Credit at Sight, confirmed by Emirates NBD.

Best regards,
Mohammed Al Rashed
Procurement Manager
Al Rashed Trading LLC
Dubai, UAE
"""

# ---------------------------------------------------------------------------
# Fixture 2 — Matching supplier FOB quote (Indian exporter)
# ---------------------------------------------------------------------------

MATCHING_SUPPLIER_EMAIL = """\
Subject: Re: Quotation — Basmati 1121 Sella Rice 5% Broken — FOB Mundra

Dear Buyer,

Thank you for your inquiry.  We are pleased to offer the following:

  - Commodity : Basmati 1121 Sella Rice, 5% broken, 2025 crop
  - Quantity  : 500 MT
  - Price     : USD 920 per MT, FOB Mundra Port, Gujarat
  - Packing   : 25 kg PP bags on pallets
  - Shipment  : 21–28 days from order confirmation
  - Payment   : Irrevocable LC at Sight

This offer is valid for 7 days from the date of this email.  Kindly
confirm so we may prepare the Proforma Invoice and banking details.

Warm regards,
Rajesh Gupta
Export Manager
Gupta Agri Exports Pvt. Ltd.
Karnal, Haryana, India
"""

# ---------------------------------------------------------------------------
# Fixture 3 — Low-ball buyer inquiry (breaches hard CIF floor)
# ---------------------------------------------------------------------------

LOWBALL_BUYER_EMAIL = """\
Subject: Price Inquiry — Basmati 1121 Sella Rice — CIF Karachi

Hello,

We are interested in buying Basmati 1121 Sella Rice, 5% broken,
for our distribution network in Pakistan.

Details:
  - Quantity : 500 MT
  - Target price : USD 900 per MT, CIF Karachi
  - Packing  : 50 kg PP bags

Please send your best offer.

Regards,
Imran Malik
Chief Buyer
Pak Rice Distributors
Karachi, Pakistan
"""
