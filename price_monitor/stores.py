"""Clientes para Éxito y Jumbo Colombia (ambas tiendas sobre VTEX)."""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Optional
from urllib.parse import quote

import requests

from units import Quantity, parse_quantity, price_per_reference, quantity_from_vtex

log = logging.getLogger(__name__)

STORES = {
    "exito": {"name": "Éxito", "base": "https://www.exito.com"},
    "jumbo": {"name": "Jumbo", "base": "https://www.jumbocolombia.com"},
}

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"),
    "Accept": "application/json, text/html;q=0.9",
    "Accept-Language": "es-CO,es;q=0.9",
}


class StoreError(RuntimeError):
    pass


class StoreBlocked(StoreError):
    """La tienda rechaza consultas automáticas (p. ej. página de Cloudflare)."""


@dataclass
class ProductSnapshot:
    store: str
    sku: str
    product_id: str
    ean: str
    name: str
    brand: str
    url: str
    price: Optional[float]          # menor precio ofrecido (incluye tarjeta/promoción)
    list_price: Optional[float]     # precio "antes" (tachado)
    available: bool
    regular_price: Optional[float] = None   # precio sin tarjeta ni condiciones
    price_label: str = ""                   # condición del menor precio, p. ej. "con tarjeta -20%"
    unit_qty: Optional[float] = None
    unit: Optional[str] = None
    price_per_unit: Optional[float] = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @property
    def quantity(self) -> Optional[Quantity]:
        return Quantity(self.unit_qty, self.unit) if self.unit_qty and self.unit else None

    @property
    def presentation(self) -> str:
        q = self.quantity
        return q.label() if q else "?"

    def to_dict(self) -> dict:
        return asdict(self)


def _to_float(v: Any) -> Optional[float]:
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _lower_keys(obj: Any) -> Any:
    """Normaliza las claves de los teasers (catalog_system usa 'Name', intelligent-search 'name')."""
    if isinstance(obj, dict):
        return {str(k).lower(): _lower_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_lower_keys(v) for v in obj]
    return obj


def best_offer_price(co: dict) -> tuple[float, str]:
    """Menor precio de una oferta VTEX: precio normal, spotPrice o promociones
    (p. ej. descuento con tarjeta: PromotionTeasers con RestrictionsBins)."""
    regular = _to_float(co.get("Price"))
    best, label = regular, ""
    spot = _to_float(co.get("spotPrice"))
    if spot and spot < best:
        best, label = spot, "precio contado"
    teasers = co.get("PromotionTeasers") or co.get("teasers") or []
    for t in _lower_keys(teasers):
        cond = t.get("conditions") or {}
        if (cond.get("minimumquantity") or 0) > 1:
            continue  # exige llevar varias unidades
        bins = any(p.get("name") == "RestrictionsBins" for p in cond.get("parameters") or [])
        for eff in (t.get("effects") or {}).get("parameters") or []:
            try:
                val = float(str(eff.get("value")).replace(",", "."))
            except ValueError:
                continue
            if eff.get("name") == "PercentualDiscount" and 0 < val < 100:
                cand, tag = regular * (1 - val / 100), f"-{val:g}%"
            elif eff.get("name") == "NominalDiscount" and 0 < val < regular:
                cand, tag = regular - val, f"-${val:,.0f}".replace(",", ".")
            else:
                continue
            cand = round(cand, 0)
            if cand < best:
                best, label = cand, f"con tarjeta {tag}" if bins else f"promoción {tag}"
    return best, label


def parse_vtex_product(store: str, product: dict, base_url: str,
                       sku: str | None = None) -> list[ProductSnapshot]:
    """Convierte un producto VTEX (catalog_system o intelligent-search) en snapshots.

    Devuelve un snapshot por SKU (item); si se indica `sku` solo ese.
    """
    out = []
    name_p = product.get("productName") or ""
    brand = product.get("brand") or ""
    link = product.get("link") or ""
    if link and link.startswith("/"):
        link = base_url + link
    if not link and product.get("linkText"):
        link = f"{base_url}/{product['linkText']}/p"
    for item in product.get("items") or []:
        item_id = str(item.get("itemId") or "")
        if sku and item_id != str(sku):
            continue
        # productName es el nombre visible; con varios SKU se usa el del item
        if name_p and len(product.get("items") or []) == 1:
            name = name_p
        else:
            name = item.get("name") or item.get("nameComplete") or name_p
        best = None  # (precio, etiqueta, regular, list_price, disponible)
        for seller in item.get("sellers") or []:
            co = seller.get("commertialOffer") or seller.get("commercialOffer") or {}
            regular = _to_float(co.get("Price"))
            if regular is None:
                continue
            avail = bool(co.get("IsAvailable", (co.get("AvailableQuantity") or 0) > 0))
            low, label = best_offer_price(co)
            lp = _to_float(co.get("ListPrice")) or _to_float(co.get("PriceWithoutDiscount"))
            cand = (low, label, regular, lp, avail)
            # prefiere vendedores con stock; entre ellos, el menor precio
            if best is None or (avail, -low) > (best[4], -best[0]):
                best = cand
        price, label, regular, list_price, available = best or (None, "", None, None, False)
        qty = quantity_from_vtex(item.get("measurementUnit"), _to_float(item.get("unitMultiplier")), name)
        out.append(ProductSnapshot(
            store=store, sku=item_id, product_id=str(product.get("productId") or ""),
            ean=str(item.get("ean") or "").strip(), name=name.strip(), brand=brand.strip(),
            url=link, price=price, list_price=list_price, available=available,
            regular_price=regular, price_label=label,
            unit_qty=qty.amount if qty else None, unit=qty.unit if qty else None,
            price_per_unit=price_per_reference(price, qty),
        ))
    return out


def parse_jsonld(store: str, html: str, url: str, sku: str = "") -> Optional[ProductSnapshot]:
    """Plan B: lee el bloque schema.org/Product del HTML de la página."""
    for raw in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError:
            continue
        nodes = data if isinstance(data, list) else data.get("@graph", [data])
        for node in nodes:
            if not isinstance(node, dict) or node.get("@type") != "Product":
                continue
            offers = node.get("offers") or {}
            if isinstance(offers, dict) and "offers" in offers:
                offers = offers["offers"]
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            price = _to_float(offers.get("price") or offers.get("lowPrice"))
            name = node.get("name") or ""
            brand = node.get("brand") or ""
            if isinstance(brand, dict):
                brand = brand.get("name", "")
            qty = parse_quantity(name)
            return ProductSnapshot(
                store=store, sku=str(node.get("sku") or sku), product_id="",
                ean=str(node.get("gtin13") or node.get("gtin") or ""), name=name, brand=brand,
                url=url, price=price, list_price=None,
                available="InStock" in str(offers.get("availability", "InStock")),
                unit_qty=qty.amount if qty else None, unit=qty.unit if qty else None,
                price_per_unit=price_per_reference(price, qty))
    return None


def _is_bot_wall(r: requests.Response) -> bool:
    body = r.text[:4000].lower()
    return ("cloudflare" in body or "cf-ray" in {k.lower() for k in r.headers}
            or "captcha" in body or "access denied" in body)


class VtexStore:
    def __init__(self, key: str, sales_channel: str | int | None = None,
                 delay: float = 1.5, timeout: float = 20, retries: int = 3,
                 session: requests.Session | None = None, base_url: str | None = None):
        if key not in STORES:
            raise ValueError(f"Tienda desconocida: {key}")
        self.key = key
        self.display = STORES[key]["name"]
        self.base = (base_url or STORES[key]["base"]).rstrip("/")
        self.blocked: str | None = None
        self.sc = sales_channel
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.http = session or requests.Session()
        self.http.headers.update(HEADERS)
        self._last = 0.0

    # ---- HTTP -----------------------------------------------------------------
    def _get(self, url: str, params: dict | None = None) -> requests.Response:
        if self.blocked:
            raise StoreBlocked(self.blocked)
        params = dict(params or {})
        if self.sc not in (None, ""):
            params.setdefault("sc", self.sc)
        last_exc: Exception | None = None
        for attempt in range(self.retries):
            wait = self.delay - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                r = self.http.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(2 ** attempt)
                continue
            self._last = time.monotonic()
            if r.status_code in (403, 503) and _is_bot_wall(r):
                self.blocked = (f"{self.display} rechazó la consulta (HTTP {r.status_code}, "
                                "protección anti-bots). Ver README: 'Si una tienda bloquea'.")
                raise StoreBlocked(self.blocked)
            if r.status_code in (429, 500, 502, 503, 504):
                last_exc = StoreError(f"HTTP {r.status_code}")
                time.sleep(2 ** attempt)
                continue
            # VTEX responde 206 en búsquedas paginadas
            if r.status_code not in (200, 206):
                raise StoreError(f"{self.display}: HTTP {r.status_code} en {r.url}")
            return r
        raise StoreError(f"{self.display}: no se pudo consultar {url}: {last_exc}")

    def _json(self, url: str, params: dict | None = None) -> Any:
        r = self._get(url, params)
        try:
            return r.json()
        except ValueError as exc:
            raise StoreError(f"{self.display}: respuesta no es JSON ({url})") from exc

    # ---- API ------------------------------------------------------------------
    def search(self, text: str, n: int = 20) -> list[ProductSnapshot]:
        """Búsqueda por texto libre. Prueba catalog_system y luego intelligent-search."""
        results: list[ProductSnapshot] = []
        try:
            # VTEX exige espacios como %20 en ft (con '+' responde HTTP 400)
            data = self._json(f"{self.base}/api/catalog_system/pub/products/search"
                              f"?ft={quote(text.lower())}",
                              {"_from": 0, "_to": max(0, min(n, 50) - 1)})
            for p in data or []:
                results.extend(parse_vtex_product(self.key, p, self.base))
        except StoreBlocked:
            raise
        except StoreError as exc:
            log.warning("%s catalog_system falló (%s); probando intelligent-search", self.display, exc)
        if not results:
            try:
                data = self._json(
                    f"{self.base}/api/io/_v/api/intelligent-search/product_search/",
                    {"query": text, "count": n, "page": 1, "locale": "es-CO"})
                for p in (data or {}).get("products", []):
                    results.extend(parse_vtex_product(self.key, p, self.base))
            except StoreBlocked:
                raise
            except StoreError as exc:
                log.warning("%s intelligent-search falló: %s", self.display, exc)
        return results

    def fetch_by_sku(self, sku: str, url: str = "") -> Optional[ProductSnapshot]:
        """Consulta estable de un SKU ya emparejado; plan B: JSON-LD de la página."""
        try:
            data = self._json(f"{self.base}/api/catalog_system/pub/products/search",
                              {"fq": f"skuId:{sku}"})
            for p in data or []:
                snaps = parse_vtex_product(self.key, p, self.base, sku=sku)
                if snaps:
                    return snaps[0]
        except StoreBlocked:
            raise
        except StoreError as exc:
            log.warning("%s: consulta por SKU %s falló: %s", self.display, sku, exc)
        if url:
            try:
                return parse_jsonld(self.key, self._get(url).text, url, sku)
            except StoreError as exc:
                log.warning("%s: página %s falló: %s", self.display, url, exc)
        return None


def search_url(store: str, text: str) -> str:
    return f"{STORES[store]['base']}/s?q={quote(text)}"
