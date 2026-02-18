import os
import json
import uuid
import time
import threading
import re
from datetime import datetime
from io import BytesIO

import requests
from flask import Flask, render_template, request, jsonify, Response
from simple_salesforce import Salesforce

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload


# ===============================
# Flask App
# ===============================
app = Flask(__name__)
app.secret_key = "clave-camara-china"


# ===============================
# Salesforce
# ===============================
SF_USER = os.environ.get("SF_USER", "kdelacruz@camarachina.com")
SF_PASS = os.environ.get("SF_PASS", "Camara1234")
SF_SECURITY_TOKEN = os.environ.get("SF_SECURITY_TOKEN", "iCbyXWW5eZn0XUzx3PyZAX3cF")

MAKE_WEBHOOK_URL = "https://hook.us2.make.com/ilh879hn49xq3dxxhbihguy2x9vtcjx1"

sf = None
try:
    sf = Salesforce(username=SF_USER, password=SF_PASS, security_token=SF_SECURITY_TOKEN)
except Exception as e:
    print(f"Error conectando a Salesforce: {e}")
    sf = None


# ===============================
# Google Drive (Service Account)
# ===============================
SCOPES = ["https://www.googleapis.com/auth/drive"]
CREDENTIALS_FILE = "credentials.json"
USE_SHARED_DRIVE = True
_drive_service = None


def get_drive_service():
    global _drive_service
    if _drive_service:
        return _drive_service

    creds = service_account.Credentials.from_service_account_file(
        CREDENTIALS_FILE,
        scopes=SCOPES
    )
    _drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _drive_service


# ===============================
# Helpers
# ===============================
def normalizar_op(numero: str) -> str:
    numero = (numero or "").strip()
    return f"OP-00{numero}"


def extract_drive_folder_id(url_or_id: str) -> str | None:
    s = (url_or_id or "").strip()
    if not s:
        return None

    # ID directo
    if "http" not in s.lower() and "drive.google.com" not in s.lower():
        if len(s) >= 10 and re.fullmatch(r"[A-Za-z0-9_-]+", s):
            return s
        return None

    m = re.search(r"/folders/([A-Za-z0-9_-]+)", s)
    if m:
        return m.group(1)

    return None


def ensure_drive_folder(service, folder_name: str, parent_id: str):
    safe_name = folder_name.replace("'", "\\'")
    q = (
        "mimeType='application/vnd.google-apps.folder' and "
        f"name='{safe_name}' and "
        f"'{parent_id}' in parents and "
        "trashed=false"
    )

    list_kwargs = {
        "q": q,
        "fields": "files(id,name,webViewLink)",
    }

    if USE_SHARED_DRIVE:
        list_kwargs.update({
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
        })

    res = service.files().list(**list_kwargs).execute()
    files = res.get("files", [])
    if files:
        f0 = files[0]
        link = f0.get("webViewLink") or f"https://drive.google.com/drive/folders/{f0['id']}"
        return f0["id"], link

    metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }

    create_kwargs = {
        "body": metadata,
        "fields": "id,webViewLink",
    }

    if USE_SHARED_DRIVE:
        create_kwargs["supportsAllDrives"] = True

    folder = service.files().create(**create_kwargs).execute()
    link = folder.get("webViewLink") or f"https://drive.google.com/drive/folders/{folder['id']}"
    return folder["id"], link


def make_public_read(service, file_id: str):
    perm_body = {"type": "anyone", "role": "reader"}
    perm_kwargs = {"fileId": file_id, "body": perm_body}
    if USE_SHARED_DRIVE:
        perm_kwargs["supportsAllDrives"] = True
    service.permissions().create(**perm_kwargs).execute()


def sf_get_order_info(op_completa: str) -> dict | None:
    """
    Devuelve info mínima de la OP:
    - Id
    - Name
    - Nombre_del_cliente__c
    - Enlace_de_Fotos__c
    """
    query = (
        "SELECT Id, Name, Nombre_del_cliente__c, Enlace_de_Fotos__c "
        "FROM Orden_Proveedor__c "
        f"WHERE Orden_Proveedor_Nro__c = '{op_completa}'"
    )
    result = sf.query_all(query)
    if result.get("totalSize", 0) == 0:
        return None
    return result["records"][0]


# ===============================
# Progreso SSE
# ===============================
jobs = {}
jobs_lock = threading.Lock()


def push_event(job_id: str, msg: str, event: str = "message"):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]["events"].append({"event": event, "msg": msg, "ts": time.time()})


def mark_done(job_id: str, ok: bool, result: dict | None = None):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]["done"] = True
            jobs[job_id]["ok"] = ok
            jobs[job_id]["result"] = result or {}


def worker_upload(job_id: str, order_numbers_raw: list[str], files_payload: list[dict]):
    try:
        if sf is None:
            push_event(job_id, "⚠️ Salesforce no está conectado.", "error")
            mark_done(job_id, False)
            return

        service = get_drive_service()

        # Normalizar y deduplicar manteniendo orden
        seen = set()
        ops = []
        for raw in order_numbers_raw:
            op = normalizar_op(raw)
            if op not in seen:
                seen.add(op)
                ops.append(op)

        if not ops:
            push_event(job_id, "❌ No se recibieron órdenes válidas.", "error")
            mark_done(job_id, False)
            return

        push_event(job_id, f"🔎 Verificando {len(ops)} orden(es) en Salesforce...")

        # 1) Cargar info de todas las OP y validar cliente igual
        orders_info = []
        client_name_ref = None

        for op in ops:
            info = sf_get_order_info(op)
            if not info:
                push_event(job_id, f"❌ Orden no encontrada: {op}", "error")
                mark_done(job_id, False)
                return

            client_name = (info.get("Nombre_del_cliente__c") or "").strip()
            if not client_name:
                push_event(job_id, f"❌ La orden {op} no tiene Nombre_del_cliente__c.", "error")
                mark_done(job_id, False)
                return

            if client_name_ref is None:
                client_name_ref = client_name
            elif client_name != client_name_ref:
                push_event(job_id, "❌ Las órdenes no pertenecen al mismo cliente.", "error")
                mark_done(job_id, False)
                return

            base_url = info.get("Enlace_de_Fotos__c")
            base_id = extract_drive_folder_id(base_url)
            if not base_id:
                push_event(job_id, f"❌ {op} no tiene Enlace_de_Fotos__c válido (URL/ID de carpeta).", "error")
                mark_done(job_id, False)
                return

            orders_info.append({
                "op": op,
                "record_id": info["Id"],
                "name": info.get("Name"),
                "client_name": client_name,
                "base_folder_id": base_id
            })

        push_event(job_id, f"✅ Órdenes verificadas ({len(orders_info)}).")

        # 2) Por cada OP: crear ENTREGA y subir fotos
        total_photos = len(files_payload)
        results_per_order = []

        # Guardamos un contador global para el UI (solo fotos), pero internamente subimos a todas
        # UI verá "Subiendo (i/total)" por cada foto (una vez), aunque bajo el capó se sube a N órdenes.
        for i, f in enumerate(files_payload, start=1):
            push_event(job_id, f"⬆️ Subiendo {i}/{total_photos}: {f['filename']} ...")

            for order in orders_info:
                entrega_folder_id, entrega_folder_link = ensure_drive_folder(service, "ENTREGA", order["base_folder_id"])

                bio = BytesIO(f["bytes"])
                media = MediaIoBaseUpload(bio, mimetype=f["mimetype"], resumable=False)

                metadata = {
                    "name": f"{order['op']}_{i:02d}_{f['filename']}",
                    "parents": [entrega_folder_id]
                }

                create_file_kwargs = {
                    "body": metadata,
                    "media_body": media,
                    "fields": "id, webViewLink",
                }
                if USE_SHARED_DRIVE:
                    create_file_kwargs["supportsAllDrives"] = True

                created = service.files().create(**create_file_kwargs).execute()
                file_id = created["id"]

                try:
                    make_public_read(service, file_id)
                except Exception:
                    # warning técnico, UI lo ignora
                    push_event(job_id, "⚠️ No se pudo hacer público el archivo (Drive policy).", "warn")

                # Guardamos link de archivo si algún día lo necesitas
                _ = created.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"

                # guardamos ENTREGA link por orden (para update y para Make)
                order.setdefault("entrega_folder_link", entrega_folder_link)

        # 3) Actualizar Salesforce en todas las OP
        push_event(job_id, "🧾 Guardando link de carpeta en Salesforce...")

        for order in orders_info:
            sf.Orden_Proveedor__c.update(order["record_id"], {
                "Guia_de_Entrega_URL__c": order["entrega_folder_link"]
            })
            results_per_order.append({
                "order_id": order["record_id"],
                "order_number": order["op"],
                "drive_folder": order["entrega_folder_link"]
            })

        push_event(job_id, f"✅ Guardado en Salesforce ({len(orders_info)}).")

        # 4) Webhook Make: SOLO UNA VEZ
        push_event(job_id, "📡 Notificando a Make...")

        legacy_first = results_per_order[0]
        webhook_data = {
            # Legacy (para no romper tu Make actual)
            "order_id": legacy_first["order_id"],
            "order_number": legacy_first["order_number"],
            "drive_folder": legacy_first["drive_folder"],
            "uploaded_count": total_photos,
            "timestamp": datetime.now().isoformat(),

            # Nuevo (multi-OP)
            "client_name": client_name_ref,
            "orders": results_per_order
        }

        try:
            requests.post(MAKE_WEBHOOK_URL, json=webhook_data, timeout=10)
        except Exception:
            push_event(job_id, "⚠️ Make no respondió, pero ya se subió todo.", "warn")

        push_event(job_id, "🎉 Proceso completado.", "done")
        mark_done(job_id, True, {
            "client_name": client_name_ref,
            "orders": results_per_order,
            "folder_link": legacy_first["drive_folder"]
        })

    except Exception as e:
        push_event(job_id, f"⚠️ Error: {str(e)}", "error")
        mark_done(job_id, False)


# ===============================
# Routes
# ===============================
@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/get_order_info/<number>", methods=["GET"])
def get_order_info(number):
    """
    Devuelve info para validar en frontend:
    - Name
    - op (normalizada)
    - client_name (Nombre_del_cliente__c)
    """
    if sf is None:
        return jsonify({"success": False, "error": "Salesforce no conectado"})

    op = normalizar_op(number)
    try:
        info = sf_get_order_info(op)
        if not info:
            return jsonify({"success": False})

        return jsonify({
            "success": True,
            "name": info.get("Name"),
            "op": op,
            "client_name": (info.get("Nombre_del_cliente__c") or "").strip()
        })
    except Exception:
        return jsonify({"success": False})


@app.route("/start_upload", methods=["POST"])
def start_upload():
    """
    Recibe:
    - order_numbers_json: JSON array de strings (ej ["000123", "000124"])
      (fallback: order_number si viene una sola)
    - photos: imágenes
    """
    if sf is None:
        return jsonify({"success": False, "error": "Salesforce no conectado"}), 500

    order_numbers_json = (request.form.get("order_numbers_json") or "").strip()
    order_number_single = (request.form.get("order_number") or "").strip()

    files = request.files.getlist("photos")

    if not files:
        return jsonify({"success": False, "error": "Debes seleccionar al menos 1 foto"}), 400

    order_numbers = []
    if order_numbers_json:
        try:
            order_numbers = json.loads(order_numbers_json)
            if not isinstance(order_numbers, list):
                order_numbers = []
        except Exception:
            order_numbers = []

    if not order_numbers and order_number_single:
        order_numbers = [order_number_single]

    if not order_numbers:
        return jsonify({"success": False, "error": "Faltan órdenes proveedor"}), 400

    payload = []
    for f in files:
        if not f or not f.filename:
            continue

        mimetype = (f.mimetype or "").lower()
        if not mimetype.startswith("image/"):
            return jsonify({"success": False, "error": f"Archivo no permitido (solo fotos): {f.filename}"}), 400

        b = f.read()
        if not b:
            return jsonify({"success": False, "error": f"Archivo vacío: {f.filename}"}), 400

        payload.append({"filename": f.filename, "mimetype": mimetype, "bytes": b})

    if not payload:
        return jsonify({"success": False, "error": "No se recibieron fotos válidas"}), 400

    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {"events": [], "done": False, "ok": None, "result": {}}

    t = threading.Thread(target=worker_upload, args=(job_id, order_numbers, payload), daemon=True)
    t.start()

    return jsonify({"success": True, "job_id": job_id})


@app.route("/progress/<job_id>")
def progress(job_id):
    def event_stream():
        last_index = 0
        while True:
            with jobs_lock:
                job = jobs.get(job_id)
                if not job:
                    yield "event: error\ndata: Job no existe\n\n"
                    return
                events = job["events"]
                done = job["done"]

            while last_index < len(events):
                ev = events[last_index]
                last_index += 1
                yield f"event: {ev['event']}\ndata: {ev['msg']}\n\n"

            if done:
                with jobs_lock:
                    ok = jobs[job_id]["ok"]
                    result = jobs[job_id]["result"]
                yield f"event: final\ndata: {json.dumps({'ok': ok, 'result': result})}\n\n"
                return

            time.sleep(0.35)

    return Response(event_stream(), mimetype="text/event-stream")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
