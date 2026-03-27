import os
import json
import uuid
import time
import threading
import re
import logging
from collections import deque
from datetime import datetime
from io import BytesIO
from functools import wraps

import requests
from flask import Flask, render_template, request, jsonify, Response, abort
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
# Sistema de Logs Interno
# ===============================
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "sistemas2024")  # Cambia esto o ponlo en variable de entorno

# Guardamos los últimos 500 logs en memoria (circular)
_log_buffer: deque = deque(maxlen=500)
_log_lock = threading.Lock()

LOG_LEVELS = {
    "DEBUG": "🔍",
    "INFO": "ℹ️",
    "WARNING": "⚠️",
    "ERROR": "❌",
    "CRITICAL": "🔥",
}


def syslog(level: str, message: str, context: dict = None):
    """Registra un evento interno en el buffer de logs."""
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "level": level.upper(),
        "icon": LOG_LEVELS.get(level.upper(), "•"),
        "msg": message,
        "ctx": context or {},
    }
    with _log_lock:
        _log_buffer.append(entry)

    # También imprime en consola del servidor (visible en Render logs)
    print(f"[{entry['ts']}] [{entry['level']}] {message}" + (f" | {context}" if context else ""))


def require_admin(f):
    """Decorator: protege rutas con token en query param ?token=..."""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.args.get("token", "")
        if token != ADMIN_TOKEN:
            abort(403)
        return f(*args, **kwargs)
    return decorated


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
    syslog("INFO", "Salesforce conectado correctamente", {"user": SF_USER})
except Exception as e:
    syslog("CRITICAL", f"Error conectando a Salesforce: {e}", {"user": SF_USER})
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

    try:
        creds = service_account.Credentials.from_service_account_file(
            CREDENTIALS_FILE,
            scopes=SCOPES
        )
        _drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
        syslog("INFO", "Google Drive service account conectado correctamente")
        return _drive_service
    except Exception as e:
        syslog("CRITICAL", f"Error conectando a Google Drive: {e}")
        raise


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
# Progreso SSE (jobs)
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
    syslog("INFO", f"[job:{job_id}] Iniciando upload", {
        "orders_raw": order_numbers_raw,
        "num_photos": len(files_payload)
    })

    try:
        if sf is None:
            msg = "Salesforce no está conectado"
            push_event(job_id, f"⚠️ {msg}", "error")
            syslog("ERROR", f"[job:{job_id}] {msg}")
            mark_done(job_id, False)
            return

        service = get_drive_service()

        seen = set()
        ops = []
        for raw in order_numbers_raw:
            op = normalizar_op(raw)
            if op not in seen:
                seen.add(op)
                ops.append(op)

        if not ops:
            msg = "No se recibieron órdenes válidas"
            push_event(job_id, f"❌ {msg}", "error")
            syslog("ERROR", f"[job:{job_id}] {msg}")
            mark_done(job_id, False)
            return

        push_event(job_id, f"🔎 Verificando {len(ops)} orden(es) en Salesforce...")
        syslog("INFO", f"[job:{job_id}] Verificando órdenes en SF", {"ops": ops})

        orders_info = []
        client_name_ref = None

        for op in ops:
            info = sf_get_order_info(op)
            if not info:
                msg = f"Orden no encontrada en Salesforce: {op}"
                push_event(job_id, f"❌ {msg}", "error")
                syslog("ERROR", f"[job:{job_id}] {msg}")
                mark_done(job_id, False)
                return

            client_name = (info.get("Nombre_del_cliente__c") or "").strip()
            if not client_name:
                msg = f"La orden {op} no tiene Nombre_del_cliente__c"
                push_event(job_id, f"❌ {msg}", "error")
                syslog("ERROR", f"[job:{job_id}] {msg}", {"op": op, "sf_id": info.get("Id")})
                mark_done(job_id, False)
                return

            if client_name_ref is None:
                client_name_ref = client_name
            elif client_name != client_name_ref:
                msg = "Las órdenes no pertenecen al mismo cliente"
                push_event(job_id, f"❌ {msg}", "error")
                syslog("ERROR", f"[job:{job_id}] {msg}", {
                    "op": op,
                    "cliente_op": client_name,
                    "cliente_ref": client_name_ref
                })
                mark_done(job_id, False)
                return

            base_url = info.get("Enlace_de_Fotos__c")
            base_id = extract_drive_folder_id(base_url)
            if not base_id:
                msg = f"{op} no tiene Enlace_de_Fotos__c válido"
                push_event(job_id, f"❌ {msg}", "error")
                syslog("ERROR", f"[job:{job_id}] {msg}", {"url_raw": base_url})
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
        syslog("INFO", f"[job:{job_id}] Órdenes verificadas OK", {"ops": [o["op"] for o in orders_info]})

        total_photos = len(files_payload)
        results_per_order = []

        for i, f in enumerate(files_payload, start=1):
            push_event(job_id, f"⬆️ Subiendo {i}/{total_photos}: {f['filename']} ...")
            syslog("DEBUG", f"[job:{job_id}] Subiendo foto {i}/{total_photos}", {
                "filename": f["filename"],
                "mimetype": f["mimetype"],
                "size_kb": round(len(f["bytes"]) / 1024, 1)
            })

            for order in orders_info:
                try:
                    entrega_folder_id, entrega_folder_link = ensure_drive_folder(
                        service, "ENTREGA", order["base_folder_id"]
                    )

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
                    except Exception as e:
                        push_event(job_id, "⚠️ No se pudo hacer público el archivo (Drive policy).", "warn")
                        syslog("WARNING", f"[job:{job_id}] make_public_read falló", {
                            "file_id": file_id,
                            "error": str(e)
                        })

                    order.setdefault("entrega_folder_link", entrega_folder_link)

                except Exception as e:
                    msg = f"Error subiendo foto '{f['filename']}' para {order['op']}"
                    push_event(job_id, f"❌ {msg}", "error")
                    syslog("ERROR", f"[job:{job_id}] {msg}", {"error": str(e)})
                    mark_done(job_id, False)
                    return

        syslog("INFO", f"[job:{job_id}] Todas las fotos subidas OK", {"total": total_photos})

        push_event(job_id, "🧾 Guardando link de carpeta en Salesforce...")

        for order in orders_info:
            try:
                sf.Orden_Proveedor__c.update(order["record_id"], {
                    "Guia_de_Entrega_URL__c": order["entrega_folder_link"]
                })
                results_per_order.append({
                    "order_id": order["record_id"],
                    "order_number": order["op"],
                    "drive_folder": order["entrega_folder_link"]
                })
                syslog("INFO", f"[job:{job_id}] SF actualizado OK", {"op": order["op"]})
            except Exception as e:
                msg = f"Error actualizando Salesforce para {order['op']}"
                push_event(job_id, f"❌ {msg}", "error")
                syslog("ERROR", f"[job:{job_id}] {msg}", {"error": str(e), "record_id": order["record_id"]})
                mark_done(job_id, False)
                return

        push_event(job_id, f"✅ Guardado en Salesforce ({len(orders_info)}).")

        push_event(job_id, "📡 Notificando a Make...")

        legacy_first = results_per_order[0]
        webhook_data = {
            "order_id": legacy_first["order_id"],
            "order_number": legacy_first["order_number"],
            "drive_folder": legacy_first["drive_folder"],
            "uploaded_count": total_photos,
            "timestamp": datetime.now().isoformat(),
            "client_name": client_name_ref,
            "orders": results_per_order
        }

        try:
            resp = requests.post(MAKE_WEBHOOK_URL, json=webhook_data, timeout=10)
            syslog("INFO", f"[job:{job_id}] Make webhook OK", {
                "status_code": resp.status_code,
                "orders": [o["order_number"] for o in results_per_order]
            })
        except Exception as e:
            push_event(job_id, "⚠️ Make no respondió, pero ya se subió todo.", "warn")
            syslog("WARNING", f"[job:{job_id}] Make webhook falló", {"error": str(e)})

        push_event(job_id, "🎉 Proceso completado.", "done")
        syslog("INFO", f"[job:{job_id}] Upload completado exitosamente", {
            "client": client_name_ref,
            "ops": [o["order_number"] for o in results_per_order],
            "photos": total_photos
        })

        mark_done(job_id, True, {
            "client_name": client_name_ref,
            "orders": results_per_order,
            "folder_link": legacy_first["drive_folder"]
        })

    except Exception as e:
        push_event(job_id, f"⚠️ Error inesperado: {str(e)}", "error")
        syslog("CRITICAL", f"[job:{job_id}] Excepción no controlada en worker_upload", {"error": str(e)})
        mark_done(job_id, False)


# ===============================
# Routes — Aplicación principal
# ===============================
@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/get_order_info/<number>", methods=["GET"])
def get_order_info(number):
    if sf is None:
        syslog("ERROR", "get_order_info llamado pero Salesforce no conectado")
        return jsonify({"success": False, "error": "Salesforce no conectado"})

    op = normalizar_op(number)
    try:
        info = sf_get_order_info(op)
        if not info:
            syslog("WARNING", "get_order_info: orden no encontrada", {"op": op})
            return jsonify({"success": False})

        return jsonify({
            "success": True,
            "name": info.get("Name"),
            "op": op,
            "client_name": (info.get("Nombre_del_cliente__c") or "").strip()
        })
    except Exception as e:
        syslog("ERROR", "get_order_info: excepción", {"op": op, "error": str(e)})
        return jsonify({"success": False})


@app.route("/start_upload", methods=["POST"])
def start_upload():
    if sf is None:
        syslog("ERROR", "start_upload llamado pero Salesforce no conectado")
        return jsonify({"success": False, "error": "Salesforce no conectado"}), 500

    order_numbers_json = (request.form.get("order_numbers_json") or "").strip()
    order_number_single = (request.form.get("order_number") or "").strip()
    files = request.files.getlist("photos")

    if not files:
        syslog("WARNING", "start_upload: no se recibieron fotos")
        return jsonify({"success": False, "error": "Debes seleccionar al menos 1 foto"}), 400

    order_numbers = []
    if order_numbers_json:
        try:
            order_numbers = json.loads(order_numbers_json)
            if not isinstance(order_numbers, list):
                order_numbers = []
        except Exception as e:
            syslog("WARNING", "start_upload: error parseando order_numbers_json", {"raw": order_numbers_json, "error": str(e)})
            order_numbers = []

    if not order_numbers and order_number_single:
        order_numbers = [order_number_single]

    if not order_numbers:
        syslog("WARNING", "start_upload: request sin órdenes válidas")
        return jsonify({"success": False, "error": "Faltan órdenes proveedor"}), 400

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

    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {"events": [], "done": False, "ok": None, "result": {}}

    syslog("INFO", "start_upload: job creado", {
        "job_id": job_id,
        "orders": order_numbers,
        "num_photos": len(payload)
    })

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


# ===============================
# Routes — Admin / Diagnóstico
# ===============================
@app.route("/admin/logs")
@require_admin
def admin_logs():
    """Panel HTML de logs. Acceso: /admin/logs?token=TU_TOKEN"""
    html = """<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Sistema de Logs — Admin</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: #0d1117; color: #c9d1d9; font-family: 'Courier New', monospace; font-size: 13px; }
    header { background: #161b22; border-bottom: 1px solid #30363d; padding: 14px 20px; display: flex; align-items: center; gap: 12px; }
    header h1 { font-size: 15px; color: #58a6ff; font-weight: bold; }
    header span { font-size: 11px; color: #8b949e; }
    #controls { padding: 10px 20px; background: #161b22; border-bottom: 1px solid #30363d; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
    button { padding: 5px 12px; border-radius: 6px; border: 1px solid #30363d; background: #21262d; color: #c9d1d9; cursor: pointer; font-size: 12px; }
    button:hover { background: #30363d; }
    button.active { border-color: #58a6ff; color: #58a6ff; }
    #filter { background: #0d1117; border: 1px solid #30363d; color: #c9d1d9; padding: 5px 10px; border-radius: 6px; font-size: 12px; width: 220px; }
    #log-container { padding: 16px 20px; overflow-y: auto; height: calc(100vh - 110px); }
    .log-entry { display: flex; gap: 10px; padding: 5px 8px; border-radius: 4px; margin-bottom: 2px; border-left: 3px solid transparent; }
    .log-entry:hover { background: #161b22; }
    .ts { color: #6e7681; min-width: 155px; }
    .icon { min-width: 24px; }
    .msg { flex: 1; word-break: break-all; }
    .ctx { color: #8b949e; font-size: 11px; margin-left: 4px; }
    .level-DEBUG { border-left-color: #8b949e; }
    .level-INFO { border-left-color: #58a6ff; }
    .level-WARNING { border-left-color: #d29922; color: #e3b341; }
    .level-ERROR { border-left-color: #f85149; color: #ff7b72; }
    .level-CRITICAL { border-left-color: #da3633; background: #2d1117; color: #ff7b72; font-weight: bold; }
    #status { position: fixed; bottom: 12px; right: 16px; font-size: 11px; color: #8b949e; }
    #status.live { color: #3fb950; }
    .empty { color: #6e7681; text-align: center; margin-top: 60px; }
  </style>
</head>
<body>
<header>
  <h1>🛠 Panel de Diagnóstico</h1>
  <span id="log-count">— entradas</span>
</header>
<div id="controls">
  <button onclick="setFilter('ALL')" class="active" id="btn-ALL">Todos</button>
  <button onclick="setFilter('DEBUG')" id="btn-DEBUG">🔍 Debug</button>
  <button onclick="setFilter('INFO')" id="btn-INFO">ℹ️ Info</button>
  <button onclick="setFilter('WARNING')" id="btn-WARNING">⚠️ Warning</button>
  <button onclick="setFilter('ERROR')" id="btn-ERROR">❌ Error</button>
  <button onclick="setFilter('CRITICAL')" id="btn-CRITICAL">🔥 Critical</button>
  <input id="filter" placeholder="Filtrar texto..." oninput="applyFilter()">
  <button onclick="clearDisplay()">Limpiar vista</button>
  <button onclick="refreshLogs()">⟳ Refrescar</button>
</div>
<div id="log-container"><p class="empty">Cargando logs...</p></div>
<div id="status">conectando...</div>

<script>
  let allLogs = [];
  let activeLevel = 'ALL';
  const token = new URLSearchParams(location.search).get('token');
  const container = document.getElementById('log-container');
  const statusEl = document.getElementById('status');

  function renderLogs() {
    const text = document.getElementById('filter').value.toLowerCase();
    const filtered = allLogs.filter(l => {
      const matchLevel = activeLevel === 'ALL' || l.level === activeLevel;
      const matchText = !text || l.msg.toLowerCase().includes(text) || JSON.stringify(l.ctx).toLowerCase().includes(text);
      return matchLevel && matchText;
    });

    document.getElementById('log-count').textContent = filtered.length + ' entradas';

    if (!filtered.length) {
      container.innerHTML = '<p class="empty">No hay logs con ese filtro.</p>';
      return;
    }

    const wasAtBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 60;

    container.innerHTML = filtered.map(l => {
      const ctx = Object.keys(l.ctx).length ? `<span class="ctx">${JSON.stringify(l.ctx)}</span>` : '';
      return `<div class="log-entry level-${l.level}">
        <span class="ts">${l.ts}</span>
        <span class="icon">${l.icon}</span>
        <span class="msg">${escHtml(l.msg)}${ctx}</span>
      </div>`;
    }).join('');

    if (wasAtBottom) container.scrollTop = container.scrollHeight;
  }

  function escHtml(s) {
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  function setFilter(level) {
    activeLevel = level;
    document.querySelectorAll('[id^=btn-]').forEach(b => b.classList.remove('active'));
    document.getElementById('btn-' + level).classList.add('active');
    renderLogs();
  }

  function applyFilter() { renderLogs(); }
  function clearDisplay() { allLogs = []; renderLogs(); }

  function refreshLogs() {
    fetch('/admin/logs/json?token=' + token)
      .then(r => r.json())
      .then(data => { allLogs = data; renderLogs(); });
  }

  // SSE para logs en tiempo real
  function connectSSE() {
    const es = new EventSource('/admin/logs/stream?token=' + token);
    es.onopen = () => { statusEl.textContent = '● En vivo'; statusEl.className = 'live'; };
    es.onmessage = (e) => {
      const entry = JSON.parse(e.data);
      allLogs.push(entry);
      renderLogs();
    };
    es.onerror = () => {
      statusEl.textContent = '⚠ Desconectado — reintentando...';
      statusEl.className = '';
    };
  }

  refreshLogs();
  connectSSE();
</script>
</body>
</html>"""
    return html


@app.route("/admin/logs/json")
@require_admin
def admin_logs_json():
    """Devuelve todos los logs en JSON."""
    with _log_lock:
        data = list(_log_buffer)
    return jsonify(data)


@app.route("/admin/logs/stream")
@require_admin
def admin_logs_stream():
    """SSE: empuja logs nuevos en tiempo real al panel admin."""
    def stream():
        last_len = 0
        while True:
            with _log_lock:
                current = list(_log_buffer)

            if len(current) > last_len:
                for entry in current[last_len:]:
                    yield f"data: {json.dumps(entry)}\n\n"
                last_len = len(current)

            time.sleep(0.5)

    return Response(stream(), mimetype="text/event-stream")


@app.route("/admin/status")
@require_admin
def admin_status():
    """Health check rápido en JSON."""
    with _log_lock:
        total_logs = len(_log_buffer)
        errors = sum(1 for l in _log_buffer if l["level"] in ("ERROR", "CRITICAL"))

    with jobs_lock:
        active_jobs = sum(1 for j in jobs.values() if not j["done"])
        total_jobs = len(jobs)

    return jsonify({
        "timestamp": datetime.now().isoformat(),
        "salesforce_connected": sf is not None,
        "drive_connected": _drive_service is not None,
        "logs_in_buffer": total_logs,
        "errors_in_buffer": errors,
        "active_jobs": active_jobs,
        "total_jobs_session": total_jobs,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)