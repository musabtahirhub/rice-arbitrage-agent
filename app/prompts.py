"""
Prompt templates for Gemini LLM.
LLM usage is strictly scoped to unstructured text extraction and polite trade correspondence.
All arithmetic and boundary logic is calculated beforehand by pure Python and passed as context.
"""

EMAIL_PARSER_PROMPT = """\
You are an expert commodity trade assistant.
Analyze the following email correspondence and extract commercial trade terms into a JSON object matching this schema:

{
  "sender_role": "buyer" or "supplier",
  "commodity": "<commodity name, e.g. Basmati 1121>",
  "quantity_mt": <number in metric tons>,
  "price_usd_per_mt": <numeric unit price in USD per MT>,
  "incoterm": "FOB" or "CIF",
  "port": "<port name, e.g. Jebel Ali, Mundra, Karachi>",
  "payment_terms": "<e.g. 100% LC at sight, CAD>"
}

Rules:
- Output valid JSON only without markdown fences or additional commentary.
- If incoterm is unspecified: assume 'CIF' for buyers and 'FOB' for suppliers.
- If quantity is unspecified: default to 500.0 MT.

Email to parse:
\"\"\"
{raw_email}
\"\"\"
"""

BUYER_COUNTER_PROMPT = """\
You are a senior physical commodity trader representing an international grain desk.
Draft a professional, concise counter-offer email to an institutional rice buyer.

Context:
- Campaign: {commodity}
- Buyer Name / Port: {buyer_port}
- Buyer's Current Offer: USD {buyer_offered_cif:.2f}/MT CIF (or requested quote)
- Desk's Dynamic CIF Floor: USD {cif_floor:.2f}/MT CIF
- Viable Deal: {is_viable}
- Note from Risk Desk: {evaluation_reason}

Instructions:
- If the deal is viable (is_viable=True), express confirmation, accept the terms, and request their company details for the formal Soft Corporate Offer (SCO).
- If the deal is below our floor (is_viable=False), politely explain market dynamics and counter-offer firmly at USD {cif_floor:.2f}/MT CIF {buyer_port}.
- Keep the tone polite, firm, and commercial. Include sign-off from "Trading Desk, Global Agro Arbitrage".
"""

SUPPLIER_RFQ_PROMPT = """\
You are a procurement specialist representing an international grain desk.
Draft a professional RFQ or counter-bid email to an agricultural rice mill / exporter.

Context:
- Commodity: {commodity}
- Target Volume: {target_volume_mt} MT
- Supplier Port: {origin_port}
- Supplier's Current Quote: USD {supplier_offered_fob:.2f}/MT FOB (if provided)
- Our Ceiling Acquisition Price: USD {fob_ceiling:.2f}/MT FOB
- Viable Deal: {is_viable}
- Note from Risk Desk: {evaluation_reason}

Instructions:
- If the deal is viable (is_viable=True), confirm agreement at the quoted FOB price, lock the volume allocation, and request the Proforma Invoice (PI) and banking coordinates.
- If the supplier's price exceeds our ceiling, counter firmly at our target FOB price of USD {fob_ceiling:.2f}/MT FOB {origin_port}, citing current port benchmark levels.
- Keep the email concise and commercial. Sign off from "Procurement Desk, Global Agro Arbitrage".
"""
