import copy

import alerts as al
import monitor
from storage import Storage
from helpers import FakeStore, vtex_product
from test_matcher import EXITO, JUMBO

CFG = monitor._merge(monitor.DEFAULTS, {})


class SilentNotifier(al.Notifier):
    def __init__(self):
        super().__init__(enabled=False)
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)


def _stores(exito=EXITO, jumbo=JUMBO):
    return {"exito": FakeStore("exito", copy.deepcopy(exito)),
            "jumbo": FakeStore("jumbo", copy.deepcopy(jumbo))}


def test_parse_sheet():
    text = "﻿Producto,Umbral_Bajada,Activo\npony malta 1 litro,,si\n" \
           "queso mozarella alpina,15%,\narroz,,no\n,,\nPony Malta 1 litro,,\n"
    rows = monitor.parse_sheet(text)
    assert rows == [{"producto": "pony malta 1 litro", "umbral": None},
                    {"producto": "queso mozarella alpina", "umbral": 15.0}]


def test_to_csv_url():
    u = "https://docs.google.com/spreadsheets/d/ABC123/edit#gid=456"
    assert monitor.to_csv_url(u) == \
        "https://docs.google.com/spreadsheets/d/ABC123/export?format=csv&gid=456"


def test_run_first_time_then_drop_and_cross_store(tmp_path):
    db = Storage(str(tmp_path / "p.db"))
    rows = [{"producto": "pony malta 1 litro", "umbral": None}]
    stores = _stores()
    n = SilentNotifier()

    # Primera ejecución: guarda base, sin alerta de bajada; diferencia 3900 vs 3600 < 15 %
    alerts1 = monitor.cmd_run(CFG, db, stores, rows, n)
    assert not [a for a in alerts1 if a.kind == "bajada"]
    m = db.get_matches("pony malta 1 litro")
    assert m["exito"]["base_price"] == 3900 and m["jumbo"]["base_price"] == 3600

    # Baja Éxito a 2900 (-25 %): alerta de bajada y de diferencia entre tiendas
    stores["exito"].products[0]["items"][0]["sellers"][0]["commertialOffer"]["Price"] = 2900
    stores["jumbo"].products[0]["items"][0]["sellers"][0]["commertialOffer"]["Price"] = 3600
    alerts2 = monitor.cmd_run(CFG, db, stores, rows, n)
    kinds = {a.kind for a in alerts2}
    assert "bajada" in kinds and "diferencia" in kinds
    drop = next(a for a in alerts2 if a.kind == "bajada")
    assert "Éxito" in drop.title and "mínimo histórico" in drop.message
    sent_before = len(n.sent)

    # Misma situación otra vez: el enfriamiento evita repetir la diferencia
    monitor.cmd_run(CFG, db, stores, rows, n)
    assert len(n.sent) == sent_before
    # el precio base no cambia
    assert db.get_matches("pony malta 1 litro")["exito"]["base_price"] == 3900


def test_ambiguous_goes_pending_and_notifies(tmp_path):
    db = Storage(str(tmp_path / "p.db"))
    rows = [{"producto": "queso mozarella alpina", "umbral": None}]
    n = SilentNotifier()
    alerts = monitor.cmd_run(CFG, db, _stores(), rows, n)
    assert any(a.kind == "pendiente" for a in alerts)
    assert all(m["status"] == "pendiente" for m in db.get_matches(rows[0]["producto"]).values())
    assert db.last_price(rows[0]["producto"], "exito", "e3") is None  # no se monitorea aún


def test_interactive_confirmation(tmp_path):
    db = Storage(str(tmp_path / "p.db"))
    q = "queso mozarella alpina"
    stores = _stores()
    answers = iter(["", "0"])  # Éxito: sugerida; Jumbo: no está
    st = monitor.resolve_product(q, stores, db, 20, interactive=True, inp=lambda _: next(answers))
    assert st == "confirmado"
    m = db.get_matches(q)
    assert m["exito"]["status"] == "confirmado" and m["exito"]["sku"] in ("e3", "e4")
    assert m["jumbo"]["status"] == "no_existe"
    # ya emparejado: no vuelve a preguntar
    assert monitor.resolve_product(q, stores, db, 20, interactive=True) == "ya_emparejado"


def test_cross_store_different_sizes_uses_unit_price():
    from stores import parse_vtex_product
    a = parse_vtex_product("exito", vtex_product("1", "a", "Queso Alpina 400 g", "Alpina", 20000),
                           "https://www.exito.com")[0]
    b = parse_vtex_product("jumbo", vtex_product("2", "b", "Queso Alpina 250 g", "Alpina", 10000),
                           "https://www.jumbocolombia.com")[0]
    [alert] = al.check_cross_store("queso", {"exito": a, "jumbo": b}, 15)
    # 50.000/kg vs 40.000/kg → Jumbo 20 % más barato
    assert "Jumbo" in alert.title and "20%" in alert.title
    assert "presentaciones distintas" in alert.message


def test_discount_alert():
    from stores import parse_vtex_product
    s = parse_vtex_product("exito", vtex_product("1", "a", "Pony 1 L", "Pony", 3000, 4000),
                           "https://www.exito.com")[0]
    [a] = al.check_discount("pony", s, 20)
    assert "25%" in a.title


def test_not_found_is_not_saved(tmp_path):
    db = Storage(str(tmp_path / "p.db"))
    stores = {"exito": FakeStore("exito", []), "jumbo": FakeStore("jumbo", [])}
    st = monitor.resolve_product("producto inexistente", stores, db, 20, interactive=False)
    assert st == "no_encontrado" and db.get_matches("producto inexistente") == {}


def test_sheet_flexible_headers():
    assert [r["producto"] for r in monitor.parse_sheet("Productos\npony malta 1 litro\n")] == \
        ["pony malta 1 litro"]
    rows = monitor.parse_sheet("Nombre del Artículo;Umbral de bajada %;Activo\n"
                               "queso mozarella alpina;15;Sí\narroz;;No\n")
    assert rows == [{"producto": "queso mozarella alpina", "umbral": 15.0}]


def test_sheet_title_row_and_guess():
    text = "Mi lista de mercado,,\n,,\nProducto,Activo,\npony malta 1 litro,si,\n"
    assert [r["producto"] for r in monitor.parse_sheet(text)] == ["pony malta 1 litro"]
    info = {}
    rows = monitor.parse_sheet("Cosas\npony malta\narroz diana\n", info)
    assert [r["producto"] for r in rows] == ["pony malta", "arroz diana"]
    assert info["guessed"] is True


def test_read_sheet_reports_zero_rows(tmp_path, capsys):
    f = tmp_path / "productos.csv"
    f.write_text("producto,activo\n,\n", encoding="utf-8")
    assert monitor.read_sheet({"sheet_csv_url": "", "sheet_local_csv": str(f)}) == []
    out = capsys.readouterr().out
    assert "0 producto" in out and "No se leyó ningún producto" in out


def test_read_sheet_cp1252(tmp_path, capsys):
    f = tmp_path / "productos.csv"
    f.write_bytes("Producto;Activo\nAzúcar Manuelita 1 kg;si\n".encode("cp1252"))
    rows = monitor.read_sheet({"sheet_csv_url": "", "sheet_local_csv": str(f)})
    assert rows[0]["producto"] == "Azúcar Manuelita 1 kg"
    assert "1 producto(s)" in capsys.readouterr().out


def test_report_empty(tmp_path, capsys):
    monitor.cmd_report(Storage(str(tmp_path / "p.db")), [])
    assert "No hay productos emparejados" in capsys.readouterr().out


def test_blocked_store_is_retried_later(tmp_path):
    from stores import StoreBlocked
    db = Storage(str(tmp_path / "p.db"))
    stores = _stores()

    def blocked(*a, **k):
        raise StoreBlocked("Éxito rechazó la consulta")
    stores["exito"].search = blocked
    st = monitor.resolve_product("pony malta 1 litro", stores, db, 20, interactive=False)
    m = db.get_matches("pony malta 1 litro")
    assert st == "auto" and m["jumbo"]["sku"] == "j1" and "exito" not in m
    # cuando Éxito vuelve a responder, se empareja usando el EAN de Jumbo
    stores["exito"] = FakeStore("exito", copy.deepcopy(EXITO))
    assert monitor.resolve_product("pony malta 1 litro", stores, db, 20, interactive=False) == "auto"
    assert db.get_matches("pony malta 1 litro")["exito"]["sku"] == "e1"
