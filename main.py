import os
import json
import time
import base64
import secrets
import threading
import re
import sqlite3
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from functools import wraps

import requests
from flask import Flask, render_template, request, jsonify, abort, send_from_directory
from simple_salesforce import Salesforce, format_soql


# ===============================
# Flask App
# ===============================
app = Flask(__name__)

# Las fotos se leen enteras en memoria antes de subirlas, así que un POST gigante
# tumbaría el proceso. Con compresión previa (1024px / q0.65) una foto pesa ~60 KB.
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "40"))
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024


# ===============================
# Credenciales / configuración
# ===============================
# ADMIN_TOKEN protege el panel de diagnóstico (/admin/*).
# ADMIN_KEY protege la UI y los endpoints destructivos del supervisor
# (/historial/<clave>, /registro/<clave>, borrado de historial).
# Ambos son obligatorios: si no están definidos, esas rutas quedan cerradas.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN")
ADMIN_KEY = os.environ.get("ADMIN_KEY")
DB_PATH = os.environ.get("DB_PATH", "./entregas.db")


# ===============================
# Fechas — Ecuador (UTC-5)
# ===============================
# Arriba del archivo a propósito: save_delivery, api_history y los helpers de jobs
# dependen de esto. Antes se definía a mitad del archivo, después de las funciones
# que lo usaban, y solo funcionaba porque se ejecutan en runtime.
_ECUADOR = timezone(timedelta(hours=-5))
_MESES_FULL = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
               "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
_DIAS_FULL = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]


def hoy_ec() -> str:
    """Fecha de hoy en hora Ecuador, formato YYYY-MM-DD (la clave del historial)."""
    return datetime.now(_ECUADOR).strftime("%Y-%m-%d")


def ahora_ec_iso() -> str:
    """Timestamp actual en hora Ecuador, ISO 8601 con offset (lo que guarda la DB)."""
    return datetime.now(_ECUADOR).isoformat()


def _secret_ok(provided: str | None, expected: str | None) -> bool:
    """Comparación en tiempo constante. Fail-closed si el secreto no está configurado.

    Se comparan bytes: compare_digest con str exige ASCII puro y lanzaría TypeError
    si la clave tuviera acentos o un carácter no ASCII.
    """
    if not expected:
        return False
    return secrets.compare_digest((provided or "").encode("utf-8"), expected.encode("utf-8"))


# ===============================
# Sistema de Logs Interno
# ===============================
_log_buffer: deque = deque(maxlen=500)
_log_lock = threading.Lock()
_log_seq = 0

LOG_LEVELS = {
    "DEBUG": "🔍",
    "INFO": "ℹ️",
    "WARNING": "⚠️",
    "ERROR": "❌",
    "CRITICAL": "🔥",
}


def syslog(level: str, message: str, context: dict = None):
    global _log_seq
    with _log_lock:
        _log_seq += 1
        entry = {
            # `seq` es monotónico y nunca se reinicia: el panel pide "dame lo posterior
            # a N". Contar el largo del buffer no sirve porque es un deque acotado que
            # deja de crecer al llegar a maxlen.
            "seq": _log_seq,
            "ts": datetime.now().isoformat(timespec="seconds"),
            "level": level.upper(),
            "icon": LOG_LEVELS.get(level.upper(), "•"),
            "msg": message,
            "ctx": context or {},
        }
        _log_buffer.append(entry)
    print(f"[{entry['ts']}] [{entry['level']}] {message}" + (f" | {context}" if context else ""))


# ===============================
# Alertas — avisar cuando el servicio se rompe
# ===============================
# Sin esto, una falla silenciosa (el webhook de Make caído, imgbb sin cuota, la sesión
# de Salesforce vencida) no la nota nadie: el repartidor ve un error en el teléfono o
# ni eso, y en la oficina se asume que todo anduvo hasta que un cliente reclama.
#
# ALERT_WEBHOOK_URL puede ser un segundo escenario de Make, un webhook de Telegram,
# Slack, Discord o lo que sea: se le manda un JSON plano por POST.
ALERT_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL")
ALERT_COOLDOWN_MIN = int(os.environ.get("ALERT_COOLDOWN_MIN", "30"))

_alert_lock = threading.Lock()
_alert_last_sent: dict[str, float] = {}


def _post_alert(clave: str, payload: dict):
    """Envía la alerta. Corre en un hilo aparte: que el aviso no demore la entrega."""
    try:
        resp = requests.post(ALERT_WEBHOOK_URL, json=payload, timeout=15)
        resp.raise_for_status()
        syslog("INFO", "Alerta enviada", {"clave": clave, "status": resp.status_code})
    except Exception as e:
        syslog("ERROR", "No se pudo enviar la alerta", {"clave": clave, "error": str(e)})
        # Se libera el cooldown para que el próximo intento vuelva a probar en vez de
        # quedarse callado media hora por un fallo de red puntual.
        with _alert_lock:
            _alert_last_sent.pop(clave, None)


def notify_alert(clave: str, titulo: str, detalle: str = "", forzar: bool = False) -> bool:
    """Avisa de un problema de servicio. `clave` agrupa alertas repetidas.

    El cooldown por clave evita que una caída de Salesforce durante una ruta con 30
    entregas mande 30 mensajes. Nunca lanza excepción: una alerta rota no puede tumbar
    una entrega.
    """
    if not ALERT_WEBHOOK_URL:
        return False

    try:
        ahora = time.time()
        with _alert_lock:
            ultimo = _alert_last_sent.get(clave, 0)
            if not forzar and ahora - ultimo < ALERT_COOLDOWN_MIN * 60:
                return False  # ya avisamos hace poco por lo mismo
            _alert_last_sent[clave] = ahora

        payload = {
            "app": "guias-entrega",
            "clave": clave,
            "titulo": titulo,
            "detalle": detalle,
            "timestamp": ahora_ec_iso(),
        }
        threading.Thread(target=_post_alert, args=(clave, payload), daemon=True).start()
        return True
    except Exception as e:
        syslog("ERROR", "Fallo armando la alerta", {"clave": clave, "error": str(e)})
        return False


def require_admin(f):
    """Panel de diagnóstico. Acepta el token por header (preferido) o query string."""
    @wraps(f)
    def decorated(*args, **kwargs):
        provided = request.headers.get("X-Admin-Token") or request.args.get("token", "")
        if not _secret_ok(provided, ADMIN_TOKEN):
            abort(403)
        return f(*args, **kwargs)
    return decorated


def require_admin_key(f):
    """Endpoints destructivos del supervisor."""
    @wraps(f)
    def decorated(*args, **kwargs):
        provided = request.headers.get("X-Admin-Key") or request.args.get("key", "")
        if not _secret_ok(provided, ADMIN_KEY):
            syslog("WARNING", "Intento de acceso sin clave de admin", {"path": request.path})
            abort(403)
        return f(*args, **kwargs)
    return decorated


# ===============================
# Salesforce — con reconexión automática
# ===============================
SF_USER = os.environ.get("SF_USER")
SF_PASS = os.environ.get("SF_PASS")
SF_SECURITY_TOKEN = os.environ.get("SF_SECURITY_TOKEN")

MAKE_WEBHOOK_URL = os.environ.get("MAKE_WEBHOOK_URL")
IMGBB_API_KEY = os.environ.get("IMGBB_API_KEY")

_sf = None
_sf_lock = threading.Lock()
_sf_last_ok: float = 0
_sf_last_error: str | None = None  # último error de conexión, lo reporta /health
_SF_REVALIDATE_SECS = 300  # re-pinguear SF solo cada 5 minutos


def get_sf():
    """Devuelve una instancia de Salesforce activa, reconectando si la sesión expiró."""
    global _sf, _sf_last_ok, _sf_last_error

    # Camino rápido sin lock: es el 99% de los casos. Antes todas las requests se
    # serializaban acá, incluso cuando la sesión estaba fresca, y una reconexión de
    # 2-3 segundos hacía esperar a todos los repartidores. Una lectura desactualizada
    # solo cae al camino con lock, que sigue serializado a propósito para no disparar
    # varios logins en paralelo.
    sf_cached = _sf
    if sf_cached is not None and time.time() - _sf_last_ok < _SF_REVALIDATE_SECS:
        return sf_cached

    with _sf_lock:
        if _sf is not None:
            if time.time() - _sf_last_ok < _SF_REVALIDATE_SECS:
                return _sf  # otra request reconectó mientras esperábamos el lock
            try:
                _sf.query("SELECT Id FROM Organization LIMIT 1")
                _sf_last_ok = time.time()
                return _sf
            except Exception as e:
                syslog("WARNING", f"Sesión Salesforce muerta, reconectando... ({e})")
                _sf = None

        try:
            _sf = Salesforce(username=SF_USER, password=SF_PASS, security_token=SF_SECURITY_TOKEN)
            _sf_last_ok = time.time()
            _sf_last_error = None
            syslog("INFO", "Salesforce reconectado correctamente", {"user": SF_USER})
            return _sf
        except Exception as e:
            syslog("CRITICAL", f"Error reconectando a Salesforce: {e}", {"user": SF_USER})
            _sf = None
            _sf_last_error = str(e)
            notify_alert(
                "salesforce_caido",
                "Salesforce no responde",
                f"No se pudo conectar con el usuario {SF_USER}. Las entregas quedan en cola "
                f"en los teléfonos y no se registran. Error: {e}",
            )
            return None


# Nota: no se conecta a Salesforce al importar el módulo. get_sf() es lazy y se llama en
# cada uso, así que el login eager solo retrasaba el arranque unos segundos y registraba
# un CRITICAL si Salesforce estaba momentáneamente caído durante un deploy.


# ===============================
# Helpers — Salesforce
# ===============================
def normalizar_op(numero: str) -> str:
    numero = re.sub(r'[^0-9]', '', (numero or "").strip())
    return f"OP-{numero.zfill(7)}"


def sf_get_order_info(op_completa: str, sf_client=None) -> dict | None:
    client = sf_client or get_sf()
    if not client:
        return None
    query = format_soql(
        "SELECT Id, Name, Nombre_del_cliente__c, Link_Guia_de_Entrega__c "
        "FROM Orden_Proveedor__c "
        "WHERE Orden_Proveedor_Nro__c = {op} LIMIT 1",
        op=op_completa,
    )
    result = client.query(query)
    if result.get("totalSize", 0) == 0:
        return None
    return result["records"][0]


def sf_upload_photo(sf_client, filename: str, file_bytes: bytes) -> tuple[str, str]:
    """Sube un archivo a Salesforce Files. Retorna (content_version_id, content_doc_id)."""
    b64 = base64.b64encode(file_bytes).decode("utf-8")
    cv_result = sf_client.ContentVersion.create({
        "Title": filename,
        "PathOnClient": filename,
        "VersionData": b64,
        "Origin": "H",
    })
    cv_id = cv_result["id"]
    cv_data = sf_client.query(
        format_soql("SELECT ContentDocumentId FROM ContentVersion WHERE Id = {cv_id}", cv_id=cv_id)
    )
    content_doc_id = cv_data["records"][0]["ContentDocumentId"]
    return cv_id, content_doc_id


def imgbb_upload_photo(file_bytes: bytes, filename: str) -> str | None:
    """Sube una foto a imgbb y retorna la URL pública directa."""
    b64 = base64.b64encode(file_bytes).decode("utf-8")
    try:
        resp = requests.post(
            "https://api.imgbb.com/1/upload",
            data={"key": IMGBB_API_KEY, "image": b64, "name": filename},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("success"):
            return data["data"]["url"]
    except Exception as e:
        syslog("WARNING", "imgbb upload falló", {"filename": filename, "error": str(e)})
    return None


def sf_link_file_to_record(sf_client, content_doc_id: str, record_id: str):
    """Vincula un ContentDocument a un registro de Salesforce.

    Idempotente: si el vínculo ya existe (reintento de un job que falló a mitad de
    camino), Salesforce responde DUPLICATE_VALUE y lo tratamos como éxito.
    """
    try:
        sf_client.ContentDocumentLink.create({
            "ContentDocumentId": content_doc_id,
            "LinkedEntityId": record_id,
            "ShareType": "I",
            "Visibility": "AllUsers",
        })
    except Exception as e:
        if "DUPLICATE_VALUE" in str(e).upper():
            return
        raise


# ===============================
# Procesamiento de subida (síncrono, idempotente por job_id)
# ===============================
def _fail(msg: str, permanent: bool = False) -> dict:
    """Un fallo `permanent` no se arregla reintentando (orden inexistente, datos mal).
    El resto es transitorio (SF caído, red) y el Service Worker debe volver a intentar."""
    return {"ok": False, "error": msg, "permanent": permanent}


def process_upload(job_id: str, order_numbers_raw: list[str], files_payload: list[dict]) -> dict:
    """Ejecuta el pipeline completo de una entrega. Retorna {ok, result} o {ok: False, error, permanent}."""
    syslog("INFO", f"[job:{job_id}] Iniciando upload", {
        "orders_raw": order_numbers_raw,
        "num_photos": len(files_payload)
    })

    try:
        sf_client = get_sf()
        if sf_client is None:
            msg = "Salesforce no está conectado"
            syslog("ERROR", f"[job:{job_id}] {msg}")
            return _fail(msg)

        seen = set()
        ops = []
        for raw in order_numbers_raw:
            op = normalizar_op(raw)
            if op not in seen:
                seen.add(op)
                ops.append(op)

        if not ops:
            msg = "No se recibieron órdenes válidas"
            syslog("ERROR", f"[job:{job_id}] {msg}")
            return _fail(msg, permanent=True)

        syslog("INFO", f"[job:{job_id}] Verificando órdenes en SF", {"ops": ops})

        orders_info = []
        client_name_ref = None

        for op in ops:
            info = sf_get_order_info(op)
            if not info:
                msg = f"Orden no encontrada en Salesforce: {op}"
                syslog("ERROR", f"[job:{job_id}] {msg}")
                return _fail(msg, permanent=True)

            client_name = (info.get("Nombre_del_cliente__c") or "").strip()
            if not client_name:
                msg = f"La orden {op} no tiene cliente asignado"
                syslog("ERROR", f"[job:{job_id}] {msg}", {"op": op, "sf_id": info.get("Id")})
                return _fail(msg, permanent=True)

            if client_name_ref is None:
                client_name_ref = client_name
            elif client_name != client_name_ref:
                msg = "Las órdenes no pertenecen al mismo cliente"
                syslog("ERROR", f"[job:{job_id}] {msg}", {
                    "op": op,
                    "cliente_op": client_name,
                    "cliente_ref": client_name_ref
                })
                return _fail(msg, permanent=True)

            orders_info.append({
                "op": op,
                "record_id": info["Id"],
                "name": info.get("Name"),
                "client_name": client_name,
            })

        syslog("INFO", f"[job:{job_id}] Órdenes verificadas OK", {"ops": [o["op"] for o in orders_info]})

        total_photos = len(files_payload)

        # Progreso persistido por foto: si un intento anterior murió a mitad de camino
        # (worker matado por timeout, red cortada), al reintentar no volvemos a subir
        # las fotos que ya están en Salesforce/imgbb. Sin esto cada reintento duplicaba
        # los archivos en la orden.
        progress = load_job_progress(job_id)
        done_photos = progress.setdefault("photos", {})

        # Cada foto se sube a Salesforce (interno) e imgbb (URL pública para clientes)
        public_urls = []
        for i, f in enumerate(files_payload, start=1):
            fname = f["filename"]
            prev = done_photos.get(fname) or {}
            content_doc_id = prev.get("cdid")
            public_url = prev.get("url")

            # Salesforce y imgbb se reanudan por separado: si una vez subió a SF pero
            # imgbb falló, en el reintento solo se repite imgbb.
            if content_doc_id:
                syslog("DEBUG", f"[job:{job_id}] Foto {i}/{total_photos} ya estaba en Salesforce, se reutiliza", {
                    "filename": fname, "content_doc_id": content_doc_id
                })
            else:
                syslog("DEBUG", f"[job:{job_id}] Subiendo foto {i}/{total_photos}", {
                    "filename": fname,
                    "mimetype": f["mimetype"],
                    "size_kb": round(len(f["bytes"]) / 1024, 1)
                })
                try:
                    _, content_doc_id = sf_upload_photo(sf_client, fname, f["bytes"])
                except Exception as e:
                    msg = f"Error subiendo foto '{fname}'"
                    syslog("ERROR", f"[job:{job_id}] {msg}", {"error": str(e)})
                    return _fail(msg)

            if not public_url:
                public_url = imgbb_upload_photo(f["bytes"], fname)
                if public_url:
                    syslog("DEBUG", f"[job:{job_id}] imgbb OK foto {i}", {"url": public_url})
                else:
                    syslog("WARNING", f"[job:{job_id}] imgbb falló foto {i}, sin URL pública")

            # Se guarda antes de vincular: si el vínculo falla, el reintento no vuelve a
            # subir el archivo (sf_link_file_to_record tolera duplicados).
            if prev.get("cdid") != content_doc_id or prev.get("url") != public_url:
                done_photos[fname] = {"cdid": content_doc_id, "url": public_url}
                save_job_progress(job_id, progress)

            try:
                for order in orders_info:
                    sf_link_file_to_record(sf_client, content_doc_id, order["record_id"])
            except Exception as e:
                msg = f"Error vinculando foto '{fname}' a la orden"
                syslog("ERROR", f"[job:{job_id}] {msg}", {"error": str(e)})
                return _fail(msg)

            if public_url:
                public_urls.append(public_url)

        syslog("INFO", f"[job:{job_id}] Todas las fotos subidas OK", {
            "total": total_photos, "con_url_publica": len(public_urls)
        })

        if not public_urls:
            # Sin URLs públicas el cliente no recibe nada por WhatsApp y el campo de SF
            # quedaría vacío: es un fallo, no un éxito silencioso.
            msg = "Ninguna foto obtuvo URL pública (imgbb no respondió)"
            syslog("ERROR", f"[job:{job_id}] {msg}", {"imgbb_key_configurada": bool(IMGBB_API_KEY)})
            notify_alert(
                "imgbb_caido",
                "imgbb no está generando los links",
                "Ninguna foto obtuvo URL pública. Sin links no se le puede mandar nada al "
                "cliente, así que las entregas están fallando. Revisar la cuota o la "
                f"IMGBB_API_KEY (configurada: {bool(IMGBB_API_KEY)}).",
            )
            return _fail(msg)

        results_per_order = []
        for order in orders_info:
            try:
                sf_active = get_sf()
                if not sf_active:
                    raise Exception("Salesforce no disponible al actualizar registro")

                links_html = "<br/>".join(
                    f'<a href="{url}">Foto {i}</a>'
                    for i, url in enumerate(public_urls, 1)
                )
                sf_active.Orden_Proveedor__c.update(order["record_id"], {
                    "Link_Guia_de_Entrega__c": links_html
                })
                results_per_order.append({
                    "order_id": order["record_id"],
                    "order_number": order["op"],
                    "links_html": links_html
                })
                syslog("INFO", f"[job:{job_id}] SF actualizado OK", {"op": order["op"]})
            except Exception as e:
                msg = f"Error actualizando Salesforce para {order['op']}"
                syslog("ERROR", f"[job:{job_id}] {msg}", {"error": str(e), "record_id": order["record_id"]})
                return _fail(msg)

        legacy_first = results_per_order[0]
        photos_url = "\n".join(public_urls)

        webhook_data = {
            "order_id": legacy_first["order_id"],
            "order_number": legacy_first["order_number"],
            "photos_url": photos_url,
            "uploaded_count": total_photos,
            "timestamp": datetime.now().isoformat(),
            "client_name": client_name_ref,
            "orders": results_per_order
        }

        # El webhook se dispara ANTES de guardar la entrega para poder registrar si
        # salió o no. Es el aviso al cliente: que falle no invalida la entrega (las
        # fotos ya están en Salesforce), pero tiene que quedar asentado y avisar, no
        # perderse en un WARNING como pasaba antes.
        webhook_ok = False
        webhook_error = ""
        if not MAKE_WEBHOOK_URL:
            webhook_error = "MAKE_WEBHOOK_URL no está configurada"
            syslog("ERROR", f"[job:{job_id}] {webhook_error}")
        else:
            try:
                resp = requests.post(MAKE_WEBHOOK_URL, json=webhook_data, timeout=15)
                resp.raise_for_status()
                webhook_ok = True
                syslog("INFO", f"[job:{job_id}] Make webhook OK", {
                    "status_code": resp.status_code,
                    "orders": [o["order_number"] for o in results_per_order]
                })
            except Exception as e:
                webhook_error = str(e)
                syslog("ERROR", f"[job:{job_id}] Make webhook falló", {"error": webhook_error})

        if not webhook_ok:
            notify_alert(
                "webhook_make_caido",
                "El aviso al cliente no se está enviando",
                f"La entrega de {client_name_ref} ({', '.join(o['order_number'] for o in results_per_order)}) "
                f"se guardó bien en Salesforce, pero el webhook de Make falló, así que el "
                f"cliente NO recibió su mensaje. Hay que reenviarlo a mano. Error: {webhook_error}",
            )

        for order in results_per_order:
            save_delivery(
                order["order_number"],
                client_name_ref or "",
                order.get("links_html", ""),
                photo_count=total_photos,
                webhook_ok=webhook_ok,
            )

        syslog("INFO", f"[job:{job_id}] Upload completado exitosamente", {
            "client": client_name_ref,
            "ops": [o["order_number"] for o in results_per_order],
            "photos": total_photos,
            "webhook_ok": webhook_ok,
        })

        return {
            "ok": True,
            "result": {
                "client_name": client_name_ref,
                "orders": results_per_order,
            },
        }

    except Exception as e:
        syslog("CRITICAL", f"[job:{job_id}] Excepción no controlada en process_upload", {"error": str(e)})
        return _fail(f"Error inesperado: {str(e)}")


# ===============================
# Base de datos — historial persistente
# ===============================
@contextmanager
def db():
    """Conexión SQLite que siempre se cierra, con espera ante bloqueos.

    Es un context manager y no una conexión suelta por un motivo concreto: antes cada
    función abría la conexión y la cerraba al final, así que cualquier excepción en el
    medio la dejaba abierta con una transacción de escritura viva. Esa conexión huérfana
    mantenía el lock y el resto de la app recibía "database is locked" hasta que el
    recolector de basura la liberara. El caso más grave era create_upload_job: el
    IntegrityError del job_id repetido —que ocurre en CADA reintento de Background
    Sync— se capturaba sin cerrar nada.

    Hace commit solo si el bloque terminó bien; si hubo excepción, rollback implícito.
    """
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 15000")
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_column(conn, table: str, column: str, decl: str):
    """ALTER TABLE ADD COLUMN idempotente (SQLite no soporta IF NOT EXISTS aquí)."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        syslog("INFO", "Columna agregada a la base de datos", {"table": table, "column": column})


def init_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with db() as conn:
        # WAL: permite leer (historial, supervisor) mientras otra request escribe.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS deliveries (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date_key    TEXT NOT NULL,
            day_label   TEXT NOT NULL,
            time_ec     TEXT NOT NULL,
            op          TEXT NOT NULL,
            client      TEXT NOT NULL,
            photo_count INTEGER DEFAULT 0,
            links_html  TEXT DEFAULT ''
        )
    """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_date_key ON deliveries(date_key)")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS upload_jobs (
            job_id        TEXT PRIMARY KEY,
            status        TEXT NOT NULL DEFAULT 'processing',
            order_numbers TEXT NOT NULL,
            client_name   TEXT,
            photo_count   INTEGER DEFAULT 0,
            result_json   TEXT,
            error_msg     TEXT,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        )
    """)
        _ensure_column(conn, "upload_jobs", "progress_json", "TEXT")
        # webhook_ok: 1 si el cliente recibió su aviso, 0 si Make falló. Antes esto no
        # se guardaba en ningún lado y una caída de Make era invisible.
        _ensure_column(conn, "deliveries", "webhook_ok", "INTEGER DEFAULT 1")
        # created_at: timestamp completo. date_key + time_ec no permiten calcular
        # "cuánto hace que no entra una entrega" sin parsear a mano.
        _ensure_column(conn, "deliveries", "created_at", "TEXT")


def create_upload_job(job_id: str, order_numbers: list[str], photo_count: int) -> bool:
    """Inserta un job nuevo. Retorna False si el job_id ya existía (reintento)."""
    now = ahora_ec_iso()
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO upload_jobs (job_id, status, order_numbers, photo_count, created_at, updated_at) "
                "VALUES (?, 'processing', ?, ?, ?, ?)",
                (job_id, json.dumps(order_numbers), photo_count, now, now),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def get_upload_job(job_id: str) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM upload_jobs WHERE job_id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def load_job_progress(job_id: str) -> dict:
    """Progreso parcial de un job: qué fotos ya llegaron a Salesforce/imgbb."""
    row = get_upload_job(job_id)
    if not row or not row.get("progress_json"):
        return {}
    try:
        return json.loads(row["progress_json"]) or {}
    except Exception:
        return {}


def save_job_progress(job_id: str, progress: dict):
    try:
        with db() as conn:
            conn.execute(
                "UPDATE upload_jobs SET progress_json = ?, updated_at = ? WHERE job_id = ?",
                (json.dumps(progress), ahora_ec_iso(), job_id),
            )
    except Exception as e:
        # Perder el progreso solo cuesta re-subir fotos en un reintento; no aborta la entrega.
        syslog("WARNING", "No se pudo guardar el progreso del job", {"job_id": job_id, "error": str(e)})


def mark_job_processing(job_id: str):
    """Reabre un job en estado `error` para reprocesarlo."""
    with db() as conn:
        conn.execute(
            "UPDATE upload_jobs SET status = 'processing', error_msg = NULL, updated_at = ? WHERE job_id = ?",
            (ahora_ec_iso(), job_id),
        )


# Un job que lleva más de esto en 'processing' es un zombie: el worker que lo tomó
# murió (timeout de gunicorn, deploy, crash) y nadie va a terminarlo. Sin esta
# limpieza el job quedaba trabado para siempre y el Service Worker recibía 409
# en cada reintento, sin salir nunca del bucle.
_JOB_STALE_SECS = int(os.environ.get("JOB_STALE_SECS", "600"))


def reclaim_stale_jobs() -> int:
    cutoff = time.time() - _JOB_STALE_SECS
    reclaimed = 0
    try:
        with db() as conn:
            rows = conn.execute("SELECT job_id, updated_at FROM upload_jobs WHERE status = 'processing'").fetchall()
            stale = []
            for row in rows:
                try:
                    ts = datetime.fromisoformat(row["updated_at"]).timestamp()
                except Exception:
                    ts = 0
                if ts < cutoff:
                    stale.append(row["job_id"])
            for job_id in stale:
                conn.execute(
                    "UPDATE upload_jobs SET status = 'error', error_msg = ?, updated_at = ? WHERE job_id = ?",
                    ("Interrumpido: el servidor se reinició durante la subida",
                     ahora_ec_iso(), job_id),
                )
                reclaimed += 1
    except Exception as e:
        syslog("WARNING", "No se pudo liberar jobs zombie", {"error": str(e)})
        return 0

    if reclaimed:
        syslog("WARNING", "Jobs zombie liberados para reintento", {"cantidad": reclaimed})
    return reclaimed


def finish_upload_job(job_id: str, ok: bool, client_name: str = None, result: dict | None = None, error: str | None = None):
    now = ahora_ec_iso()
    with db() as conn:
        conn.execute(
            "UPDATE upload_jobs SET status = ?, client_name = ?, result_json = ?, error_msg = ?, updated_at = ? "
            "WHERE job_id = ?",
            (
                "done" if ok else "error",
                client_name,
                json.dumps(result) if result is not None else None,
                error,
                now,
                job_id,
            ),
        )


def save_delivery(op: str, client: str, links_html: str, photo_count: int, now_ec=None,
                  webhook_ok: bool = True):
    if now_ec is None:
        now_ec = datetime.now(_ECUADOR)
    date_key  = now_ec.strftime("%Y-%m-%d")
    day_label = (
        f"{_DIAS_FULL[now_ec.weekday()]}, {now_ec.day} "
        f"de {_MESES_FULL[now_ec.month - 1]} de {now_ec.year}"
    )
    time_ec = now_ec.strftime("%H:%M")
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO deliveries "
                "(date_key, day_label, time_ec, op, client, photo_count, links_html, webhook_ok, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (date_key, day_label, time_ec, op, client, photo_count, links_html,
                 1 if webhook_ok else 0, now_ec.isoformat()),
            )
    except Exception as e:
        syslog("ERROR", "Error guardando entrega en DB", {"op": op, "error": str(e)})
        notify_alert(
            "db_escritura_falla",
            "No se puede escribir en la base de datos",
            f"La entrega {op} de {client} se subió a Salesforce pero no quedó registrada "
            f"en el historial. Error: {e}",
        )


def get_deliveries_for_date(date_key: str) -> list[dict]:
    """Entregas de un día concreto, la más reciente primero."""
    with db() as conn:
        rows = conn.execute(
            "SELECT time_ec, op, client, photo_count, links_html FROM deliveries "
            "WHERE date_key = ? ORDER BY id DESC",
            (date_key,),
        ).fetchall()
    return [
        {
            "time": r["time_ec"],
            "op": r["op"],
            "client": r["client"],
            "photo_count": r["photo_count"],
            "links_html": r["links_html"],
        }
        for r in rows
    ]


try:
    init_db()
    syslog("INFO", "Base de datos inicializada", {"path": DB_PATH})
except Exception as e:
    syslog("CRITICAL", f"No se pudo inicializar la base de datos: {e}", {"path": DB_PATH})


# ===============================
# Routes — Aplicación principal
# ===============================
@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/sw.js")
def service_worker():
    # Servido en la raíz (no bajo /static/) para que el scope del Service Worker cubra toda la app
    return send_from_directory("static/js", "sw.js", mimetype="application/javascript")


@app.route("/manifest.webmanifest")
def manifest():
    # También en la raíz: así el scope de la PWA es "/" sin ambigüedades.
    return send_from_directory("static", "manifest.json", mimetype="application/manifest+json")


@app.route("/get_order_info/<number>", methods=["GET"])
def get_order_info(number):
    sf_client = get_sf()
    if sf_client is None:
        syslog("ERROR", "get_order_info llamado pero Salesforce no conectado")
        return jsonify({"success": False, "error": "Salesforce no conectado"})

    op = normalizar_op(number)
    try:
        info = sf_get_order_info(op, sf_client=sf_client)
        if not info:
            syslog("WARNING", "get_order_info: orden no encontrada", {"op": op})
            return jsonify({"success": False})

        return jsonify({
            "success": True,
            "name": info.get("Name"),
            "op": op,
            "client_name": (info.get("Nombre_del_cliente__c") or "").strip(),
            "has_delivery_url": bool(info.get("Link_Guia_de_Entrega__c")),
        })
    except Exception as e:
        syslog("ERROR", "get_order_info: excepción", {"op": op, "error": str(e)})
        return jsonify({"success": False})


@app.route("/start_upload", methods=["POST"])
def start_upload():
    job_id = (request.form.get("job_id") or "").strip()
    if not job_id:
        syslog("WARNING", "start_upload: request sin job_id")
        return jsonify({"success": False, "error": "Falta job_id"}), 400

    order_numbers_json = (request.form.get("order_numbers_json") or "").strip()
    files = request.files.getlist("photos")

    order_numbers = []
    if order_numbers_json:
        try:
            order_numbers = json.loads(order_numbers_json)
            if not isinstance(order_numbers, list):
                order_numbers = []
        except Exception as e:
            syslog("WARNING", "start_upload: error parseando order_numbers_json", {"raw": order_numbers_json, "error": str(e)})
            order_numbers = []

    if not order_numbers:
        syslog("WARNING", "start_upload: request sin órdenes válidas", {"job_id": job_id})
        return jsonify({"success": False, "error": "Faltan órdenes proveedor"}), 400

    if not files:
        syslog("WARNING", "start_upload: no se recibieron fotos", {"job_id": job_id})
        return jsonify({"success": False, "error": "Debes seleccionar al menos 1 foto"}), 400

    payload = []
    for f in files:
        if not f or not f.filename:
            continue

        mimetype = (f.mimetype or "").lower()
        if not mimetype.startswith("image/"):
            syslog("WARNING", "start_upload: archivo no imagen rechazado", {
                "filename": f.filename, "mimetype": mimetype
            })
            return jsonify({"success": False, "error": f"Archivo no permitido (solo fotos): {f.filename}"}), 400

        b = f.read()
        if not b:
            syslog("WARNING", "start_upload: archivo vacío rechazado", {"filename": f.filename})
            return jsonify({"success": False, "error": f"Archivo vacío: {f.filename}"}), 400

        payload.append({"filename": f.filename, "mimetype": mimetype, "bytes": b})

    if not payload:
        syslog("WARNING", "start_upload: payload vacío tras validación")
        return jsonify({"success": False, "error": "No se recibieron fotos válidas"}), 400

    # Antes de decidir si este job está "en proceso", liberar los que quedaron
    # colgados por un worker muerto.
    reclaim_stale_jobs()

    # Idempotencia: si este job_id ya se procesó (reintento de Background Sync), no repetir el trabajo
    is_new = create_upload_job(job_id, order_numbers, len(payload))
    if not is_new:
        existing = get_upload_job(job_id)
        if not existing:
            return jsonify({"success": False, "error": "Estado de job inconsistente"}), 503
        if existing["status"] == "done":
            syslog("INFO", "start_upload: job ya procesado, devolviendo resultado guardado", {"job_id": job_id})
            try:
                saved = json.loads(existing["result_json"]) if existing.get("result_json") else None
            except Exception:
                saved = None
            return jsonify({"success": True, "job_id": job_id, "result": saved})
        if existing["status"] == "processing":
            syslog("WARNING", "start_upload: job ya en procesamiento", {"job_id": job_id})
            return jsonify({"success": False, "error": "El job ya se está procesando"}), 409
        # status == "error": se permite reprocesar, reutilizando el progreso ya guardado
        mark_job_processing(job_id)

    sf_client = get_sf()
    if sf_client is None:
        syslog("ERROR", "start_upload llamado pero Salesforce no conectado", {"job_id": job_id})
        finish_upload_job(job_id, False, error="Salesforce no conectado")
        # 503: transitorio, el Service Worker debe reintentar.
        return jsonify({"success": False, "error": "Salesforce no conectado"}), 503

    syslog("INFO", "start_upload: procesando job", {
        "job_id": job_id,
        "orders": order_numbers,
        "num_photos": len(payload)
    })

    outcome = process_upload(job_id, order_numbers, payload)

    if outcome["ok"]:
        finish_upload_job(job_id, True, client_name=outcome["result"].get("client_name"), result=outcome["result"])
        return jsonify({"success": True, "job_id": job_id, "result": outcome["result"]})

    finish_upload_job(job_id, False, error=outcome.get("error"))
    # 422 = el job nunca va a funcionar (orden inexistente, clientes distintos): que el
    # Service Worker deje de reintentar y lo muestre como error al repartidor.
    # 503 = transitorio: reintentar cuando haya señal.
    status_code = 422 if outcome.get("permanent") else 503
    return jsonify({"success": False, "job_id": job_id, "error": outcome.get("error")}), status_code


# ===============================
# Routes — Salud del servicio
# ===============================
# Minutos sin ninguna entrega a partir de los cuales el servicio se considera sospechoso.
# 0 = desactivado, que es el default: en un día flojo puede no haber entregas por horas
# y no significa que algo esté roto. Conviene activarlo solo si conocés tu ritmo real.
ALERTA_SIN_ENTREGAS_MIN = int(os.environ.get("ALERTA_SIN_ENTREGAS_MIN", "0"))


def _resumen_salud() -> tuple[dict, bool]:
    """Arma el estado del servicio. Retorna (detalle, todo_ok)."""
    checks = {}
    ok = True

    # Base de datos: si no se puede escribir, no hay historial ni idempotencia de jobs.
    try:
        with db() as conn:
            conn.execute("SELECT 1 FROM deliveries LIMIT 1").fetchone()
        checks["db"] = "ok"
    except Exception as e:
        checks["db"] = f"error: {e}"
        ok = False

    # Salesforce: se reporta el último estado conocido en vez de forzar un login, para
    # que un monitor pegándole cada 5 minutos no dispare un login cada vez (ni se quede
    # colgado esperando el timeout cuando SF está caído).
    if _sf is not None and time.time() - _sf_last_ok < _SF_REVALIDATE_SECS * 2:
        checks["salesforce"] = "ok"
    elif _sf_last_error:
        checks["salesforce"] = f"error: {_sf_last_error}"
        ok = False
    else:
        checks["salesforce"] = "sin_usar"  # todavía nadie buscó una orden

    # Configuración: si falta una credencial, las entregas van a fallar.
    faltantes = [nombre for nombre, valor in (
        ("SF_USER", SF_USER), ("SF_PASS", SF_PASS), ("SF_SECURITY_TOKEN", SF_SECURITY_TOKEN),
        ("IMGBB_API_KEY", IMGBB_API_KEY), ("MAKE_WEBHOOK_URL", MAKE_WEBHOOK_URL),
    ) if not valor]
    checks["config"] = "ok" if not faltantes else f"faltan: {', '.join(faltantes)}"
    if faltantes:
        ok = False

    checks["alertas"] = "ok" if ALERT_WEBHOOK_URL else "sin_configurar"

    # Fallas recientes: lo que ya pasó y nadie miró.
    corte = (datetime.now(_ECUADOR) - timedelta(hours=24)).isoformat()
    try:
        with db() as conn:
            checks["jobs_fallidos_24h"] = conn.execute(
                "SELECT COUNT(*) FROM upload_jobs WHERE status = 'error' AND updated_at > ?",
                (corte,),
            ).fetchone()[0]
            checks["avisos_no_enviados_24h"] = conn.execute(
                "SELECT COUNT(*) FROM deliveries WHERE webhook_ok = 0 AND created_at > ?",
                (corte,),
            ).fetchone()[0]
            fila = conn.execute(
                "SELECT created_at FROM deliveries WHERE created_at IS NOT NULL "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        checks["ultima_entrega"] = fila["created_at"] if fila else None
    except Exception as e:
        checks["consultas"] = f"error: {e}"
        ok = False
        return checks, ok

    if checks["avisos_no_enviados_24h"]:
        ok = False

    if ALERTA_SIN_ENTREGAS_MIN and checks["ultima_entrega"]:
        try:
            hace = (datetime.now(_ECUADOR) - datetime.fromisoformat(checks["ultima_entrega"]))
            checks["minutos_sin_entregas"] = int(hace.total_seconds() // 60)
            if checks["minutos_sin_entregas"] > ALERTA_SIN_ENTREGAS_MIN:
                ok = False
        except Exception:
            pass

    return checks, ok


@app.route("/health")
def health():
    """Estado del servicio, para que lo consulte un monitor externo.

    Sin token a propósito: los servicios de uptime gratuitos no mandan headers, y acá no
    se expone nada sensible (ni credenciales ni datos de clientes).

    Devuelve 200 si está sano y 503 si no, que es lo único que la mayoría de los
    monitores sabe interpretar. Un monitor externo es imprescindible: una app caída no
    puede avisar de su propia caída.
    """
    checks, ok = _resumen_salud()
    return jsonify({"ok": ok, "checks": checks}), (200 if ok else 503)


@app.route("/health/probar-alerta")
@require_admin
def probar_alerta():
    """Dispara una alerta de prueba para verificar que el canal de avisos funciona."""
    if not ALERT_WEBHOOK_URL:
        return jsonify({"ok": False, "error": "ALERT_WEBHOOK_URL no está configurada"}), 400
    enviada = notify_alert(
        "prueba_manual",
        "Prueba del canal de alertas",
        "Si estás leyendo esto, los avisos del sistema de guías funcionan.",
        forzar=True,
    )
    return jsonify({"ok": enviada})


@app.errorhandler(413)
def too_large(_e):
    return jsonify({
        "success": False,
        "error": f"Las fotos superan el límite de {MAX_UPLOAD_MB} MB por subida"
    }), 413


# ===============================
# Routes — Supervisor
# ===============================
@app.route("/registro")
@app.route("/registro/<clave>")
def supervisor_view(clave=None):
    es_admin = _secret_ok(clave, ADMIN_KEY)
    return render_template("supervisor.html", admin=es_admin, admin_key=(clave if es_admin else ""))


@app.route("/api/history")
def api_history():
    """Entregas de HOY (hora Ecuador), leídas de la base de datos.

    Antes esto devolvía una lista en memoria: se vaciaba en cada reinicio y cada worker
    de gunicorn tenía la suya, así que el supervisor veía datos distintos según quién
    respondiera. Además incluía días anteriores, por lo que un cliente entregado ayer
    aparecía como entregado hoy.

    Se devuelve `date_key` para que el cliente use el día del servidor y no el del
    teléfono (evita desfases de zona horaria en el checklist).
    """
    today = hoy_ec()
    try:
        deliveries = get_deliveries_for_date(today)
    except Exception as e:
        syslog("ERROR", "Error leyendo entregas de hoy", {"error": str(e)})
        return jsonify({"date_key": today, "deliveries": []}), 503
    return jsonify({"date_key": today, "deliveries": deliveries})


# ===============================
# Routes — Admin / Diagnóstico
# ===============================
@app.route("/admin/logs")
@require_admin
def admin_logs():
    # El HTML vive en templates/admin_logs.html. Antes eran 130 líneas de HTML+CSS+JS
    # embebidas en un string de Python: sin resaltado de sintaxis y sin poder editarlas
    # como lo que son.
    return render_template("admin_logs.html")


@app.route("/admin/logs/json")
@require_admin
def admin_logs_json():
    """Logs posteriores a `since` (número de secuencia). El panel hace polling con esto.

    Antes había además un endpoint SSE con `while True: sleep(0.5)`. Con workers sync de
    gunicorn ese bucle ocupaba un worker completo mientras el panel estuviera abierto —
    con 1 o 2 workers la app se colgaba para los repartidores. El polling no puede
    bloquear un worker, así que el stream se eliminó.
    """
    try:
        since = int(request.args.get("since", "0"))
    except ValueError:
        since = 0
    with _log_lock:
        data = [e for e in _log_buffer if e["seq"] > since]
    return jsonify(data)


@app.route("/admin/status")
@require_admin
def admin_status():
    with _log_lock:
        total_logs = len(_log_buffer)
        errors = sum(1 for l in _log_buffer if l["level"] in ("ERROR", "CRITICAL"))

    with db() as conn:
        active_jobs = conn.execute("SELECT COUNT(*) FROM upload_jobs WHERE status = 'processing'").fetchone()[0]
        total_jobs = conn.execute("SELECT COUNT(*) FROM upload_jobs").fetchone()[0]

    return jsonify({
        "timestamp": datetime.now().isoformat(),
        "salesforce_connected": get_sf() is not None,
        "logs_in_buffer": total_logs,
        "errors_in_buffer": errors,
        "active_jobs": active_jobs,
        "total_jobs_session": total_jobs,
    })


# ===============================
# Routes — Historial persistente
# ===============================
@app.route("/historial")
@app.route("/historial/<clave>")
def historial_view(clave=None):
    es_admin = _secret_ok(clave, ADMIN_KEY)
    return render_template("historial.html", admin=es_admin, admin_key=(clave if es_admin else ""))


@app.route("/api/historial/borrar", methods=["POST"])
@require_admin_key
def api_historial_borrar():
    solo_hoy = request.args.get("solo_hoy") == "1"
    try:
        with db() as conn:
            if solo_hoy:
                deleted = conn.execute("DELETE FROM deliveries WHERE date_key = ?", (hoy_ec(),)).rowcount
            else:
                deleted = conn.execute("DELETE FROM deliveries").rowcount
    except Exception as e:
        syslog("ERROR", "Error borrando historial", {"error": str(e)})
        return jsonify({"ok": False, "error": str(e)}), 500

    syslog("WARNING", "Historial borrado manualmente", {"solo_hoy": solo_hoy, "filas": deleted})
    return jsonify({"ok": True, "deleted": deleted})


@app.route("/api/historial/data")
def api_historial_data():
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT date_key, day_label, time_ec, op, client, photo_count, links_html, webhook_ok "
                "FROM deliveries ORDER BY date_key DESC, id DESC"
            ).fetchall()
    except Exception as e:
        syslog("ERROR", "Error leyendo historial DB", {"error": str(e)})
        return jsonify([])

    days = {}
    order_keys = []
    for row in rows:
        dk = row["date_key"]
        if dk not in days:
            days[dk] = {"day_label": row["day_label"], "deliveries": []}
            order_keys.append(dk)
        days[dk]["deliveries"].append({
            "time": row["time_ec"],
            "op": row["op"],
            "client": row["client"],
            "photo_count": row["photo_count"],
            "links_html": row["links_html"],
            # Las filas viejas (previas a esta columna) quedan en 1 por el DEFAULT.
            "webhook_ok": bool(row["webhook_ok"]),
        })
    return jsonify([days[k] for k in order_keys])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
