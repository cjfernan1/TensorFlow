#!/usr/bin/env python3
"""Monitor de precios Éxito / Jumbo.

Uso:
    python monitor.py match [--reelegir "texto"]   # empareja productos de la hoja
    python monitor.py run [--dry-run]               # consulta precios y envía alertas
    python monitor.py report                        # tabla comparativa en consola
"""
from __future__ import annotations

import argparse
import csv
import io
import logging
import sys
from pathlib import Path
from typing import Optional

import requests
import yaml

import alerts as al
from matcher import Intent, MatchResult, Scored, match, search_with_fallback
from storage import Storage
from stores import STORES, ProductSnapshot, VtexStore
from units import Quantity, price_per_reference, reference_label, strip_accents

HERE = Path(__file__).resolve().parent
ACTIVE = ("auto", "confirmado")

DEFAULTS = {
    "sheet_csv_url": "",
    "sheet_local_csv": "productos.csv",
    "db_path": "precios.db",
    "log_path": "monitor.log",
    "stores": {"exito": {"enabled": True, "sales_channel": ""},
               "jumbo": {"enabled": True, "sales_channel": ""}},
    "request_delay_seconds": 1.5,
    "search_results": 20,
    "thresholds": {"drop_pct": 10, "cross_store_pct": 15, "discount_pct": 25,
                   "average_days": 30, "cooldown_hours": 24},
    "notifications": {"enabled": True, "discounts": False},
}

log = logging.getLogger("monitor")


# ---------------------------------------------------------------------------
# Configuración y hoja
# ---------------------------------------------------------------------------
def _merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in (extra or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(path: Path) -> dict:
    cfg = DEFAULTS
    if path.exists():
        cfg = _merge(DEFAULTS, yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    return cfg


def _resolve(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else HERE / path


def to_csv_url(url: str) -> str:
    """Acepta el enlace normal de la hoja y lo convierte a exportación CSV."""
    if "/export" in url or "output=csv" in url or "format=csv" in url:
        return url
    if "docs.google.com/spreadsheets/d/" in url:
        sheet_id = url.split("/d/")[1].split("/")[0]
        gid = "0"
        if "gid=" in url:
            gid = url.split("gid=")[1].split("&")[0].split("#")[0]
        return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    return url


NAME_KEYS = ("producto", "nombre", "product", "articulo", "item", "descripcion")
OFF_VALUES = ("no", "n", "false", "0", "falso", "x", "inactivo")


def _key(h: str) -> str:
    return strip_accents((h or "").strip().lower())


def _rows_of(text: str) -> list[list[str]]:
    text = text.lstrip("﻿")
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    # Excel en español guarda CSV con ';'
    delim = max([",", ";", "\t"], key=first.count) if first else ","
    return [[c.strip() for c in row] for row in csv.reader(io.StringIO(text), delimiter=delim)]


def parse_sheet(text: str, info: dict | None = None) -> list[dict]:
    """Columna de producto (obligatoria), umbral de bajada y activo (opcionales).

    Reconoce encabezados como "Producto", "Productos", "Nombre del producto"...;
    salta filas de título; si no reconoce ninguno, usa la primera columna.
    `info` (opcional) recibe datos de diagnóstico.
    """
    info = info if info is not None else {}
    table = [r for r in _rows_of(text) if any(r)]
    info.update(lines=len(table), headers=table[0] if table else [], name_col=None, guessed=False)
    if not table:
        return []

    header_idx, name_col = None, None
    for i, row in enumerate(table[:5]):
        for j, h in enumerate(row):
            if any(k in _key(h) for k in NAME_KEYS):
                header_idx, name_col = i, j
                break
        if header_idx is not None:
            break
    if header_idx is None:
        # sin encabezado reconocible: primera columna con texto, primera fila como encabezado
        header_idx = 0
        name_col = next((j for j, h in enumerate(table[0]) if h), 0)
        info["guessed"] = True
    headers = [_key(h) for h in table[header_idx]]
    info.update(headers=table[header_idx], name_col=table[header_idx][name_col] if table[header_idx] else "")
    thr_col = next((j for j, h in enumerate(headers) if "umbral" in h or "bajada" in h), None)
    act_col = next((j for j, h in enumerate(headers) if "activo" in h), None)

    def cell(row, j):
        return row[j] if j is not None and j < len(row) else ""

    out, seen = [], set()
    for row in table[header_idx + 1:]:
        name = " ".join(cell(row, name_col).split())
        if not name or name.lower() in seen:
            continue
        if _key(cell(row, act_col)) in OFF_VALUES:
            continue
        thr = None
        raw_thr = cell(row, thr_col).replace("%", "").replace(",", ".").strip()
        if raw_thr:
            try:
                thr = float(raw_thr)
            except ValueError:
                pass
        seen.add(name.lower())
        out.append({"producto": name, "umbral": thr})
    return out


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252")  # CSV guardado con Excel en Windows


def read_sheet(cfg: dict) -> list[dict]:
    url = (cfg.get("sheet_csv_url") or "").strip()
    if url:
        source = to_csv_url(url)
        r = requests.get(source, timeout=30)
        if r.status_code in (401, 403, 404):
            raise SystemExit(f"Hoja: HTTP {r.status_code} al leer {source}\n"
                             "Revisa el enlace y compártela como 'Cualquier persona con el enlace: Lector'.")
        r.raise_for_status()
        text = _decode(r.content)
        if "text/html" in r.headers.get("Content-Type", "") or text.lstrip().lower().startswith(("<!doctype", "<html")):
            raise SystemExit("Hoja: Google devolvió una página web en lugar del CSV.\n"
                             "La hoja no es pública: Compartir → Acceso general → "
                             "'Cualquier persona con el enlace' (Lector).")
    else:
        local = _resolve(cfg.get("sheet_local_csv") or "productos.csv")
        if not local.exists():
            raise SystemExit("Configura 'sheet_csv_url' en config.yaml (o crea productos.csv).")
        source = str(local)
        text = _decode(local.read_bytes())

    info: dict = {}
    rows = parse_sheet(text, info)
    print(f"Hoja: {source} → {len(rows)} producto(s) leído(s)")
    if info.get("guessed") and rows:
        print(f"  (no encontré una columna 'producto'; usé la columna '{info['name_col']}')")
    if not rows:
        print("  ⚠ No se leyó ningún producto.")
        if not info.get("lines"):
            print("  El CSV está vacío. Si la hoja tiene varias pestañas, copia el enlace "
                  "con la pestaña correcta abierta (incluye '#gid=...').")
        else:
            print(f"  Encabezados encontrados: {info.get('headers')}")
            print("  Esperaba una columna llamada 'producto' con un producto por fila, por ejemplo:")
            print("    producto,umbral_bajada,activo\n    pony malta 1 litro,,si")
            print("  Primeras líneas recibidas:")
            for ln in text.lstrip("﻿").splitlines()[:3]:
                print("   |", ln[:120])
    return rows


def build_stores(cfg: dict) -> dict[str, VtexStore]:
    out = {}
    for key, sc in cfg["stores"].items():
        if sc.get("enabled", True):
            out[key] = VtexStore(key, sc.get("sales_channel") or None,
                                 delay=cfg["request_delay_seconds"])
    return out


# ---------------------------------------------------------------------------
# Emparejamiento
# ---------------------------------------------------------------------------
def _fmt(sc: Scored) -> str:
    s = sc.snap
    ppu = f"{al.money(s.price_per_unit)}/{reference_label(s.quantity)}" if s.price_per_unit else ""
    extra = f" [{', '.join(sc.notes)}]" if sc.notes else ""
    return f"{s.name} | {s.brand} | {s.presentation} | {al.money(s.price)} {ppu} (puntaje {sc.score:.0f}){extra}"


def ask_choice(store: str, result: MatchResult, inp=input) -> Optional[Scored]:
    cands = result.candidates.get(store, [])[:5]
    suggested = result.chosen.get(store)
    print(f"\n  {STORES[store]['name']}:")
    if not cands:
        print("    (sin resultados)")
        return None
    default = 1
    for i, c in enumerate(cands, 1):
        mark = "*" if suggested and c.snap.sku == suggested.snap.sku else " "
        if mark == "*":
            default = i
        print(f"   {mark}{i}. {_fmt(c)}")
    print("    0. No está en esta tienda")
    while True:
        ans = inp(f"  Elige [0-{len(cands)}] (Enter = {default}): ").strip()
        if not ans:
            return cands[default - 1]
        if ans.isdigit() and 0 <= int(ans) <= len(cands):
            return cands[int(ans) - 1] if int(ans) else None
        print("  Opción no válida.")


def resolve_product(query: str, stores: dict[str, VtexStore], db: Storage, n: int,
                    interactive: bool, force: bool = False, inp=input) -> str:
    """Empareja un producto de la hoja. Devuelve el estado resultante."""
    existing = db.get_matches(query)
    if not force and existing and all(
            existing.get(s) is not None and existing[s]["status"] != "pendiente" for s in stores):
        return "ya_emparejado"
    intent = Intent.from_text(query)
    results = {}
    for key, store in stores.items():
        try:
            results[key] = search_with_fallback(store, intent, n)
        except Exception as exc:
            log.error("Búsqueda '%s' en %s falló: %s", query, key, exc)
            results[key] = []
    res = match(intent, results)

    if res.method == "ninguno":
        # sin resultados (o tiendas caídas): no se guarda nada y se reintenta la próxima vez
        log.warning("'%s' no se encontró en ninguna tienda", query)
        if interactive:
            print(f"\n✖ {query}: no se encontró en ninguna tienda (revisa el texto o monitor.log)")
        return "no_encontrado"

    if res.confident:
        for key in stores:
            ch = res.chosen.get(key)
            if ch:
                db.save_match(query, key, "auto", ch.snap, res.method, ch.score)
            else:
                db.save_match(query, key, "no_existe", method=res.method)
        log.info("'%s' emparejado automáticamente (%s)", query, res.method)
        if interactive:
            print(f"\n✔ {query}  [{res.method}]")
            for key in stores:
                ch = res.chosen.get(key)
                print(f"   {STORES[key]['name']}: {_fmt(ch) if ch else 'no disponible'}")
        return "auto"

    if interactive:
        print(f"\n? {query}: coincidencia dudosa ({res.method}). {' '.join(res.notes)}")
        for key in stores:
            ch = ask_choice(key, res, inp)
            if ch:
                db.save_match(query, key, "confirmado", ch.snap, "manual", ch.score)
            else:
                db.save_match(query, key, "no_existe", method="manual")
        return "confirmado"

    for key in stores:
        ch = res.chosen.get(key)
        db.save_match(query, key, "pendiente", ch.snap if ch else None, res.method,
                      ch.score if ch else None)
    log.info("'%s' queda pendiente de confirmación", query)
    return "pendiente"


def cmd_match(cfg, db, stores, rows, reelegir: Optional[str] = None, inp=input) -> dict:
    interactive = sys.stdin is not None and sys.stdin.isatty()
    counts: dict[str, int] = {}
    targets = rows
    if reelegir:
        targets = [r for r in rows if r["producto"].lower() == reelegir.lower()] or [{"producto": reelegir}]
    for r in targets:
        st = resolve_product(r["producto"], stores, db, cfg["search_results"],
                             interactive=interactive or bool(reelegir), force=bool(reelegir), inp=inp)
        counts[st] = counts.get(st, 0) + 1
    names = {"auto": "emparejados automáticamente", "confirmado": "confirmados por ti",
             "pendiente": "pendientes", "no_encontrado": "no encontrados",
             "ya_emparejado": "ya estaban emparejados"}
    if not targets:
        print("\nResumen: la hoja no tiene productos para emparejar (revisa el mensaje de arriba).")
    else:
        print("\nResumen:", ", ".join(f"{v} {names.get(k, k)}" for k, v in counts.items()))
    return counts


# ---------------------------------------------------------------------------
# Monitoreo
# ---------------------------------------------------------------------------
def cmd_run(cfg, db, stores, rows, notifier: al.Notifier, dry_run=False) -> list[al.Alert]:
    th = cfg["thresholds"]
    all_alerts: list[al.Alert] = []
    pending = []
    for r in rows:
        query = r["producto"]
        status = resolve_product(query, stores, db, cfg["search_results"], interactive=False)
        if status == "pendiente":
            pending.append(query)
        elif status == "no_encontrado":
            print(f"✖ '{query}' no se encontró en ninguna tienda (ver monitor.log)")
        matches = db.get_matches(query)
        snaps: dict[str, ProductSnapshot] = {}
        for key, store in stores.items():
            m = matches.get(key)
            if not m or m["status"] not in ACTIVE or not m["sku"]:
                continue
            try:
                snap = store.fetch_by_sku(m["sku"], m["url"] or "")
            except Exception as exc:
                log.error("Consulta %s en %s falló: %s", query, key, exc)
                snap = None
            if snap is None:
                log.warning("Sin datos para '%s' en %s (sku %s)", query, key, m["sku"])
                continue
            # conserva la presentación del emparejamiento si la consulta no la trae
            if snap.unit_qty is None and m["unit_qty"]:
                snap.unit_qty, snap.unit = m["unit_qty"], m["unit"]
                snap.price_per_unit = price_per_reference(snap.price, Quantity(m["unit_qty"], m["unit"]))
            all_alerts += al.check_price_drop(query, snap, db, r.get("umbral") or th["drop_pct"],
                                              th["average_days"])
            if cfg["notifications"].get("discounts"):
                all_alerts += al.check_discount(query, snap, th["discount_pct"])
            if not dry_run:
                db.add_price(query, snap)
                db.set_base_price(query, key, snap)
            snaps[key] = snap
            log.info("%s | %s | %s | %s", query, key, snap.name, al.money(snap.price))
        all_alerts += al.check_cross_store(query, snaps, th["cross_store_pct"])

    if pending:
        all_alerts.append(al.Alert(
            key=f"pendiente:{'|'.join(sorted(pending))}",
            title=f"📝 {len(pending)} producto(s) por confirmar",
            message="Ejecuta: python monitor.py match  → " + ", ".join(pending)[:200],
            kind="pendiente"))
    n = al.dispatch(all_alerts, db, notifier, th["cooldown_hours"], dry_run)
    print(f"\n{len(rows)} productos revisados, {n} alerta(s) enviada(s).")
    return all_alerts


def cmd_report(db: Storage, rows: list[dict] | None):
    queries = [r["producto"] for r in rows] if rows else sorted({m["query"] for m in db.all_matches()})
    if not queries:
        print("No hay productos emparejados todavía; ejecuta 'python monitor.py match'.")
        return
    for q in queries:
        ms = db.get_matches(q)
        print(f"\n■ {q}")
        if not ms:
            print("   (sin emparejar: ejecuta 'python monitor.py match')")
            continue
        for key, m in ms.items():
            name = STORES[key]["name"]
            if m["status"] == "no_existe":
                print(f"   {name:6} — no disponible")
                continue
            if m["status"] == "pendiente":
                print(f"   {name:6} [pendiente] sugerido: {m['name'] or '—'} → ejecuta 'monitor.py match'")
                continue
            last = db.last_price(q, key, m["sku"]) if m["sku"] else None
            pres = m["unit_qty"] and Quantity(m["unit_qty"], m["unit"]).label()
            price = last["price"] if last else None
            ppu = last["price_per_unit"] if last else None
            print(f"   {name:6} [{m['status']}] {m['name']} ({pres or '?'})"
                  f"\n          actual {al.money(price)} | base {al.money(m['base_price'])}"
                  f" | por unidad {al.money(ppu)}{'' if not last else ' · ' + last['ts'][:16]}")


# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Monitor de precios Éxito / Jumbo")
    ap.add_argument("comando", choices=["match", "run", "report"])
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    ap.add_argument("--dry-run", action="store_true", help="no guarda precios ni envía notificaciones")
    ap.add_argument("--reelegir", help="vuelve a emparejar este producto")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_config(Path(args.config))
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(_resolve(cfg["log_path"]), encoding="utf-8")]
        + ([logging.StreamHandler()] if args.verbose else []))
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    db = Storage(str(_resolve(cfg["db_path"])))
    try:
        if args.comando == "report":
            try:
                rows = read_sheet(cfg)
            except (SystemExit, requests.RequestException):
                rows = None
            cmd_report(db, rows)
            return
        rows = read_sheet(cfg)
        stores = build_stores(cfg)
        if args.comando == "match":
            cmd_match(cfg, db, stores, rows, args.reelegir)
        else:
            notifier = al.Notifier(cfg["notifications"]["enabled"] and not args.dry_run)
            cmd_run(cfg, db, stores, rows, notifier, args.dry_run)
    finally:
        db.close()


if __name__ == "__main__":
    main()
