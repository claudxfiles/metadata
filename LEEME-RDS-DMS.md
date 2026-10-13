# CSV → Amazon RDS / DMS

App local Tkinter: `RDS DMS Migrator.bat` abre `rds_dms_migrator.py`.

## Qué hace
- Perfila uno o muchos CSV: detecta separador/codificación, estima filas y columnas, infiere tipos desde hasta 1.000 filas.
- Genera DDL PostgreSQL o MySQL, mappings JSON de selección para DMS y un manifest del lote.
- Incluye carga directa opcional a PostgreSQL. Requiere instalar `psycopg[binary]` en el Python usado por la app. Inserta en modo **append**, nunca trunca, y al repetir puede duplicar registros.

## Importante sobre DMS
Amazon DMS replica entre endpoints de bases de datos compatibles. Un archivo CSV local no es por sí solo un endpoint relacional de DMS. Flujo típico: cargar CSV a una base staging compatible (o procesarlos desde S3 con un flujo apropiado), validar la fuente, configurar endpoints/instancia/tarea DMS en AWS y migrar a RDS. Esta app prepara esquema/mappings, no crea ni ejecuta recursos AWS.

## Recomendaciones antes de producción
1. Revisa el DDL/tipos y ajusta claves primarias, índices, fechas, precisión decimal y relaciones.
2. Prueba con copias pequeñas y respalda el destino. Define una clave idempotente/estrategia de upsert antes de repetir cargas.
3. Protege credenciales; para RDS activa TLS, limita Security Groups y usa secretos/IAM según tu arquitectura. La app no almacena la contraseña.
4. Prueba el CSV con delimitadores entrecomillados, comillas, saltos de línea y valores nulos reales. La inferencia es orientativa.
5. Para una carga directa PostgreSQL, desde la carpeta `metadata`: `venv\Scripts\python.exe -m pip install "psycopg[binary]"`.

El procesamiento y los archivos exportados permanecen en tu equipo; no se suben automáticamente.
