"""Tests de la capa de tools con la API de Zammad mockeada (sin red)."""
import agente_zammad
from agente_zammad import Config, ZammadClient, ejecutar_herramienta, parse_dt


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def _client():
    return ZammadClient(Config(url="https://zammad.test", token="t", verify_ssl=True))


def test_parse_dt():
    assert parse_dt("2026-03-23T10:00:00Z") is not None
    assert parse_dt(None) is None
    assert parse_dt("basura") is None


def test_obtener_tickets_filtra_estados_y_grupo(monkeypatch):
    pagina = [
        {"id": 1, "state_id": 2, "group_id": 3, "title": "abierto TI"},
        {"id": 2, "state_id": 5, "group_id": 3, "title": "merged (excluir)"},
        {"id": 3, "state_id": 2, "group_id": 2, "title": "otro grupo"},
    ]
    monkeypatch.setattr(agente_zammad.requests, "get", lambda *a, **k: FakeResponse(pagina))

    res = _client().obtener_tickets(grupo_id=3)
    assert res["total_encontrados"] == 1
    assert res["tickets"][0]["id"] == 1


def test_obtener_tickets_trunca_titulo(monkeypatch):
    largo = "x" * 200
    pagina = [{"id": 1, "state_id": 2, "group_id": 3, "title": largo}]
    monkeypatch.setattr(agente_zammad.requests, "get", lambda *a, **k: FakeResponse(pagina))

    res = _client().obtener_tickets(grupo_id=3)
    assert len(res["tickets"][0]["titulo"]) == 80


def test_verificar_servidor_ok(monkeypatch):
    monkeypatch.setattr(
        agente_zammad.requests, "get",
        lambda *a, **k: FakeResponse({"name": "Agente TI", "email": "a@b.c"}),
    )
    res = _client().verificar_servidor()
    assert res["estado"] == "OK"
    assert res["usuario_autenticado"] == "Agente TI"


def test_ejecutar_herramienta_desconocida():
    res = ejecutar_herramienta(_client(), "no_existe", {})
    assert "error" in res
