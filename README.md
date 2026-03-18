# PRE-FACTURAS (Django + SQL Server)

Proyecto Django listo para trabajar con una base de datos existente en SQL Server.

## 1) Activar entorno virtual

```powershell
.\.venv\Scripts\Activate.ps1
```

## 2) Completar conexión en `.env`

Edita `./.env` con los datos reales de tu servidor:

```env
SQLSERVER_HOST=TU_HOST_O_IP
SQLSERVER_PORT=1433
SQLSERVER_DB=TU_BASE_EXISTENTE
SQLSERVER_USER=TU_USUARIO
SQLSERVER_PASSWORD=TU_PASSWORD
SQLSERVER_DRIVER=ODBC Driver 18 for SQL Server
SQLSERVER_EXTRA_PARAMS=TrustServerCertificate=yes;
```

## 3) Verificar conexión Django -> SQL Server

```powershell
.\.venv\Scripts\python.exe manage.py check
```

## 4) Generar modelos desde tablas existentes

```powershell
.\.venv\Scripts\python.exe manage.py inspectdb > prefacturas_app\models_existing.py
```

Si quieres solo algunas tablas:

```powershell
.\.venv\Scripts\python.exe manage.py inspectdb tabla1 tabla2 > prefacturas_app\models_existing.py
```

## 5) Levantar servidor de desarrollo

```powershell
.\.venv\Scripts\python.exe manage.py runserver
```

## 6) Parametros base para e-CF

Puedes complementar `.env` con una estrategia de integracion:

```env
ECF_PROVIDER_MODE=external
ECF_CERT_FILE=C:\certificados\empresa.p12
ECF_CALLBACK_API_KEY=define-una-clave-larga
ECF_EXTERNAL_SUBMIT_URL=https://tu-integrador/api/ecf/submit
ECF_EXTERNAL_API_KEY=token-del-integrador
ECF_DGII_AUTH_URL=
ECF_DGII_SUBMIT_URL=
```

Modos soportados:

- `manual`: solo preparacion interna.
- `external`: el sistema despacha al integrador externo cuando `modo_envio=automatico`.
- `dgii_direct`: reservado para futura integracion directa con DGII.

## 7) Auditar readiness e-CF

```powershell
.\.venv\Scripts\python.exe manage.py ecf_readiness
```

## 8) Previsualizar payload para integrador

```powershell
.\.venv\Scripts\python.exe manage.py ecf_preview_payload 12345
```

Esto imprime el documento con:

- datos de empresa
- cabecera de la factura o nota de credito
- detalle de lineas
- montos y referencias base

## 9) Checklist para integrador externo e-CF

Antes de cambiar `ECF_PROVIDER_MODE=external`, el integrador debe entregar como minimo:

- URL de envio: endpoint `POST` que reciba el payload del documento.
- Mecanismo de autenticacion: por ejemplo `X-ECF-API-Key` o `Bearer token`.
- Contrato de respuesta: debe devolver al menos `track_id` y un mensaje o `detail`.
- Callback de recepcion: debe invocar la URL de recepcion del emisor con `id_doc`, `encf` o `track_id`.
- Callback de aprobacion: debe invocar la URL de aprobacion con el estado final comercial.
- Codigo de seguridad: debe devolver `codigo_seguridad` cuando el e-CF quede listo para QR.
- URL QR opcional: puede devolver `url_consulta_qr`; si no, el sistema la construye cuando recibe `codigo_seguridad`.
- Flags operativos: debe poder informar `xml_generado`, `firmado` y `enviado_dgii`.
- Mapeo de estados: documentar valores de `estado`, `estatus`, `decision` y errores.
- Ambiente de pruebas: URL y credenciales separadas para precertificacion.
- Muestras reales: al menos un request y un response de factura, nota de credito y rechazo.
- Politica de reintentos: tiempos de espera, reenvio y manejo de duplicados.

## 10) Variables que debes completar para activar integrador

```env
ECF_PROVIDER_MODE=external
ECF_CERT_FILE=C:\certificados\empresa.p12
ECF_CALLBACK_API_KEY=una-clave-larga-y-privada
ECF_EXTERNAL_SUBMIT_URL=https://tu-integrador/api/ecf/submit
ECF_EXTERNAL_API_KEY=token-del-integrador
```

Con eso listo, vuelve a ejecutar:

```powershell
.\.venv\Scripts\python.exe manage.py ecf_readiness
```
