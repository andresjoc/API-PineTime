# PineTime BLE PPG API (FastAPI + Bleak)

API en **FastAPI** para **escaneo BLE**, **conexión persistente** y **lectura/streaming de PPG** **exclusivamente para PineTime**, usando **Bleak** sobre **Windows**.

Incluye:
- Escaneo de dispositivos BLE
- Búsqueda por nombre
- Conexión BLE persistente (se mantiene viva en el servidor)
- Lectura PPG por HTTP (ventana fija de 64 muestras)
- Streaming PPG por WebSocket (incremental)
- Cliente web HTML para visualización en tiempo real y exportación a CSV

---

## Requisitos (Windows)

- Windows 10 / 11  
- Bluetooth habilitado (BLE funcional)  
- Python 3.10+ (recomendado 3.11 o superior)  
- PineTime encendido y anunciándose por BLE. Además debe tener el firmware personalizado de Infinitime para habilitar el UUID PPG.

> En Windows, Bleak usa WinRT.  
> Asegúrate de:
> - Tener Bluetooth activo  
> - No estar conectado al PineTime desde otra app BLE  
> - Aceptar permisos Bluetooth si Windows los solicita  

---

## Estructura del proyecto

Ejemplo de estructura mínima:

```
pinetime-ppg-api/
│
├─ main.py                  # API FastAPI
├─ hrs_analysis_tools.py    # Lógica de agregación overlap/reset
├─ receiver.html            # Cliente WebSocket (opcional)
├─ requirements.txt
└─ README.md
```

---

## Instalación

### 1. Crear entorno virtual

Desde PowerShell:

```
python -m venv .venv
.venv\Scripts\Activate.ps1
```

---

### 2. Instalar dependencias

```
pip install -r requirements.txt
```

> `hrs_analysis_tools` debe existir como archivo local o como paquete instalable.

---

## Ejecutar el servidor

```
uvicorn main:api --host 0.0.0.0 --port 8000
```

- Swagger UI:  
  `http://localhost:8000/docs`

- OpenAPI JSON:  
  `http://localhost:8000/openapi.json`

Al apagar el servidor, todas las conexiones BLE persistentes se cierran automáticamente.

---

## Flujo de uso recomendado

### 1. Escanear dispositivos BLE

```
GET /ble/scan?timeout=5
```

Ejemplo:

```
http://localhost:8000/ble/scan?timeout=5
```

Devuelve una lista con:
- name
- address
- rssi

---

### 2. Buscar PineTime por nombre (opcional)

```
GET /ble/name?name=InfiniTime&timeout=5
```

> Para buscar este dispositivo es necesario escribir el nombre del firmware y no del dispositivo, en este caso el firmware es Infitime

La búsqueda es case-insensitive y exacta.

---

### 3. Conectar de forma persistente

#### Opción A: por nombre (recomendado)

```
POST /ble/connect/persistent?name=Infitime&scan_timeout=5&connect_timeout=10
```

- Si hay varios PineTime, intenta primero el de mejor RSSI.
- Si un intento falla, prueba con el siguiente.

#### Opción B: por address

```
POST /ble/connect/persistent/address?address=AA:BB:CC:DD:EE:FF&connect_timeout=10
```

---

### 4. Ver estado de conexión

```
GET /ble/status?address=AA:BB:CC:DD:EE:FF
```

Listar todas las conexiones:

```
GET /ble/connections
```

---

### 5. Leer PPG por HTTP (pull)

```
GET /ble/ppg/read?address=AA:BB:CC:DD:EE:FF&char_uuid=2A39
```

Respuesta:
- unix_time
- samples (64 muestras uint16)

> El PineTime debe enviar exactamente 128 bytes  
> (64 × uint16 little-endian).

---

### 6. Streaming PPG por WebSocket (tiempo real)

```
ws://localhost:8000/ws/ble/ppg
  ?address=AA:BB:CC:DD:EE:FF
  &interval_ms=2000
  &char_uuid=2A39
```

Mensajes enviados:
- new_samples (solo muestras nuevas)
- aggregated_len
- unix_time
- address
- char_uuid

El servidor agrega datos usando lógica de overlap/reset que se encuetra en hrs_analysis_tools.py .

---

## Cliente Web (receiver.html)

1. Abrir `receiver.html` en el navegador  
2. Completar:
   - Backend: `localhost:8000`
   - Address BLE
   - interval_ms
   - char_uuid (`2A39`)
3. Presionar **Conectar**

Funciones:
- Gráfica en tiempo real (PPG crudo)
- Buffer visual limitado
- Exportación CSV  
  - 1 columna: `ppg`  
  - 1 muestra por fila  

> El WebSocket requiere que el PineTime esté conectado previamente de forma persistente.

---

## Endpoints disponibles

- GET  /ble/scan  
- GET  /ble/name  
- POST /ble/connect/persistent  
- POST /ble/connect/persistent/address  
- POST /ble/disconnect  
- GET  /ble/status  
- GET  /ble/connections  
- GET  /ble/ppg/read  
- WS   /ws/ble/ppg  

---

## Troubleshooting (Windows)

### No aparece el PineTime en el escaneo
- Verifica que esté anunciándose por BLE
- Acércalo al PC
- Cierra otras apps BLE

### Fallos de conexión (BleakError)
- Ninguna otra app debe estar conectada al PineTime
- Incrementa `connect_timeout`
- Apaga y enciende Bluetooth en Windows

---

## Notas técnicas

- API exclusiva para PineTime
- Conexiones BLE viven en memoria (no hay base de datos)
- Locks globales y por dispositivo para evitar condiciones de carrera
- Compatible con múltiples versiones de Bleak
