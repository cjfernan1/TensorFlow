"""Productos VTEX de ejemplo y tiendas falsas para pruebas offline."""
from stores import STORES, parse_vtex_product


def vtex_product(pid, sku, name, brand, price, list_price=None, ean="", available=True,
                 mu="un", mult=1.0, link_text=None):
    return {
        "productId": pid, "productName": name, "brand": brand,
        "linkText": link_text or name.lower().replace(" ", "-"),
        "link": f"/{link_text or name.lower().replace(' ', '-')}/p",
        "items": [{
            "itemId": sku, "name": name, "nameComplete": name, "ean": ean,
            "measurementUnit": mu, "unitMultiplier": mult,
            "sellers": [{"sellerId": "1", "commertialOffer": {
                "Price": price, "ListPrice": list_price or price,
                "PriceWithoutDiscount": list_price or price,
                "AvailableQuantity": 10 if available else 0, "IsAvailable": available}}],
        }],
    }


class FakeStore:
    def __init__(self, key, products):
        self.key = key
        self.base = STORES[key]["base"]
        self.products = products
        self.queries = []

    def snaps(self):
        out = []
        for p in self.products:
            out += parse_vtex_product(self.key, p, self.base)
        return out

    def search(self, text, n=20):
        self.queries.append(text)
        words = [w for w in text.lower().split() if len(w) > 2]
        return [s for s in self.snaps()
                if any(w[:5] in (s.name + " " + s.brand).lower() for w in words)][:n]

    def fetch_by_sku(self, sku, url=""):
        for s in self.snaps():
            if s.sku == sku:
                return s
        return None
