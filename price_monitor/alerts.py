"""Reglas de alerta y notificaciones de escritorio (Windows)."""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Optional

from stores import STORES, ProductSnapshot
from storage import Storage
from units import reference_label

log = logging.getLogger(__name__)


@dataclass
class Alert:
    key: str
    title: str
    message: str
    url: str = ""
    kind: str = "info"   # bajada | diferencia | descuento | disponible | pendiente


def money(v: Optional[float]) -> str:
    return "—" if v is None else "$" + f"{v:,.0f}".replace(",", ".")


def price_text(s) -> str:
    """"$3.512 con tarjeta -20% (sin tarjeta $4.390)" o solo "$3.600"."""
    txt = money(s.price)
    if getattr(s, "price_label", "") and s.regular_price and s.regular_price != s.price:
        txt += f" {s.price_label} (normal {money(s.regular_price)})"
    return txt


def _pct(new: float, old: float) -> float:
    return (old - new) / old * 100 if old else 0.0


def check_price_drop(query: str, snap: ProductSnapshot, db: Storage,
                     threshold_pct: float, avg_days: int = 30) -> list[Alert]:
    """Evalúa la bajada ANTES de guardar el precio actual en el historial."""
    if snap.price is None:
        return []
    alerts = []
    store = STORES[snap.store]["name"]
    last = db.last_price(query, snap.store, snap.sku)
    st = db.stats(query, snap.store, snap.sku, avg_days)
    if last is None or last["price"] is None:
        return []
    refs = []
    drop_last = _pct(snap.price, last["price"])
    if drop_last >= threshold_pct:
        refs.append(f"-{drop_last:.0f}% vs última consulta ({money(last['price'])})")
    if st["avg"]:
        drop_avg = _pct(snap.price, st["avg"])
        if drop_avg >= threshold_pct and not refs:
            refs.append(f"-{drop_avg:.0f}% vs promedio {avg_days} días ({money(st['avg'])})")
    if refs:
        if st["min_all"] is not None and snap.price < st["min_all"]:
            refs.append("¡mínimo histórico!")
        alerts.append(Alert(
            key=f"bajada:{query}:{snap.store}:{snap.price:.0f}",
            title=f"⬇ {query} bajó en {store}",
            message=f"{snap.name} ({snap.presentation}): {price_text(snap)}. " + " · ".join(refs),
            url=snap.url, kind="bajada"))
    # regreso de disponibilidad
    if snap.available and last["available"] == 0:
        alerts.append(Alert(
            key=f"disponible:{query}:{snap.store}:{snap.timestamp[:10]}",
            title=f"✅ {query} volvió a {store}",
            message=f"{snap.name}: {money(snap.price)}", url=snap.url, kind="disponible"))
    return alerts


def check_discount(query: str, snap: ProductSnapshot, min_pct: float) -> list[Alert]:
    if not snap.price or not snap.list_price or snap.list_price <= snap.price:
        return []
    pct = _pct(snap.price, snap.list_price)
    if pct < min_pct:
        return []
    return [Alert(
        key=f"descuento:{query}:{snap.store}:{snap.price:.0f}",
        title=f"🏷 {query}: {pct:.0f}% de descuento en {STORES[snap.store]['name']}",
        message=f"{snap.name}: {price_text(snap)} (antes {money(snap.list_price)})",
        url=snap.url, kind="descuento")]


def check_cross_store(query: str, snaps: dict[str, ProductSnapshot],
                      threshold_pct: float) -> list[Alert]:
    """Compara el precio por unidad (kg/L/und) entre tiendas."""
    valid = [s for s in snaps.values() if s and s.available and s.price_per_unit]
    if len(valid) < 2:
        valid = [s for s in snaps.values() if s and s.available and s.price]
        if len(valid) < 2:
            return []
        # sin presentación conocida: solo comparable si son el mismo EAN
        if not (valid[0].ean and valid[0].ean == valid[1].ean):
            return []
        cheap, exp = sorted(valid, key=lambda s: s.price)[:2]
        a, b, per = cheap.price, exp.price, ""
    else:
        if valid[0].unit != valid[1].unit:
            return []
        cheap, exp = sorted(valid, key=lambda s: s.price_per_unit)[:2]
        a, b = cheap.price_per_unit, exp.price_per_unit
        per = "/" + reference_label(cheap.quantity)
    diff = _pct(a, b)
    if diff < threshold_pct:
        return []
    same_pres = cheap.quantity and exp.quantity and cheap.quantity.close_to(exp.quantity)
    detail = (f"{STORES[cheap.store]['name']}: {price_text(cheap)} ({cheap.presentation}, {money(cheap.price_per_unit)}{per})"
              f" vs {STORES[exp.store]['name']}: {price_text(exp)} ({exp.presentation}, {money(exp.price_per_unit)}{per})")
    if not same_pres:
        detail += " · presentaciones distintas"
    return [Alert(
        key=f"diferencia:{query}:{cheap.store}:{round(diff / 5) * 5}",
        title=f"⚖ {query}: {diff:.0f}% más barato en {STORES[cheap.store]['name']}",
        message=detail, url=cheap.url, kind="diferencia")]


class Notifier:
    def __init__(self, enabled: bool = True, app_id: str = "Monitor de precios"):
        self.enabled = enabled
        self.app_id = app_id
        self._backend = None
        if enabled:
            self._backend = self._detect()

    @staticmethod
    def _detect():
        if sys.platform.startswith("win"):
            try:
                import winotify  # noqa: F401
                return "winotify"
            except ImportError:
                pass
        try:
            from plyer import notification  # noqa: F401
            return "plyer"
        except ImportError:
            return None

    def send(self, alert: Alert):
        log.info("ALERTA %s | %s | %s", alert.title, alert.message, alert.url)
        print(f"\n🔔 {alert.title}\n   {alert.message}\n   {alert.url}")
        if not self.enabled or not self._backend:
            return
        try:
            if self._backend == "winotify":
                from winotify import Notification, audio
                toast = Notification(app_id=self.app_id, title=alert.title,
                                     msg=alert.message[:250], duration="long",
                                     launch=alert.url or None)
                if alert.url:
                    toast.add_actions(label="Ver producto", launch=alert.url)
                toast.set_audio(audio.Default, loop=False)
                toast.show()
            else:
                from plyer import notification
                notification.notify(title=alert.title[:64], message=alert.message[:250],
                                    app_name=self.app_id, timeout=15)
        except Exception as exc:  # una notificación fallida no debe detener el monitor
            log.warning("No se pudo mostrar la notificación: %s", exc)


def dispatch(alerts: list[Alert], db: Storage, notifier: Notifier, cooldown_hours: float,
             dry_run: bool = False) -> int:
    sent = 0
    for a in alerts:
        if db.alert_recent(a.key, cooldown_hours):
            log.debug("Alerta omitida por enfriamiento: %s", a.key)
            continue
        if dry_run:
            print(f"\n[dry-run] 🔔 {a.title}\n   {a.message}\n   {a.url}")
        else:
            notifier.send(a)
            db.mark_alert(a.key, f"{a.title} | {a.message}")
        sent += 1
    return sent
