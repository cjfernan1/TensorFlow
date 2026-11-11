from matcher import Intent, match, normalize, search_with_fallback
from helpers import FakeStore, vtex_product

EXITO = [
    vtex_product("1", "e1", "Bebida De Malta Pony Malta 1000 ml", "PONY MALTA", 3900, ean="7702004000011"),
    vtex_product("2", "e2", "Bebida De Malta Pony Malta 330 ml", "PONY MALTA", 1900, ean="7702004000028"),
    vtex_product("3", "e3", "Queso Mozzarella Alpina Bloque 400 g", "ALPINA", 17900, ean="7702001100004"),
    vtex_product("4", "e4", "Queso Mozzarella Alpina Tajado 250 g", "ALPINA", 12900, ean="7702001100005"),
    vtex_product("5", "e5", "Queso Mozzarella Colanta 400 g", "COLANTA", 15900, ean="7702129000001"),
]
JUMBO = [
    vtex_product("10", "j1", "Malta Pony 1 Lt", "Pony Malta", 3600, ean="7702004000011"),
    vtex_product("11", "j2", "Malta Pony Lata 330ml", "Pony Malta", 2000, ean="7702004000028"),
    vtex_product("12", "j3", "Queso Alpina mozzarella 400 gr", "Alpina", 16500, ean="7702001100004"),
    vtex_product("13", "j4", "Queso Alpina mozzarella tajado 250gr", "Alpina", 11900, ean="7702001100005"),
]


def _results(query):
    intent = Intent.from_text(query)
    stores = {"exito": FakeStore("exito", EXITO), "jumbo": FakeStore("jumbo", JUMBO)}
    return intent, {k: search_with_fallback(s, intent) for k, s in stores.items()}


def test_normalize_typos_and_accents():
    assert normalize("Queso MOZARELLA de Alpína") == ["queso", "mozzarella", "alpina"]


def test_match_by_ean_with_different_names():
    intent, res = _results("pony malta 1 litro")
    m = match(intent, res)
    assert m.method == "ean"
    assert m.chosen["exito"].snap.sku == "e1"
    assert m.chosen["jumbo"].snap.sku == "j1"
    assert m.confident


def test_quantity_filters_out_other_sizes():
    intent, res = _results("pony malta 1 litro")
    m = match(intent, res)
    assert all(c.snap.sku not in ("e2", "j2") for c in m.candidates["exito"] + m.candidates["jumbo"])


def test_ambiguous_without_size_is_not_confident():
    intent, res = _results("queso mozarella alpina")
    m = match(intent, res)
    assert not m.confident
    # Colanta no debe quedar por encima de Alpina
    assert m.chosen["exito"].snap.brand == "ALPINA"
    # las opciones incluyen ambas presentaciones de Alpina
    skus = {c.snap.sku for c in m.candidates["exito"]}
    assert {"e3", "e4"} <= skus


def test_size_given_makes_it_confident():
    intent, res = _results("queso mozarella alpina 400 g")
    m = match(intent, res)
    assert m.confident and m.method == "ean"
    assert (m.chosen["exito"].snap.sku, m.chosen["jumbo"].snap.sku) == ("e3", "j3")


def test_without_common_ean_uses_brand_and_size():
    jumbo = [vtex_product("12", "j3", "Queso Alpina mozzarella 400 gr", "Alpina", 16500, ean="")]
    intent = Intent.from_text("queso mozarella alpina 400 g")
    res = {"exito": FakeStore("exito", EXITO).search("queso mozarella alpina 400 g"),
           "jumbo": FakeStore("jumbo", jumbo).search("queso mozarella alpina 400 g")}
    m = match(intent, res)
    assert m.method == "marca+presentacion"
    assert m.chosen["jumbo"].snap.sku == "j3" and m.chosen["exito"].snap.sku == "e3"


def test_only_one_store():
    intent = Intent.from_text("queso mozzarella colanta 400 g")
    res = {"exito": FakeStore("exito", EXITO).search("queso mozzarella colanta"),
           "jumbo": []}
    m = match(intent, res)
    assert m.chosen["exito"].snap.sku == "e5" and m.chosen["jumbo"] is None


def test_search_fallback_shortens_query():
    store = FakeStore("exito", EXITO)
    store.search = lambda text, n=20, _orig=store.search: [] if "litro" in text else _orig(text, n)
    intent = Intent.from_text("pony malta 1 litro")
    assert search_with_fallback(store, intent)
