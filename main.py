import os
import json
import uuid
import time
import threading
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
# Salesforce (como tu versión original)
# ===============================
SF_USER = os.environ.get("SF_USER", "kdelacruz@camarachina.com")
SF_PASS = os.environ.get("SF_PASS", "Camara1234")
SF_SECURITY_TOKEN = os.environ.get("SF_SECURITY_TOKEN", "iCbyXWW5eZn0XUzx3PyZAX3cF")

# Webhook Make
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

# Tu archivo local (debe existir junto a main.py)
CREDENTIALS_FILE = "credentials.json"

# ✅ TU carpeta raíz dentro de Shared Drive
GDRIVE_PARENT_FOLDER_ID = "0AB-eJ9d6VP-xUk9PVA"

# Shared Drive support
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
    """
    Convierte input '11139' o '0011139' en el formato exacto que usa tu SF:
    OP-00 + numero (sin espacios). Ej: OP-0011139
    """
    numero = (numero or "").strip()
    return f"OP-00{numero}"


def ensure_drive_folder(service, folder_name: str, parent_id: str):
    """
    Busca carpeta por nombre dentro del parent_id; si no existe la crea.
    Devuelve (folder_id, folder_link)
    """
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
    """
    Intenta poner permiso público (anyone reader).
    Si tu dominio bloquea "anyone", fallará, pero igual queda subido.
    """
    perm_body = {"type": "anyone", "role": "reader"}
    perm_kwargs = {"fileId": file_id, "body": perm_body}
    if USE_SHARED_DRIVE:
        perm_kwargs["supportsAllDrives"] = True
    service.permissions().create(**perm_kwargs).execute()


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


def worker_upload(job_id: str, order_number_raw: str, files_payload: list[dict]):
    try:
        if sf is None:
            push_event(job_id, "⚠️ Salesforce no está conectado.", "error")
            mark_done(job_id, False)
            return

        service = get_drive_service()

        op_completa = normalizar_op(order_number_raw)
        push_event(job_id, f"🔎 Buscando orden {op_completa} en Salesforce...")

        query = f"SELECT Id FROM Orden_Proveedor__c WHERE Orden_Proveedor_Nro__c = '{op_completa}'"
        result = sf.query_all(query)

        if result.get("totalSize", 0) == 0:
            push_event(job_id, "❌ Orden no encontrada en Salesforce.", "error")
            mark_done(job_id, False)
            return

        record_id = result["records"][0]["Id"]
        push_event(job_id, "✅ Orden encontrada.")
        push_event(job_id, "📁 Creando / buscando carpeta en Google Drive...")

        folder_name = f"{op_completa} - Guias"
        folder_id, folder_link = ensure_drive_folder(service, folder_name, GDRIVE_PARENT_FOLDER_ID)

        push_event(job_id, f"📁 Carpeta lista: {folder_name}")

        total = len(files_payload)
        uploaded_links = []

        for i, f in enumerate(files_payload, start=1):
            push_event(job_id, f"⬆️ Subiendo {i}/{total}: {f['filename']} ...")

            bio = BytesIO(f["bytes"])
            media = MediaIoBaseUpload(bio, mimetype=f["mimetype"], resumable=False)

            metadata = {
                "name": f"{op_completa}_{i:02d}_{f['filename']}",
                "parents": [folder_id]
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
                push_event(job_id, "⚠️ No se pudo hacer público el archivo (Drive policy).", "warn")

            link = created.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"
            uploaded_links.append(link)

            push_event(job_id, f"✅ Subida OK: {f['filename']}")

        # ===============================
        # Guardar en Salesforce (MISMO CAMPO QUE TENÍAS)
        # ===============================
        push_event(job_id, "🧾 Guardando link de carpeta en Salesforce...")

        sf.Orden_Proveedor__c.update(record_id, {
            "Guia_de_Entrega_URL__c": folder_link
        })

        push_event(job_id, "✅ Link guardado en Salesforce.")

        # Webhook Make (no tumba el flujo)
        push_event(job_id, "📡 Notificando a Make...")
        webhook_data = {
            "order_id": record_id,
            "order_number": op_completa,
            "drive_folder": folder_link,
            "uploaded_count": len(uploaded_links),
            "timestamp": datetime.now().isoformat()
        }
        try:
            requests.post(MAKE_WEBHOOK_URL, json=webhook_data, timeout=10)
        except Exception:
            push_event(job_id, "⚠️ Make no respondió, pero ya se subió todo.", "warn")

        push_event(job_id, "🎉 Proceso completado.", "done")
        mark_done(job_id, True, {"folder_link": folder_link, "uploaded_links": uploaded_links})

    except Exception as e:
        push_event(job_id, f"⚠️ Error: {str(e)}", "error")
        mark_done(job_id, False)


# ===============================
# Routes
# ===============================
@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/get_order_name/<number>", methods=["GET"])
def get_order_name(number):
    if sf is None:
        return jsonify({"success": False, "error": "Salesforce no conectado"})

    op_completa = normalizar_op(number)
    try:
        query = f"SELECT Name FROM Orden_Proveedor__c WHERE Orden_Proveedor_Nro__c = '{op_completa}'"
        result = sf.query_all(query)
        if result.get("totalSize", 0) > 0:
            return jsonify({"success": True, "name": result["records"][0]["Name"], "op": op_completa})
        return jsonify({"success": False})
    except Exception:
        return jsonify({"success": False})


@app.route("/start_upload", methods=["POST"])
def start_upload():
    order_number = (request.form.get("order_number") or "").strip()
    files = request.files.getlist("photos")

    if not order_number:
        return jsonify({"success": False, "error": "Falta order_number"}), 400
    if not files:
        return jsonify({"success": False, "error": "Debes seleccionar al menos 1 foto"}), 400

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

    t = threading.Thread(target=worker_upload, args=(job_id, order_number, payload), daemon=True)
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
