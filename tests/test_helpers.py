"""Tests de las partes puras y de la capa de base de datos.

Nada de esto toca Salesforce ni imgbb: se ejercitan los helpers y las rutas que solo
dependen de SQLite. Para correrlos:

    pip install pytest
    python -m pytest tests -v

El DB_PATH se apunta a un archivo temporal ANTES de importar main, porque main lee la
variable de entorno al importarse y crea las tablas en ese momento.
"""
import importlib
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="module")
def app_mod():
    tmpdir = tempfile.mkdtemp(prefix="entregas-test-")
    os.environ["DB_PATH"] = os.path.join(tmpdir, "test.db")
    os.environ["ADMIN_KEY"] = "clave-de-prueba"
    os.environ["ADMIN_TOKEN"] = "token-de-prueba"

    import main
    importlib.reload(main)  # por si otro test lo importó con otro DB_PATH
    return main


@pytest.fixture()
def client(app_mod):
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


# ---------- normalizar_op ----------
@pytest.mark.parametrize("entrada,esperado", [
    ("13563", "OP-0013563"),
    ("OP-13563", "OP-0013563"),
    ("op 13563", "OP-0013563"),
    ("  13563  ", "OP-0013563"),
    ("13.563", "OP-0013563"),
    ("1", "OP-0000001"),
    ("", "OP-0000000"),
    (None, "OP-0000000"),
])
def test_normalizar_op(app_mod, entrada, esperado):
    assert app_mod.normalizar_op(entrada) == esperado


def test_normalizar_op_ignora_letras(app_mod):
    # El número se usa para armar una query SOQL, así que todo lo que no sea dígito
    # tiene que desaparecer antes.
    assert app_mod.normalizar_op("13563'; DROP--") == "OP-0013563"


# ---------- _secret_ok ----------
def test_secret_ok_compara_bien(app_mod):
    assert app_mod._secret_ok("abc", "abc") is True
    assert app_mod._secret_ok("abc", "abd") is False
    assert app_mod._secret_ok("", "abc") is False


def test_secret_ok_falla_si_no_hay_secreto(app_mod):
    # Fail-closed: sin secreto configurado, nadie entra (ni con string vacío).
    assert app_mod._secret_ok("cualquier-cosa", None) is False
    assert app_mod._secret_ok("", None) is False
    assert app_mod._secret_ok("", "") is False


def test_secret_ok_acepta_no_ascii(app_mod):
    # compare_digest con str lanzaría TypeError si la clave tuviera acentos.
    assert app_mod._secret_ok("clavé", "clavé") is True
    assert app_mod._secret_ok("clave", "clavé") is False


# ---------- fechas ----------
def test_hoy_ec_formato(app_mod):
    valor = app_mod.hoy_ec()
    assert len(valor) == 10
    assert valor[4] == "-" and valor[7] == "-"


# ---------- deliveries ----------
def test_save_y_leer_delivery(app_mod):
    hoy = app_mod.hoy_ec()
    antes = len(app_mod.get_deliveries_for_date(hoy))

    app_mod.save_delivery("OP-0000123", "JUAN CHOCA", "<a href='x'>Foto 1</a>", photo_count=2)

    filas = app_mod.get_deliveries_for_date(hoy)
    assert len(filas) == antes + 1
    fila = filas[0]  # más reciente primero
    assert fila["op"] == "OP-0000123"
    assert fila["client"] == "JUAN CHOCA"
    assert fila["photo_count"] == 2


def test_deliveries_de_otro_dia_no_aparecen(app_mod):
    assert app_mod.get_deliveries_for_date("1999-01-01") == []


def test_api_history_solo_devuelve_hoy(client, app_mod):
    app_mod.save_delivery("OP-0000999", "CLIENTE DE HOY", "", photo_count=1)

    res = client.get("/api/history")
    assert res.status_code == 200
    data = res.get_json()

    assert data["date_key"] == app_mod.hoy_ec()
    clientes = [d["client"] for d in data["deliveries"]]
    assert "CLIENTE DE HOY" in clientes


# ---------- jobs ----------
def test_create_upload_job_es_idempotente(app_mod):
    assert app_mod.create_upload_job("job-unico-1", ["13563"], 1) is True
    # El mismo job_id no se inserta dos veces: es lo que evita subir dos veces las
    # fotos cuando Background Sync reintenta.
    assert app_mod.create_upload_job("job-unico-1", ["13563"], 1) is False


def test_progreso_del_job_se_guarda_y_recupera(app_mod):
    app_mod.create_upload_job("job-progreso", ["13563"], 2)
    assert app_mod.load_job_progress("job-progreso") == {}

    app_mod.save_job_progress("job-progreso", {"photos": {"a.jpg": {"cdid": "069", "url": "http://x"}}})

    recuperado = app_mod.load_job_progress("job-progreso")
    assert recuperado["photos"]["a.jpg"]["cdid"] == "069"


def test_reclaim_libera_jobs_viejos(app_mod):
    app_mod.create_upload_job("job-zombie", ["13563"], 1)

    # Se antigüa el job a mano para simular un worker muerto.
    with app_mod.db() as conn:
        conn.execute(
            "UPDATE upload_jobs SET updated_at = ? WHERE job_id = ?",
            ("2020-01-01T00:00:00-05:00", "job-zombie"),
        )

    assert app_mod.reclaim_stale_jobs() >= 1
    assert app_mod.get_upload_job("job-zombie")["status"] == "error"


def test_reclaim_no_toca_jobs_recientes(app_mod):
    app_mod.create_upload_job("job-fresco", ["13563"], 1)
    app_mod.reclaim_stale_jobs()
    assert app_mod.get_upload_job("job-fresco")["status"] == "processing"


# ---------- autenticación ----------
def test_borrar_historial_sin_clave_da_403(client):
    res = client.post("/api/historial/borrar")
    assert res.status_code == 403


def test_borrar_historial_con_clave_mala_da_403(client):
    res = client.post("/api/historial/borrar", headers={"X-Admin-Key": "no-es"})
    assert res.status_code == 403


def test_borrar_historial_con_clave_funciona(client, app_mod):
    app_mod.save_delivery("OP-0000777", "PARA BORRAR", "", photo_count=1)

    res = client.post("/api/historial/borrar?solo_hoy=1",
                      headers={"X-Admin-Key": "clave-de-prueba"})
    assert res.status_code == 200
    assert res.get_json()["ok"] is True
    assert app_mod.get_deliveries_for_date(app_mod.hoy_ec()) == []


def test_admin_logs_sin_token_da_403(client):
    assert client.get("/admin/logs").status_code == 403
    assert client.get("/admin/logs/json").status_code == 403


def test_historial_admin_solo_con_clave_correcta(client):
    # "Zona de pruebas" es el título del bloque {% if admin %}. No sirve buscar el texto
    # de los botones: aparece también en los confirm() del JS, que se renderiza siempre.
    marcador = "Zona de pruebas".encode()

    # Sin clave: la página carga pero sin los botones de borrado.
    assert marcador not in client.get("/historial").data
    # "admin1" era la clave hardcodeada vieja: ya no habilita nada.
    assert marcador not in client.get("/historial/admin1").data
    assert marcador in client.get("/historial/clave-de-prueba").data


# ---------- render de las pantallas ----------
# Ninguna de estas rutas toca Salesforce, así que sirven para verificar que los
# templates (y la herencia de base.html) compilan y renderizan.
@pytest.mark.parametrize("ruta,marcador", [
    ("/", b"Gu\xc3\xadas de Entrega"),
    ("/registro", b"Control de Entregas"),
    ("/historial", b"Historial de Entregas"),
])
def test_pantallas_renderizan(client, ruta, marcador):
    res = client.get(ruta)
    assert res.status_code == 200
    assert marcador in res.data


@pytest.mark.parametrize("ruta", ["/", "/registro", "/historial"])
def test_pantallas_usan_css_local_y_no_el_cdn(client, ruta):
    # El CDN de Tailwind rompía el modo offline: la app tiene que abrir sin señal.
    data = client.get(ruta).data
    assert b"/static/css/app.css" in data
    assert b"cdn.tailwindcss.com" not in data


def test_esc_esta_en_todas_las_pantallas(client):
    # Viene de base.html; si faltara, los innerHTML de las tres pantallas romperían.
    for ruta in ("/", "/registro", "/historial"):
        assert b"function esc(s)" in client.get(ruta).data


def test_service_worker_y_manifest_se_sirven_desde_la_raiz(client):
    sw = client.get("/sw.js")
    assert sw.status_code == 200
    assert b"entregas-shell" in sw.data

    man = client.get("/manifest.webmanifest")
    assert man.status_code == 200
    assert man.get_json()["start_url"] == "/"


def test_index_declara_el_manifest(client):
    assert b'rel="manifest"' in client.get("/").data


# ---------- salud y alertas ----------
def test_health_responde_sin_token(client):
    # Los monitores de uptime gratuitos no mandan headers.
    res = client.get("/health")
    assert res.status_code in (200, 503)
    data = res.get_json()
    assert "ok" in data and "checks" in data


def test_health_avisa_si_faltan_credenciales(client, app_mod, monkeypatch):
    # En el entorno de test no hay credenciales de Salesforce, así que tiene que
    # reportarse como no sano en vez de fingir que todo anda.
    monkeypatch.setattr(app_mod, "SF_USER", None)
    res = client.get("/health")
    assert res.status_code == 503
    assert res.get_json()["ok"] is False
    assert "SF_USER" in res.get_json()["checks"]["config"]


def test_health_marca_no_sano_si_hay_avisos_sin_enviar(client, app_mod, monkeypatch):
    for var in ("SF_USER", "SF_PASS", "SF_SECURITY_TOKEN", "IMGBB_API_KEY", "MAKE_WEBHOOK_URL"):
        monkeypatch.setattr(app_mod, var, "puesto")

    app_mod.save_delivery("OP-0000555", "SIN AVISO", "", photo_count=1, webhook_ok=False)

    data = client.get("/health").get_json()
    assert data["checks"]["avisos_no_enviados_24h"] >= 1
    assert data["ok"] is False


def test_delivery_guarda_el_estado_del_webhook(app_mod):
    app_mod.save_delivery("OP-0000556", "CON AVISO", "", photo_count=1, webhook_ok=True)
    app_mod.save_delivery("OP-0000557", "SIN AVISO", "", photo_count=1, webhook_ok=False)

    with app_mod.db() as conn:
        ok = conn.execute("SELECT webhook_ok FROM deliveries WHERE op = ?", ("OP-0000556",)).fetchone()[0]
        fallo = conn.execute("SELECT webhook_ok FROM deliveries WHERE op = ?", ("OP-0000557",)).fetchone()[0]
    assert ok == 1
    assert fallo == 0


def test_historial_expone_webhook_ok(client, app_mod):
    app_mod.save_delivery("OP-0000558", "PARA HISTORIAL", "", photo_count=1, webhook_ok=False)
    dias = client.get("/api/historial/data").get_json()
    entregas = [d for dia in dias for d in dia["deliveries"] if d["op"] == "OP-0000558"]
    assert entregas and entregas[0]["webhook_ok"] is False


def test_notify_alert_sin_webhook_no_hace_nada(app_mod, monkeypatch):
    monkeypatch.setattr(app_mod, "ALERT_WEBHOOK_URL", None)
    assert app_mod.notify_alert("cualquiera", "titulo") is False


def test_notify_alert_respeta_el_cooldown(app_mod, monkeypatch):
    enviadas = []
    monkeypatch.setattr(app_mod, "ALERT_WEBHOOK_URL", "http://ejemplo.invalido/alerta")
    # Se intercepta el envío real: no queremos tráfico de red en los tests.
    monkeypatch.setattr(app_mod.threading, "Thread",
                        lambda target, args, daemon: type("T", (), {"start": lambda self: enviadas.append(args)})())
    app_mod._alert_last_sent.clear()

    assert app_mod.notify_alert("clave-x", "Primera") is True
    # La segunda por la misma causa se calla: una caída con 30 entregas no puede
    # generar 30 mensajes.
    assert app_mod.notify_alert("clave-x", "Segunda") is False
    # Otra causa distinta sí pasa.
    assert app_mod.notify_alert("clave-y", "Otra") is True
    # Y forzar salta el cooldown (lo usa /health/probar-alerta).
    assert app_mod.notify_alert("clave-x", "Forzada", forzar=True) is True

    assert len(enviadas) == 3


def test_probar_alerta_requiere_token(client):
    assert client.get("/health/probar-alerta").status_code == 403


# ---------- logs ----------
def test_syslog_asigna_seq_creciente(app_mod):
    app_mod.syslog("INFO", "primero")
    app_mod.syslog("INFO", "segundo")
    seqs = [e["seq"] for e in app_mod._log_buffer]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


def test_logs_json_filtra_por_since(client, app_mod):
    app_mod.syslog("INFO", "mensaje-marcador")
    ultimo_seq = max(e["seq"] for e in app_mod._log_buffer)

    res = client.get(f"/admin/logs/json?since={ultimo_seq}",
                     headers={"X-Admin-Token": "token-de-prueba"})
    assert res.status_code == 200
    assert res.get_json() == []  # nada posterior al último

    res = client.get(f"/admin/logs/json?since={ultimo_seq - 1}",
                     headers={"X-Admin-Token": "token-de-prueba"})
    assert len(res.get_json()) == 1
