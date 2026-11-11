import pytest

from stores import parse_jsonld, parse_vtex_product
from units import Quantity, parse_quantity, price_per_reference, remove_quantity
from helpers import vtex_product


@pytest.mark.parametrize("text,amount,unit", [
    ("Pony Malta 1 litro", 1000, "ml"),
    ("Leche Alquería 1100 ml", 1100, "ml"),
    ("Arroz Diana 500g", 500, "g"),
    ("Café Sello Rojo 2,5 kg", 2500, "g"),
    ("Queso Mozzarella Alpina x 400 gr", 400, "g"),
    ("Gaseosa Coca-Cola pack 6 x 400 ml", 2400, "ml"),
    ("Atún 3 und x 160 g", 480, "g"),
    ("Cerveza 330 ml x 6", 1980, "ml"),
    ("Huevos AA x 30 und", 30, "und"),
    ("Aceite 1 L", 1000, "ml"),
    ("Pony malta 1.5 lt", 1500, "ml"),
    ("Malta Pony Malta Botella Pet X200ml", 200, "ml"),
    ("Malta Pony Malta Botella Pet X1.5L", 1500, "ml"),
    ("Malta Pony Malta Botella Pet X1L", 1000, "ml"),
    ("Malta Pony Malta botella pet x30und x200ml c-u", 6000, "ml"),
    ("Malta Pony Malta Botella Pet X6 Unidades X330ml", 1980, "ml"),
    ("Malta PONY MALTA go x6und (1200 ml)", 1200, "ml"),
])
def test_parse_quantity(text, amount, unit):
    assert parse_quantity(text) == Quantity(amount, unit)


def test_no_quantity():
    assert parse_quantity("queso mozarella alpina") is None


def test_remove_quantity():
    assert remove_quantity("Pony Malta 1 litro") == "pony malta"


def test_price_per_reference():
    assert price_per_reference(5000, Quantity(500, "g")) == 10000  # por kg
    assert price_per_reference(3000, Quantity(1500, "ml")) == 2000  # por L
    assert price_per_reference(18000, Quantity(30, "und")) == 600


def test_parse_vtex_product():
    p = vtex_product("1", "123", "Queso Mozzarella Alpina 400 g", "Alpina", 17900, 19900,
                     ean="7702001", mu="g", mult=400)
    [s] = parse_vtex_product("exito", p, "https://www.exito.com")
    assert s.sku == "123" and s.ean == "7702001" and s.brand == "Alpina"
    assert s.price == 17900 and s.list_price == 19900 and s.available
    assert s.quantity == Quantity(400, "g") and s.price_per_unit == 44750
    assert s.url.startswith("https://www.exito.com/")


def test_vtex_unit_multiplier_default_uses_name():
    p = vtex_product("1", "9", "Pony Malta 1 L", "Pony", 3500, mu="un", mult=1)
    [s] = parse_vtex_product("jumbo", p, "https://www.jumbocolombia.com")
    assert s.quantity == Quantity(1000, "ml")


def test_unavailable():
    p = vtex_product("1", "9", "Pony Malta 1 L", "Pony", 0, available=False)
    [s] = parse_vtex_product("jumbo", p, "https://x")
    assert s.price is None and not s.available


def test_jsonld_fallback():
    html = """<html><script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Product","name":"Pony Malta 1 L","sku":"55",
     "gtin13":"7702004","brand":{"@type":"Brand","name":"Pony"},
     "offers":{"@type":"Offer","price":"3490","availability":"https://schema.org/InStock"}}
    </script></html>"""
    s = parse_jsonld("exito", html, "https://www.exito.com/pony/p")
    assert s.price == 3490 and s.ean == "7702004" and s.brand == "Pony"
    assert s.price_per_unit == 3490


def test_real_jumbo_card_price():
    """Respuesta real de jumbocolombia.com (recortada): 'Paga $3.512' con tarjeta."""
    import json
    from pathlib import Path
    data = json.loads((Path(__file__).parent / "fixtures" / "jumbo_pony_catalog.json")
                      .read_text(encoding="utf-8"))
    snaps = {s.name: s for p in data
             for s in parse_vtex_product("jumbo", p, "https://www.jumbocolombia.com")}
    s = snaps["Malta Pony Malta Botella Pet X1L"]
    assert s.regular_price == 4390 and s.price == 3512
    assert s.price_label == "con tarjeta -20%"
    assert s.quantity == Quantity(1000, "ml") and s.price_per_unit == 3512
    assert snaps["Malta Pony Malta Botella Pet X1.5L"].price == 5080


def test_real_jumbo_match_prefers_plain_over_go():
    import json
    from pathlib import Path
    from matcher import Intent, match
    data = json.loads((Path(__file__).parent / "fixtures" / "jumbo_pony_catalog.json")
                      .read_text(encoding="utf-8"))
    snaps = [s for p in data for s in parse_vtex_product("jumbo", p, "https://www.jumbocolombia.com")]
    m = match(Intent.from_text("pony malta 1 litro"), {"jumbo": snaps})
    assert m.confident and m.chosen["jumbo"].snap.name == "Malta Pony Malta Botella Pet X1L"


def test_best_offer_price_variants():
    from stores import best_offer_price
    # formato intelligent-search, compra mínima de 2 se ignora, descuento nominal
    co = {"Price": 10000, "spotPrice": 9800, "teasers": [
        {"name": "2x", "conditions": {"minimumQuantity": 2, "parameters": []},
         "effects": {"parameters": [{"name": "PercentualDiscount", "value": "50"}]}},
        {"name": "promo", "conditions": {"minimumQuantity": 0, "parameters": []},
         "effects": {"parameters": [{"name": "NominalDiscount", "value": "1500"}]}}]}
    assert best_offer_price(co) == (8500, "promoción -$1.500")
    assert best_offer_price({"Price": 5000}) == (5000, "")
