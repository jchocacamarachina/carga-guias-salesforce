import os
import base64
import requests 
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from simple_salesforce import Salesforce
from werkzeug.utils import secure_filename
from datetime import datetime

app = Flask(__name__)
app.secret_key = "clave-camara-china"

# Configuración Salesforce
SF_USER = os.environ.get("SF_USER", "kdelacruz@camarachina.com")
SF_PASS = os.environ.get("SF_PASS", "Camara1234")
SF_SECURITY_TOKEN = os.environ.get("SF_SECURITY_TOKEN", "iCbyXWW5eZn0XUzx3PyZAX3cF")

#Configuración Webhook de Make
MAKE_WEBHOOK_URL = "https://hook.us2.make.com/ilh879hn49xq3dxxhbihguy2x9vtcjx1"

try:
    sf = Salesforce(username=SF_USER, password=SF_PASS, security_token=SF_SECURITY_TOKEN)
except Exception as e:
    print(f"Error conectando a Salesforce: {e}")

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

@app.route("/get_order_name/<number>", methods=["GET"])
def get_order_name(number):
    op_completa = f"OP-{number}"
    try:
        query = f"SELECT Name FROM Orden_Proveedor__c WHERE Orden_Proveedor_Nro__c = '{op_completa}'"
        result = sf.query_all(query)
        if result['totalSize'] > 0:
            return jsonify({"success": True, "name": result['records'][0]['Name']})
        return jsonify({"success": False})
    except:
        return jsonify({"success": False})

@app.route("/upload", methods=["POST"])
def upload():
    numero_input = request.form.get("order_number")
    file = request.files.get("photo")
    
    op_completa = f"OP-{numero_input.strip()}"

    try:
        # 1. Buscar el registro en Salesforce
        query = f"SELECT Id FROM Orden_Proveedor__c WHERE Orden_Proveedor_Nro__c = '{op_completa}'"
        result = sf.query_all(query)
        record_id = result['records'][0]['Id']
        
        # 2. Subir archivo a Salesforce
        file_content = file.read()
        base64_image = base64.b64encode(file_content).decode('utf-8')
        filename = f"Guia_{op_completa}.jpg"

        cv = sf.ContentVersion.create({
            'Title': filename, 'PathOnClient': filename, 'VersionData': base64_image, 'FirstPublishLocationId': record_id
        })
        
        cv_id = cv['id']
        cd = sf.ContentDistribution.create({
            'Name': f'Link_{filename}', 'ContentVersionId': cv_id, 
            'PreferencesAllowViewInBrowser': True, 'PreferencesLinkLatestVersion': True
        })
        
        public_url = sf.ContentDistribution.get(cd['id'])['DistributionPublicUrl']
        
        # 3. Actualizar URL en Salesforce
        sf.Orden_Proveedor__c.update(record_id, {'Guia_de_Entrega_URL__c': public_url})

        # 4. ENVIAR AL WEBHOOK DE MAKE
        webhook_data = {
            "order_id": record_id,
            "order_number": op_completa,
            "timestamp": datetime.now().isoformat()
        }
        
        # Enviamos la petición POST a Make
        requests.post(MAKE_WEBHOOK_URL, json=webhook_data)
        # ------------------------------------

        flash(f"✅ ¡Éxito! Foto cargada.", "success")
        
    except Exception as e:
        flash(f"⚠️|Error: {str(e)}", "error")

    return redirect(url_for("index"))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)