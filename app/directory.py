from typing import Optional

from app.config import settings
from app.models import Counterparty

MIDDLE_EAST_COUNTRIES = ("UAE", "Saudi Arabia", "Qatar", "Kuwait", "Oman", "Bahrain")

BUYERS = [
    Counterparty(
        id="BUYER-001",
        name="Gulf Food Trading LLC",
        role="buyer",
        country="UAE",
        primary_port="Jebel Ali",
        preferred_commodities=["Basmati 1121", "Thai White 5%", "Jasmine Rice"],
        contact_email="procurement@gulffood.ae",
        reputation_score=4.9,
    ),
    Counterparty(
        id="BUYER-002",
        name="Al-Barakah Foods",
        role="buyer",
        country="Saudi Arabia",
        primary_port="Dammam",
        preferred_commodities=["Basmati 1121", "Super Kernel Basmati"],
        contact_email="imports@albarakah-foods.sa",
        reputation_score=4.7,
    ),
    Counterparty(
        id="BUYER-003",
        name="Emirates Grain Importers",
        role="buyer",
        country="UAE",
        primary_port="Jebel Ali",
        preferred_commodities=["Basmati 1121", "Thai White 5%", "Vietnam 5%"],
        contact_email="supply@emiratesgrain.ae",
        reputation_score=4.8,
    ),
]

SUPPLIERS = [
    Counterparty(
        id="SUPP-001",
        name="Indus Rice Mills",
        role="supplier",
        country="Pakistan",
        primary_port="Karachi",
        preferred_commodities=["Basmati 1121", "Super Kernel Basmati"],
        contact_email="export@indusrice.pk",
        reputation_score=4.8,
    ),
    Counterparty(
        id="SUPP-002",
        name="Thai Grain Corp",
        role="supplier",
        country="Thailand",
        primary_port="Bangkok",
        preferred_commodities=["Thai White 5%", "Jasmine Rice"],
        contact_email="sales@thaigrain.co.th",
        reputation_score=4.9,
    ),
    Counterparty(
        id="SUPP-003",
        name="Mekong Delta Agro Exporters",
        role="supplier",
        country="Vietnam",
        primary_port="Ho Chi Minh",
        preferred_commodities=["Vietnam 5%", "Jasmine Rice"],
        contact_email="trade@mekongdelta-agro.vn",
        reputation_score=4.6,
    ),
]


def get_all_counterparties() -> dict[str, list[Counterparty]]:
    return {"buyers": BUYERS, "suppliers": SUPPLIERS}


def get_buyers_for_commodity(commodity: str) -> list[Counterparty]:
    query = commodity.lower().strip()
    matches = [
        b for b in BUYERS
        if any(query in c.lower() or c.lower() in query for c in b.preferred_commodities)
    ]
    return matches or BUYERS


def get_suppliers_for_commodity(commodity: str) -> list[Counterparty]:
    query = commodity.lower().strip()
    matches = [
        s for s in SUPPLIERS
        if any(query in c.lower() or c.lower() in query for c in s.preferred_commodities)
    ]
    return matches or SUPPLIERS


def select_primary_buyer(commodity: str) -> Optional[Counterparty]:
    """The first Middle East buyer for the commodity, else the first buyer at all."""
    buyers = get_buyers_for_commodity(commodity)
    middle_east = [b for b in buyers if b.country in MIDDLE_EAST_COUNTRIES]
    return (middle_east or buyers or [None])[0]


def resolve_supplier_contact(
    commodity: str,
    state_email: Optional[str] = None,
    state_name: Optional[str] = None,
) -> tuple[str, str]:
    """(name, email) for supplier mail: the test inbox first (MY_TEST_SUPPLIER_EMAIL), then the address
    already on the deal, then the directory. Test mode wins so no directory address is ever mailed while testing."""
    suppliers = get_suppliers_for_commodity(commodity)
    primary = suppliers[0] if suppliers else None
    name = state_name or (primary.name if primary else "Supplier Partner")
    test_email = settings.my_test_supplier_email.strip()
    if test_email:
        return name, test_email
    if state_email:
        return name, state_email
    return name, primary.contact_email if primary else "export@supplier.com"

