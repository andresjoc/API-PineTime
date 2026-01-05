from __future__ import annotations

import asyncio
import time
import struct

import numpy as np
import hrs_analysis_tools
from dataclasses import dataclass
from typing import Dict, List, Optional, Union

from fastapi import FastAPI, Query, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from bleak import BleakScanner, BleakClient
from bleak.exc import BleakError


api = FastAPI(title="BLE Scanner API")


# -----------------------
# Models (los tuyos + algunos nuevos)
# -----------------------

class BLEDeviceOut(BaseModel):
    """Modelo para representar un dispositivo BLE encontrado en el escaneo."""
    name: Optional[str] = None
    address: str
    rssi: Optional[int] = None


class BLEDevicesFound(BaseModel):
    """Modelo para respuesta cuando se encuentra uno o más dispositivos por nombre."""
    name: str
    addresses: List[str]


class BLEDeviceFound(BaseModel):
    """Modelo para representar un dispositivo BLE encontrado."""
    name: Optional[str] = None
    address: str


class BLEDeviceNotFound(BaseModel):
    """Modelo para respuesta cuando no se encuentra un dispositivo."""
    message: str


class BLEConnectPersistentSuccess(BaseModel):
    """Modelo de respuesta exitosa para conexión persistente por nombre."""
    message: str
    name: str
    address: str
    is_connected: bool
    rssi: Optional[int] = None


class BLEConnectPersistentFailed(BaseModel):
    """Modelo de respuesta fallida para conexión persistente, incluye intentos y errores."""
    message: str
    name: str
    attempted_addresses: List[str]
    errors: Optional[List[str]] = None

class BLEConnectPersistentByAddressSuccess(BaseModel):
    """Modelo de respuesta exitosa para conexión persistente por address."""
    message: str
    address: str
    is_connected: bool


class PPGReadResponse(BaseModel):
    """Modelo de respuesta para lectura de datos PPG (64 muestras uint16)."""
    address: str
    char_uuid: str
    unix_time: float
    samples: List[int]  # 64 samples (uint16)


class BLEDisconnectResponse(BaseModel):
    """Modelo de respuesta para desconexión de dispositivo BLE."""
    message: str
    address: str
    was_connected: bool


class BLEConnectionStatus(BaseModel):
    """Modelo para estado de conexión de un dispositivo."""
    address: str
    is_connected: bool


class BLEConnectionsList(BaseModel):
    """Modelo para listar todas las conexiones persistentes y sus estados."""
    connections: List[BLEConnectionStatus]


# -----------------------
# Connection Manager (persistencia real)
# -----------------------

@dataclass
class ManagedConnection:
    """
    Contenedor para una conexión BLE persistente.
    - client: cliente BLE (BleakClient)
    - lock: lock asincrónico para evitar accesos concurrentes al mismo dispositivo
    - last_name: nombre del dispositivo (opcional)
    """
    client: BleakClient
    lock: asyncio.Lock
    last_name: Optional[str] = None


class BLEConnectionManager:
    """
    Gestor de conexiones BLE persistentes en memoria.
    Mantiene un diccionario de conexiones activas (address -> ManagedConnection).
    - Permite conectar, desconectar y consultar estado de dispositivos.
    - Usa locks para evitar condiciones de carrera.
    """
    def __init__(self) -> None:
        self._connections: Dict[str, ManagedConnection] = {}
        self._global_lock = asyncio.Lock()

    async def is_connected(self, address: str) -> bool:
        """
        Verifica si un dispositivo está conectado.
        Compatible con diferentes versiones de bleak.
        """
        async with self._global_lock:
            managed = self._connections.get(address)
        if not managed:
            return False
        # bleack client: is_connected() puede ser async o property según versión; manejamos ambos
        try:
            ic = managed.client.is_connected
            if callable(ic):
                return bool(await ic())
            return bool(ic)
        except Exception:
            return False

    async def get_connections(self) -> List[str]:
        """Retorna lista de addresses de todas las conexiones persistentes."""
        async with self._global_lock:
            return list(self._connections.keys())

    async def connect_persistent(
        self,
        address: str,
        connect_timeout: float,
        name: Optional[str] = None
    ) -> bool:
        """
        Conecta y mantiene el cliente en memoria.
        Si ya existe conexión para address, intenta re-usarla.
        Retorna True si la conexión es exitosa.
        """
        async with self._global_lock:
            managed = self._connections.get(address)
            if not managed:
                # Crear nuevo cliente BLE si no existe
                client = BleakClient(address, timeout=connect_timeout)
                managed = ManagedConnection(client=client, lock=asyncio.Lock(), last_name=name)
                self._connections[address] = managed

        # Lock por dispositivo para evitar conectar dos veces simultáneamente
        async with managed.lock:
            # Si ya está conectado, no es necesario reconectar
            if await self.is_connected(address):
                managed.last_name = name or managed.last_name
                return True

            try:
                await managed.client.connect()
                managed.last_name = name or managed.last_name
            except Exception:
                # Si falla, limpiamos el registro para no dejar basura
                async with self._global_lock:
                    self._connections.pop(address, None)
                raise

            return await self.is_connected(address)

    async def disconnect(self, address: str) -> bool:
        """
        Desconecta y elimina el cliente de memoria.
        Retorna True si el dispositivo estaba conectado al momento de desconectar.
        """
        async with self._global_lock:
            managed = self._connections.get(address)

        if not managed:
            return False

        async with managed.lock:
            was = await self.is_connected(address)
            try:
                await managed.client.disconnect()
            except Exception:
                # aunque falle el disconnect, removemos para evitar bloqueo permanente
                pass

            async with self._global_lock:
                self._connections.pop(address, None)

            return was

    async def disconnect_all(self) -> None:
        """Desconecta todos los dispositivos registrados."""
        addrs = await self.get_connections()
        for a in addrs:
            try:
                await self.disconnect(a)
            except Exception:
                pass
            
    async def get_client(self, address: str) -> Optional[BleakClient]:
        """Retorna el cliente BLE para un address específico, si existe."""
        async with self._global_lock:
            managed = self._connections.get(address)
            return managed.client if managed else None


manager = BLEConnectionManager()


@api.on_event("shutdown")
async def shutdown_event():
    """
    Evento de cierre del servidor.
    Intenta desconectar todas las conexiones persistentes de forma ordenada.
    """
    await manager.disconnect_all()


# -----------------------
# Endpoints existentes (tuyos)
# -----------------------

@api.get("/ble/scan", response_model=List[BLEDeviceOut])
async def scan_ble(
    timeout: float = Query(
        5.0,
        ge=1.0,
        le=30.0,
        description="Tiempo de escaneo en segundos"
    )
):  # ge = greater equal, le = less equal
    """
    Escanea dispositivos BLE durante `timeout` segundos y devuelve
    nombre, address e RSSI de cada dispositivo encontrado.
    """
    devices = await BleakScanner.discover(timeout=timeout)

    results: List[BLEDeviceOut] = []
    seen = set()

    for d in devices:
        addr = getattr(d, "address", None)
        # Evitar duplicados
        if not addr or addr in seen:
            continue
        seen.add(addr)

        results.append(
            BLEDeviceOut(
                name=d.name or None,
                address=addr,
                rssi=getattr(d, "rssi", None),
            )
        )

    return results


@api.get("/ble/name", response_model=Union[BLEDevicesFound, BLEDeviceNotFound])
async def get_addresses_by_name(
    name: str = Query(..., min_length=1, description="Nombre del dispositivo BLE"),
    timeout: float = Query(5.0, ge=1.0, le=30.0, description="Tiempo de escaneo en segundos"),
):
    """
    Escanea dispositivos BLE durante `timeout` segundos y devuelve
    las direcciones MAC de los dispositivos que coincidan exactamente con `name`.
    La búsqueda es case-insensitive.
    """
    devices = await BleakScanner.discover(timeout=timeout)

    target = name.strip().lower()
    addresses: List[str] = []

    for d in devices:
        dname = (d.name or "").strip().lower()
        if dname == target:
            addresses.append(d.address)

    if not addresses:
        return BLEDeviceNotFound(message="Dispositivo no encontrado")

    return BLEDevicesFound(name=name, addresses=addresses)

# Uso: /ble/name?name=MiSensor&timeout=8 | /ble/name?name=OtroDispositivo


# -----------------------
# NUEVO: conexión persistente por nombre
# -----------------------

@api.post(
    "/ble/connect/persistent",
    response_model=Union[BLEConnectPersistentSuccess, BLEDeviceNotFound, BLEConnectPersistentFailed]
)
async def connect_persistent_by_name(
    name: str = Query(..., min_length=1, description="Nombre exacto del dispositivo BLE"),
    scan_timeout: float = Query(5.0, ge=1.0, le=30.0, description="Tiempo de escaneo en segundos"),
    connect_timeout: float = Query(10.0, ge=1.0, le=60.0, description="Tiempo máximo de conexión en segundos"),
):
    """
    Escanea dispositivos BLE por `scan_timeout` segundos, busca coincidencia exacta por `name`
    y establece una conexión persistente (se queda conectada en memoria del servidor).

    - Si hay varios dispositivos con el mismo nombre, intenta primero el de mejor RSSI (si existe).
    - Si no encuentra el dispositivo: devuelve "Dispositivo no encontrado".
    - Si encuentra pero no logra conectarse a ninguno: devuelve "No se pudo conectar".
    """
    devices = await BleakScanner.discover(timeout=scan_timeout)
    target = name.strip().lower()

    # Filtrar dispositivos que coinciden con el nombre
    matches = []
    for d in devices:
        dname = (d.name or "").strip().lower()
        if dname == target:
            matches.append(d)

    if not matches:
        return BLEDeviceNotFound(message="Dispositivo no encontrado")

    # Ordenar por RSSI: mejor señal primero (valores más cercanos a 0 son mejores)
    # None se coloca al final
    def rssi_sort_key(dev):
        rssi = getattr(dev, "rssi", None)
        return (rssi is None, -(rssi if rssi is not None else -9999))

    matches.sort(key=rssi_sort_key)

    attempted_addresses: List[str] = []
    errors: List[str] = []

    # Intentar conectar a cada dispositivo encontrado
    for dev in matches:
        address = dev.address
        attempted_addresses.append(address)
        try:
            ok = await manager.connect_persistent(
                address=address,
                connect_timeout=connect_timeout,
                name=name
            )
            if ok:
                # Conexión exitosa
                return BLEConnectPersistentSuccess(
                    message="Conexión persistente establecida",
                    name=name,
                    address=address,
                    is_connected=True,
                    rssi=getattr(dev, "rssi", None)
                )
            else:
                errors.append(f"{address}: connect ok pero is_connected=False")
        except (BleakError, Exception) as e:
            errors.append(f"{address}: {type(e).__name__}: {str(e)}")

    # Todos los intentos fallaron
    return BLEConnectPersistentFailed(
        message="No se pudo conectar a ningún dispositivo con ese nombre",
        name=name,
        attempted_addresses=attempted_addresses,
        errors=errors or None
    )

@api.post("/ble/connect/persistent/address", response_model=BLEConnectPersistentByAddressSuccess)
async def connect_persistent_by_address(
    address: str = Query(..., min_length=1, description="Address (MAC/ID) del dispositivo BLE"),
    connect_timeout: float = Query(10.0, ge=1.0, le=60.0, description="Tiempo máximo de conexión en segundos"),
):
    """
    Establece una conexión persistente por address (MAC/ID) y la mantiene en memoria del servidor.
    Útil cuando ya conoces la dirección exacta del dispositivo.
    """

    try:
        ok = await manager.connect_persistent(address=address, connect_timeout=connect_timeout, name=None)
        return BLEConnectPersistentByAddressSuccess(
            message="Conexión persistente establecida" if ok else "No se pudo conectar",
            address=address,
            is_connected=ok,
        )
    except (BleakError, Exception) as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {str(e)}")


@api.post("/ble/disconnect", response_model=BLEDisconnectResponse)
async def disconnect_device(
    address: str = Query(..., min_length=1, description="Address (MAC/ID) del dispositivo BLE"),
):
    """
    Desconecta una conexión persistente por address y la elimina del manager.
    """
    was_connected = await manager.disconnect(address)
    return BLEDisconnectResponse(
        message="Desconectado" if was_connected else "No había conexión activa para ese address",
        address=address,
        was_connected=was_connected
    )


@api.get("/ble/status", response_model=BLEConnectionStatus)
async def connection_status(
    address: str = Query(..., min_length=1, description="Address (MAC/ID) del dispositivo BLE"),
):
    """
    Devuelve el estado de conexión de un dispositivo (conectado o no).
    """
    return BLEConnectionStatus(
        address=address,
        is_connected=await manager.is_connected(address)
    )


@api.get("/ble/connections", response_model=BLEConnectionsList)
async def list_connections():
    """
    Lista todas las conexiones persistentes registradas en el manager
    y su estado actual (conectado/desconectado).
    """
    addrs = await manager.get_connections()
    statuses: List[BLEConnectionStatus] = []
    for a in addrs:
        statuses.append(BLEConnectionStatus(address=a, is_connected=await manager.is_connected(a)))
    return BLEConnectionsList(connections=statuses)

# UUID por defecto para características PPG
PPG_CHAR_UUID_DEFAULT = "2A39"

@api.get("/ble/ppg/read", response_model=PPGReadResponse)
async def read_ppg_window(
    address: str = Query(..., min_length=1, description="Address (MAC/ID) del dispositivo BLE"),
    char_uuid: str = Query(PPG_CHAR_UUID_DEFAULT, min_length=1, description="UUID/short UUID de la característica (ej: 2A39)"),
):

    """
    Lee una ventana de datos PPG (64 muestras uint16) desde la característica
    especificada en el dispositivo conectado persistentemente.
    Requiere que el dispositivo esté conectado previamente.
    """

    client = await manager.get_client(address)
    if not client or not await manager.is_connected(address):
        raise HTTPException(status_code=400, detail="No existe conexión persistente para ese address")

    try:
        # Leer datos brutos de la característica (128 bytes = 64 uint16)
        raw = await client.read_gatt_char(char_uuid)
        # Desempacar como 64 valores uint16 en formato little-endian
        samples = list(struct.unpack("<64H", raw))
        return PPGReadResponse(
            address=address,
            char_uuid=char_uuid,
            unix_time=time.time(),
            samples=samples,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {str(e)}")


@api.websocket("/ws/ble/ppg")
async def ws_ble_ppg(
    websocket: WebSocket,
    address: str,
    interval_ms: int = 2000,
    char_uuid: str = PPG_CHAR_UUID_DEFAULT,
    send_full_aggregated: bool = False,
):

    """
    WebSocket que envía datos PPG continuamente desde la característica
    especificada en el dispositivo conectado persistentemente.
    
    Parámetros:
    - address: Address (MAC/ID) del dispositivo BLE
    - interval_ms: intervalo entre lecturas en milisegundos (mínimo 50 ms)
    - char_uuid: UUID/short UUID de la característica (ej: 2A39)
    - send_full_aggregated: si es True, envía también el arreglo completo agregado en cada mensaje
    
    Mensajes enviados (JSON):
    - address: address del dispositivo
    - char_uuid: UUID/short UUID de la característica
    - unix_time: timestamp de la lectura
    - new_samples: nuevas muestras agregadas en esta lectura (lista de int)
    - aggregated_len: longitud total del arreglo agregado hasta ahora
    - aggregated: (opcional) arreglo completo agregado (lista de int), si send_full_aggregated es True
    - error: en caso de error, mensaje de error
    """

    await websocket.accept()

    client = await manager.get_client(address)
    if not client or not await manager.is_connected(address):
        await websocket.send_json({"error": "No existe conexión persistente para ese address"})
        await websocket.close(code=1008)
        return

    # Convertir milisegundos a segundos, con mínimo de 50 ms
    interval_s = max(0.05, interval_ms / 1000.0)

    # Variables para rastrear datos agregados
    last_arr = None
    aggregated = None
    prev_len = 0

    try:
        while True:
            # Leer datos PPG del dispositivo
            raw = await client.read_gatt_char(char_uuid)
            arr = np.array(struct.unpack("<64H", raw), dtype=np.uint16)

            if last_arr is None:
                # Primera lectura: inicializar array agregado
                last_arr = arr
                aggregated = arr.copy()
                prev_len = len(aggregated)

                await websocket.send_json({
                    "address": address,
                    "char_uuid": char_uuid,
                    "unix_time": time.time(),
                    "new_samples": arr.astype(int).tolist(),   # primera vez: todo
                    "aggregated_len": int(len(aggregated)),
                })
            else:
                # Agregar solo lo nuevo usando la lógica de overlap/reset
                aggregated = hrs_analysis_tools.add_new_data(aggregated, last_arr, arr)
                last_arr = arr

                # Calcular segmento nuevo (desde prev_len hasta fin)
                new_segment = aggregated[prev_len:]
                prev_len = len(aggregated)

                payload = {
                    "address": address,
                    "char_uuid": char_uuid,
                    "unix_time": time.time(),
                    "new_samples": new_segment.astype(int).tolist(),
                    "aggregated_len": int(len(aggregated)),
                }

                # Opcionalmente enviar array completo agregado
                if send_full_aggregated:
                    payload["aggregated"] = aggregated.astype(int).tolist()

                await websocket.send_json(payload)

            # Esperar antes de siguiente lectura
            await asyncio.sleep(interval_s)

    except WebSocketDisconnect:
        # Cliente cerró el socket
        return
    except Exception as e:
        # Intentar enviar mensaje de error al cliente
        try:
            await websocket.send_json({"error": f"{type(e).__name__}: {str(e)}"})
        except Exception:
            pass
        try:
            await websocket.close(code=1011)
        except Exception:
            pass



# Ejemplos de uso:
#   POST /ble/connect/persistent?name=MiSensor&scan_timeout=6&connect_timeout=12
#   GET  /ble/connections
#   GET  /ble/status?address=AA:BB:CC:DD:EE:FF
#   POST /ble/disconnect?address=AA:BB:CC:DD:EE:FF
