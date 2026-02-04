import os
import base64
import requests
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from simple_salesforce import Salesforce
from datetime import datetime

app = Flask(__name__)
app.secret_key = "clave-camara-china"

# Configuración Salesforce (OJO: no dejes credenciales hardcodeadas en producción)
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


def normalizar_op(numero: str) -> str:
    """
    Convierte input '11139' o '0011139' en el formato exacto que usa tu SF:
    OP-00 + numero (sin espacios). Ej: OP-0011139
    """
    numero = (numero or "").strip()
    return f"OP-00{numero}"


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


@app.route("/upload", methods=["POST"])
def upload():
    if sf is None:
        flash("⚠️ Salesforce no está conectado en el servidor.", "error")
        return redirect(url_for("index"))

    numero_input = request.form.get("order_number", "").strip()
    file = request.files.get("photo")

    # Validaciones para que nunca reviente
    if not numero_input:
        flash("⚠️ Debes buscar una orden antes de guardar la foto.", "error")
        return redirect(url_for("index"))

    if not file or file.filename == "":
        flash("⚠️ Debes seleccionar una foto antes de guardar.", "error")
        return redirect(url_for("index"))

    # Formato OP consistente con el GET
    op_completa = normalizar_op(numero_input)

    try:
        # 1) Buscar registro en Salesforce
        query = f"SELECT Id FROM Orden_Proveedor__c WHERE Orden_Proveedor_Nro__c = '{op_completa}'"
        result = sf.query_all(query)

        if result.get("totalSize", 0) == 0:
            flash("❌ Orden no encontrada en Salesforce.", "error")
            return redirect(url_for("index"))

        record_id = result["records"][0]["Id"]

        # 2) Subir archivo a Salesforce
        file_content = file.read()
        if not file_content:
            flash("⚠️ La foto llegó vacía. Intenta nuevamente.", "error")
            return redirect(url_for("index"))

        base64_image = base64.b64encode(file_content).decode("utf-8")
        filename = f"Guia_{op_completa}.jpg"

        cv = sf.ContentVersion.create({
            "Title": filename,
            "PathOnClient": filename,
            "VersionData": base64_image,
            "FirstPublishLocationId": record_id
        })

        cv_id = cv["id"]

        cd = sf.ContentDistribution.create({
            "Name": f"Link_{filename}",
            "ContentVersionId": cv_id,
            "PreferencesAllowViewInBrowser": True,
            "PreferencesLinkLatestVersion": True
        })

        public_url = sf.ContentDistribution.get(cd["id"])["DistributionPublicUrl"]

        # 3) Guardar URL en Salesforce
        sf.Orden_Proveedor__c.update(record_id, {"Guia_de_Entrega_URL__c": public_url})

        # 4) Webhook a Make (si falla, NO tumba el flujo)
        webhook_data = {
            "order_id": record_id,
            "order_number": op_completa,
            "timestamp": datetime.now().isoformat()
        }
        try:
            requests.post(MAKE_WEBHOOK_URL, json=webhook_data, timeout=10)
        except Exception:
            pass

        flash("✅ ¡Éxito! Foto cargada.", "success")

    except Exception as e:
        flash(f"⚠️ Error: {str(e)}", "error")

    return redirect(url_for("index"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
