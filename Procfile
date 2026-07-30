# OJO: en Render manda el "Start Command" del panel, no este archivo. Esta línea es una
# copia de esa configuración para que quede versionada y para que sirva de default si
# algún día se despliega en otro lado (o se vacía el campo del panel).
# Si cambiás una, cambiá la otra.
#
# --workers 1 + --threads 4: la app espera a Salesforce e imgbb, no calcula. Con un solo
#   proceso el buffer de logs y el silencio entre alertas repetidas son consistentes
#   (ambos viven en memoria del proceso).
# --timeout 180: una entrega hace N subidas a Salesforce + N a imgbb de forma síncrona y
#   pasa de largo los 30 s por defecto de gunicorn. Si aun así se corta, el progreso
#   guardado por foto hace que el reintento no vuelva a subir lo que ya subió.
web: gunicorn main:app --workers 1 --threads 4 --timeout 180 --bind 0.0.0.0:$PORT
