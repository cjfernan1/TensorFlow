"""Búsqueda por nombre libre y emparejamiento del mismo producto en ambas tiendas."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from rapidfuzz import fuzz

from stores import ProductSnapshot
from units import Quantity, parse_quantity, remove_quantity, strip_accents

STOPWORDS = {"de", "del", "la", "el", "los", "las", "x", "en", "y", "con", "para", "por",
             "und", "un", "unid", "unidad", "unidades", "pack", "paquete", "a", "al"}
# Variantes de escritura frecuentes -> forma canónica
SYNONYMS = {
    "mozarella": "mozzarella", "mozarela": "mozzarella", "mozzarela": "mozzarella",
    "cocacola": "coca cola", "yogurt": "yogur", "yoghurt": "yogur",
}

# Palabras de empaque que no distinguen un producto de otro
GENERIC = {"botella", "pet", "bolsa", "caja", "lata", "tetrapack", "tetra", "pak", "frasco",
           "vidrio", "sobre", "doypack", "paquete", "c", "u", "cu", "ml", "g", "gr", "grs", "kg",
           "l", "lt", "litro", "litros", "unidades", "precio", "especial", "oferta", "nuevo",
           "nueva", "presentacion", "familiar", "personal", "plastica", "pote", "vaso", "tarro"}
EXTRA_PENALTY = 6    # puntos por palabra distintiva que no está en tu texto (máx. 18)

HIGH_SCORE = 85      # puntaje mínimo para elegir sin preguntar
MARGIN = 6           # ventaja mínima sobre la segunda opción
MIN_SCORE = 45       # por debajo se descarta el candidato
QTY_TOLERANCE = 0.05


def normalize(text: str) -> list[str]:
    t = strip_accents((text or "").lower())
    t = re.sub(r"[^a-z0-9ñ ]+", " ", t)
    tokens = []
    for tok in t.split():
        tok = SYNONYMS.get(tok, tok)
        tokens.extend(tok.split())
    return [tok for tok in tokens if tok not in STOPWORDS]


def _covered(tok: str, cand_tokens: list[str]) -> bool:
    return any(c == tok or (len(tok) > 3 and fuzz.ratio(tok, c) >= 80) for c in cand_tokens)


@dataclass
class Intent:
    raw: str
    keywords: list[str]
    quantity: Optional[Quantity]

    @classmethod
    def from_text(cls, text: str) -> "Intent":
        return cls(text, normalize(remove_quantity(text)), parse_quantity(text))


@dataclass
class Scored:
    snap: ProductSnapshot
    score: float
    notes: list[str] = field(default_factory=list)


def score_candidate(intent: Intent, snap: ProductSnapshot) -> Optional[Scored]:
    """Puntaje 0-100; None si se descarta (sin precio o presentación distinta)."""
    if snap.price is None:
        return None
    notes = []
    cand_tokens = normalize(remove_quantity(snap.name) + " " + snap.brand)
    if not intent.keywords:
        coverage = 0.0
    else:
        coverage = sum(_covered(t, cand_tokens) for t in intent.keywords) / len(intent.keywords)
    tsr = fuzz.token_set_ratio(" ".join(intent.keywords), " ".join(cand_tokens)) / 100
    name_pts = 60 * (0.6 * coverage + 0.4 * tsr)
    # palabras distintivas del candidato que no pediste (p. ej. "go", "griego", "fresa")
    brand_set = set(normalize(snap.brand))
    extra = {t for t in normalize(remove_quantity(snap.name))
             if t not in GENERIC and t not in brand_set and not any(ch.isdigit() for ch in t)
             and not _covered(t, intent.keywords)}
    name_pts -= min(3, len(extra)) * EXTRA_PENALTY

    brand_tokens = normalize(snap.brand)
    if brand_tokens and all(_covered(b, intent.keywords) for b in brand_tokens):
        brand_pts = 20
    elif not brand_tokens:
        brand_pts = 10
    else:
        brand_pts = 0

    if intent.quantity:
        if snap.quantity and snap.quantity.close_to(intent.quantity, QTY_TOLERANCE):
            qty_pts = 20
        else:
            return None
    else:
        qty_pts = 10  # neutral: el usuario no indicó tamaño
    score = name_pts + brand_pts + qty_pts
    if not snap.available:
        score -= 10
        notes.append("agotado")
    return Scored(snap, round(score, 1), notes)


def rank(intent: Intent, snaps: list[ProductSnapshot]) -> list[Scored]:
    seen, out = set(), []
    for s in snaps:
        if s.sku in seen:
            continue
        seen.add(s.sku)
        sc = score_candidate(intent, s)
        if sc and sc.score >= MIN_SCORE:
            out.append(sc)
    return sorted(out, key=lambda x: x.score, reverse=True)


@dataclass
class MatchResult:
    intent: Intent
    chosen: dict[str, Optional[Scored]]      # tienda -> elección (o None)
    candidates: dict[str, list[Scored]]      # tienda -> ranking completo
    confident: bool
    method: str                              # ean | marca+presentacion | mejor-puntaje | ninguno
    notes: list[str] = field(default_factory=list)


def _clear_winner(ranked: list[Scored], chosen: Scored, sized: bool = True) -> bool:
    """Elección clara: buen puntaje y ventaja sobre la siguiente opción.

    Si no indicaste tamaño (`sized=False`) y hay otra opción cercana con otra
    presentación, se considera dudosa para que elijas tú.
    """
    if chosen.score < HIGH_SCORE:
        return False
    others = [r for r in ranked if r.snap.sku != chosen.snap.sku]
    if not others:
        return True
    if not sized:
        for o in others:
            if chosen.score - o.score >= 15:
                break
            if not (o.snap.quantity and chosen.snap.quantity
                    and o.snap.quantity.close_to(chosen.snap.quantity)):
                return False
    return chosen.score - others[0].score >= MARGIN


def _pair_bonus(a: ProductSnapshot, b: ProductSnapshot) -> float:
    bonus = 0.0
    if a.brand and b.brand and normalize(a.brand) == normalize(b.brand):
        bonus += 10
    if a.quantity and b.quantity and a.quantity.close_to(b.quantity, QTY_TOLERANCE):
        bonus += 10
    bonus += fuzz.token_set_ratio(" ".join(normalize(a.name)), " ".join(normalize(b.name))) / 10
    return bonus


def match(intent: Intent, results: dict[str, list[ProductSnapshot]]) -> MatchResult:
    ranked = {store: rank(intent, snaps) for store, snaps in results.items()}
    sized = intent.quantity is not None
    stores = list(ranked)
    chosen: dict[str, Optional[Scored]] = {s: None for s in stores}
    notes: list[str] = []

    found = [s for s in stores if ranked[s]]
    if not found:
        return MatchResult(intent, chosen, ranked, False, "ninguno", ["no se encontró en ninguna tienda"])

    if len(found) == 1:
        s = found[0]
        chosen[s] = ranked[s][0]
        notes.append(f"solo se encontró en {s}")
        return MatchResult(intent, chosen, ranked, _clear_winner(ranked[s], chosen[s], sized),
                           "mejor-puntaje", notes)

    a, b = found[0], found[1]
    # 1) mismo EAN en ambas tiendas
    best_ean = None
    for ra in ranked[a]:
        if not ra.snap.ean:
            continue
        for rb in ranked[b]:
            if rb.snap.ean == ra.snap.ean:
                total = ra.score + rb.score
                if best_ean is None or total > best_ean[0]:
                    best_ean = (total, ra, rb)
    if best_ean:
        _, ra, rb = best_ean
        chosen[a], chosen[b] = ra, rb
        top = max(ranked[a][0].score, ranked[b][0].score)
        # Seguro si el par por EAN está entre lo mejor puntuado en su tienda
        confident = (ra.score >= HIGH_SCORE - 10 and rb.score >= HIGH_SCORE - 10
                     and (ra.score + rb.score) / 2 >= top - MARGIN
                     and (_clear_winner(ranked[a], ra, sized) or _clear_winner(ranked[b], rb, sized)
                          or intent.quantity is not None))
        return MatchResult(intent, chosen, ranked, confident, "ean", notes)

    # 2) mejor par combinando puntajes + similitud entre ambos productos
    best_pair = None
    for ra in ranked[a][:5]:
        for rb in ranked[b][:5]:
            total = ra.score + rb.score + _pair_bonus(ra.snap, rb.snap)
            if best_pair is None or total > best_pair[0]:
                best_pair = (total, ra, rb)
    _, ra, rb = best_pair
    chosen[a], chosen[b] = ra, rb
    same_qty = bool(ra.snap.quantity and rb.snap.quantity
                    and ra.snap.quantity.close_to(rb.snap.quantity, QTY_TOLERANCE))
    same_brand = bool(ra.snap.brand and normalize(ra.snap.brand) == normalize(rb.snap.brand))
    if not same_qty:
        notes.append("presentaciones distintas: se compara por precio por unidad")
    method = "marca+presentacion" if same_brand and same_qty else "mejor-puntaje"
    confident = (_clear_winner(ranked[a], ra, sized) and _clear_winner(ranked[b], rb, sized)
                 and same_brand and same_qty)
    return MatchResult(intent, chosen, ranked, confident, method, notes)


def search_with_fallback(store, intent: Intent, n: int = 20) -> list[ProductSnapshot]:
    """Busca el texto; si no hay resultados, reintenta sin presentación y acortando."""
    queries = [intent.raw]
    no_qty = " ".join(normalize(remove_quantity(intent.raw)))
    if no_qty and no_qty != intent.raw.lower():
        queries.append(no_qty)
    words = no_qty.split()
    while len(words) > 2:
        words = words[:-1]
        queries.append(" ".join(words))
    for q in dict.fromkeys(queries):
        res = store.search(q, n)
        if res:
            return res
    return []
