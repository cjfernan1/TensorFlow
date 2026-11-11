"""Parseo de presentaciones (500 g, 1 L, 6 x 250 ml...) y precio por unidad."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

# sinónimo -> (unidad base, factor)
_UNIT_ALIASES = {
    "kg": ("g", 1000), "kgs": ("g", 1000), "kilo": ("g", 1000), "kilos": ("g", 1000),
    "kilogramo": ("g", 1000), "kilogramos": ("g", 1000),
    "g": ("g", 1), "gr": ("g", 1), "grs": ("g", 1), "gramo": ("g", 1), "gramos": ("g", 1),
    "mg": ("g", 0.001), "lb": ("g", 500), "libra": ("g", 500), "libras": ("g", 500),
    "l": ("ml", 1000), "lt": ("ml", 1000), "lts": ("ml", 1000), "ltr": ("ml", 1000),
    "litro": ("ml", 1000), "litros": ("ml", 1000),
    "ml": ("ml", 1), "mls": ("ml", 1), "cc": ("ml", 1), "cm3": ("ml", 1),
    "mililitro": ("ml", 1), "mililitros": ("ml", 1),
    "und": ("und", 1), "un": ("und", 1), "unds": ("und", 1), "unid": ("und", 1),
    "unidad": ("und", 1), "unidades": ("und", 1), "u": ("und", 1),
}
_UNIT_RE = "|".join(sorted(map(re.escape, _UNIT_ALIASES), key=len, reverse=True))
_NUM = r"(\d+(?:[.,]\d+)?)"

# "3 x 400 g", "6x250ml", "pack 3 und x 400 g"
_PACK_RE = re.compile(
    rf"\b{_NUM}\s*(?:und|unid|unidades|un)?\s*x\s*{_NUM}\s*({_UNIT_RE})\b")
# "400 g x 3"
_PACK_REV_RE = re.compile(
    rf"\b{_NUM}\s*({_UNIT_RE})\s*x\s*{_NUM}\s*(?:und|unid|unidades|un)?\b")
_QTY_RE = re.compile(rf"\b{_NUM}\s*({_UNIT_RE})\b")
# primero medidas (g/ml); las unidades sueltas ("6und") solo si no hay medida
_MEASURE_RE = re.compile(rf"\b{_NUM}\s*({'|'.join(sorted((re.escape(k) for k, v in _UNIT_ALIASES.items() if v[0] != 'und'), key=len, reverse=True))})\b")
# "x 6 und" sin otra medida
_COUNT_RE = re.compile(r"\bx\s*(\d+)\s*(?:und|unid|unidades|un)?\b")

# Para mostrar el precio por unidad: (base, cantidad de referencia, etiqueta)
REFERENCE = {"g": (1000, "kg"), "ml": (1000, "L"), "und": (1, "und")}


@dataclass(frozen=True)
class Quantity:
    amount: float  # en unidad base
    unit: str      # g | ml | und

    def label(self) -> str:
        if self.unit in ("g", "ml") and self.amount >= 1000:
            big = "kg" if self.unit == "g" else "L"
            return f"{self.amount / 1000:g} {big}"
        return f"{self.amount:g} {self.unit}"

    def close_to(self, other: "Quantity", tol: float = 0.05) -> bool:
        if self.unit != other.unit or not other.amount:
            return False
        return abs(self.amount - other.amount) / other.amount <= tol


def strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _prep(text: str) -> str:
    t = strip_accents(text.lower()).replace("×", "x")
    # "x200ml", "X1.5L" -> "x 200ml": separa la "x" pegada al número
    return re.sub(r"\bx(?=\d)", "x ", t)


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def _norm(value: float, unit: str) -> Quantity:
    base, factor = _UNIT_ALIASES[unit]
    return Quantity(round(value * factor, 4), base)


def parse_quantity(text: str) -> Optional[Quantity]:
    """Extrae la presentación total de un texto libre o del nombre del producto."""
    if not text:
        return None
    t = _prep(text)
    m = _PACK_RE.search(t)
    if m:
        q = _norm(_num(m.group(2)), m.group(3))
        return Quantity(round(q.amount * _num(m.group(1)), 4), q.unit)
    m = _PACK_REV_RE.search(t)
    if m:
        q = _norm(_num(m.group(1)), m.group(2))
        return Quantity(round(q.amount * _num(m.group(3)), 4), q.unit)
    m = _MEASURE_RE.search(t) or _QTY_RE.search(t)
    if m:
        return _norm(_num(m.group(1)), m.group(2))
    m = _COUNT_RE.search(t)
    if m:
        return Quantity(float(m.group(1)), "und")
    return None


def remove_quantity(text: str) -> str:
    """Quita la presentación del texto (útil para reintentar búsquedas)."""
    t = _prep(text)
    for rx in (_PACK_RE, _PACK_REV_RE, _QTY_RE, _COUNT_RE):
        t = rx.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def quantity_from_vtex(measurement_unit: str | None, multiplier: float | None,
                       name: str) -> Optional[Quantity]:
    """VTEX trae measurementUnit/unitMultiplier; si es 'un' se usa el nombre."""
    from_name = parse_quantity(name)
    mu = (measurement_unit or "").strip().lower()
    if mu in _UNIT_ALIASES and _UNIT_ALIASES[mu][0] != "und" and multiplier:
        q = _norm(float(multiplier), mu)
        # unitMultiplier a veces viene en 1 por defecto; preferimos el nombre si difiere
        if from_name and from_name.unit == q.unit and float(multiplier) == 1:
            return from_name
        return q
    return from_name


def price_per_reference(price: float | None, qty: Optional[Quantity]) -> Optional[float]:
    """Precio por kg, por L o por unidad."""
    if price is None or qty is None or not qty.amount:
        return None
    ref, _ = REFERENCE[qty.unit]
    return round(price / qty.amount * ref, 2)


def reference_label(qty: Optional[Quantity]) -> str:
    return REFERENCE[qty.unit][1] if qty else ""
