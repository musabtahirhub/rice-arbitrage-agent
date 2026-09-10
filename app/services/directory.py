"""
Marketplace counterparty directory registry and discovery helpers.
"""

from __future__ import annotations

from app.models.counterparty import Counterparty

# ---------------------------------------------------------------------------
# Seed Data: Middle East Buyers
# ---------------------------------------------------------------------------

BUYERS_DIRECTORY: list[Counterparty] = [
    Counterparty(
        id="BUYER-GULF-01",
        name="Gulf Food Trading LLC",
        role="buyer",
        country="United Arab Emirates",
        port="Jebel Ali",
        preferred_commodities=[
            "Basmati 1121 Sella Rice",
            "Basmati Super Kernel",
            "Jasmine Rice",
        ],
        contact_name="Tariq Mansoor, Head of Procurement",
        contact_email="procurement@gulffoodtrading.ae",
        typical_volume_mt=500.0,
        reputation_score=4.9,
        payment_terms="100% Irrevocable LC at Sight",
    ),
    Counterparty(
        id="BUYER-BARAKAH-02",
        name="Al-Barakah Foods Co.",
        role="buyer",
        country="Saudi Arabia",
        port="Dammam",
        preferred_commodities=[
            "Basmati 1121 Sella Rice",
            "Thai White Rice 5% Broken",
            "Vietnam White Rice 5% Broken",
        ],
        contact_name="Sheikh Fahad Al-Otaibi, Supply Director",
        contact_email="purchasing@albarakahfoods.sa",
        typical_volume_mt=1000.0,
        reputation_score=4.7,
        payment_terms="Irrevocable Confirmed LC at Sight",
    ),
    Counterparty(
        id="BUYER-EMIRATES-03",
        name="Emirates Grain Importers",
        role="buyer",
        country="United Arab Emirates",
        port="Jebel Ali",
        preferred_commodities=[
            "Basmati 1121 Sella Rice",
            "Thai White Rice 5% Broken",
            "Jasmine Rice",
            "Vietnam White Rice 5% Broken",
        ],
        contact_name="Rashid Al-Nuaimi, Chief Trader",
        contact_email="trade@emiratesgrain.ae",
        typical_volume_mt=750.0,
        reputation_score=4.8,
        payment_terms="LC at Sight or CAD against BL copy",
    ),
    Counterparty(
        id="BUYER-RIYADH-04",
        name="Riyadh Commodity Hub",
        role="buyer",
        country="Saudi Arabia",
        port="Dammam",
        preferred_commodities=[
            "Basmati 1121 Sella Rice",
            "Basmati Traditional Raw",
        ],
        contact_name="Khaled Al-Ghamdi, Import Specialist",
        contact_email="imports@riyadhcommodity.sa",
        typical_volume_mt=500.0,
        reputation_score=4.6,
        payment_terms="LC at Sight",
    ),
]

# ---------------------------------------------------------------------------
# Seed Data: South & Southeast Asian Suppliers / Mills
# ---------------------------------------------------------------------------

SUPPLIERS_DIRECTORY: list[Counterparty] = [
    Counterparty(
        id="SUPP-INDUS-01",
        name="Indus Rice Mills Ltd.",
        role="supplier",
        country="Pakistan",
        port="Karachi",
        preferred_commodities=[
            "Basmati 1121 Sella Rice",
            "Basmati Super Kernel",
            "IRRI-6 Long Grain Rice",
        ],
        contact_name="Zubair Qureshi, Export Director",
        contact_email="export@indusricemills.pk",
        typical_volume_mt=1000.0,
        reputation_score=4.9,
        payment_terms="FOB Karachi, LC at Sight or 20% advance + 80% CAD",
    ),
    Counterparty(
        id="SUPP-THAI-02",
        name="Thai Grain Export Corp",
        role="supplier",
        country="Thailand",
        port="Bangkok",
        preferred_commodities=[
            "Thai White Rice 5% Broken",
            "Thai Hom Mali Jasmine Rice",
            "Pathumthani Fragrant Rice",
        ],
        contact_name="Somchai Prasert, Senior Export Manager",
        contact_email="sales@thaigraincorp.th",
        typical_volume_mt=1500.0,
        reputation_score=4.8,
        payment_terms="FOB Bangkok, 100% LC at Sight",
    ),
    Counterparty(
        id="SUPP-MEKONG-03",
        name="Mekong Delta Agro Processing",
        role="supplier",
        country="Vietnam",
        port="Ho Chi Minh",
        preferred_commodities=[
            "Vietnam White Rice 5% Broken",
            "Jasmine Rice",
            "DT8 Fragrant Rice",
        ],
        contact_name="Nguyen Van Hai, International Sales",
        contact_email="contact@mekongdeltaagro.vn",
        typical_volume_mt=800.0,
        reputation_score=4.7,
        payment_terms="FOB Ho Chi Minh, LC at Sight",
    ),
    Counterparty(
        id="SUPP-PUNJAB-04",
        name="Punjab Golden Grains Exporters",
        role="supplier",
        country="India",
        port="Mundra",
        preferred_commodities=[
            "Basmati 1121 Sella Rice",
            "Basmati 1509 Golden Sella",
            "Sharbati Steam Rice",
        ],
        contact_name="Harpreet Singh, Managing Partner",
        contact_email="exports@punjabgoldengrains.in",
        typical_volume_mt=1200.0,
        reputation_score=4.8,
        payment_terms="FOB Mundra, 100% Confirmed LC at Sight",
    ),
]


def _match_commodity(search_term: str, preferred_list: list[str]) -> bool:
    term = search_term.lower()
    tokens = [t for t in term.replace("%", "").replace(",", "").split() if len(t) > 2 and t not in {"rice", "broken", "sella"}]
    for pref in preferred_list:
        p_lower = pref.lower()
        if p_lower in term or term in p_lower:
            return True
        if any(t in p_lower for t in tokens):
            return True
    return "rice" in term


def get_buyers_for_commodity(commodity: str) -> list[Counterparty]:
    matches = [b for b in BUYERS_DIRECTORY if _match_commodity(commodity, b.preferred_commodities)]
    return matches if matches else list(BUYERS_DIRECTORY)


def get_suppliers_for_commodity(commodity: str) -> list[Counterparty]:
    matches = [s for s in SUPPLIERS_DIRECTORY if _match_commodity(commodity, s.preferred_commodities)]
    return matches if matches else list(SUPPLIERS_DIRECTORY)


def get_all_counterparties() -> dict[str, list[dict]]:
    return {
        "buyers": [b.model_dump() for b in BUYERS_DIRECTORY],
        "suppliers": [s.model_dump() for s in SUPPLIERS_DIRECTORY],
    }


def get_counterparty_by_id(counterparty_id: str) -> Counterparty | None:
    for c in BUYERS_DIRECTORY + SUPPLIERS_DIRECTORY:
        if c.id == counterparty_id:
            return c
    return None
