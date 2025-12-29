from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Dict, List, Optional, Union

from fastapi import FastAPI, Query, HTTPException
from pydantic import BaseModel
from bleak import BleakScanner, BleakClient
from bleak.exc import BleakError


api = FastAPI(title="BLE Scanner API")


# -----------------------
# Models (los tuyos + algunos nuevos)
# -----------------------

class BLEDeviceOut(BaseModel):
    name: Optional[str] = None
    address: str
    rssi: Optional[int] = None


class BLEDevicesFound(BaseModel):
    name: str
    addresses: List[str]


class BLEDeviceFound(BaseModel):
    name: Optional[str] = None
    address: str


class BLEDeviceNotFound(BaseModel):
    message: str


class BLEConnectPersistentSuccess(BaseModel):
    message: str
    name: str
    address: str
    is_connected: bool
    rssi: Optional[int] = None


class BLEConnectPersistentFailed(BaseModel):
    message: str
    name: str
    attempted_addresses: List[str]
    errors: Optional[List[str]] = None


class BLEDisconnectResponse(BaseModel):
    message: str
    address: str
    was_connected: bool


class BLEConnectionStatus(BaseModel):
    address: str
    is_connected: bool


class BLEConnectionsList(BaseModel):
    connections: List[BLEConnectionStatus]


# -----------------------
# Connection Manager (persistencia real)
# -----------------------

@dataclass
class ManagedConnection:
    client: BleakClient
    lock: asyncio.Lock
    last_name: Optional[str] = None


class BLEConnectionManager:
    """
    Mantiene conexiones BLE persistentes en memoria.
    - key: address (MAC/ID)
    - value: BleakClient conectado + lock por dispositivo
    """
    def __init__(self) -> None:
        self._connections: Dict[str, ManagedConnection] = {}
        self._global_lock = asyncio.Lock()

    async def is_connected(self, address: str) -> bool:
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
        """
        async with self._global_lock:
            managed = self._connections.get(address)
            if not managed:
                client = BleakClient(address, timeout=connect_timeout)
                managed = ManagedConnection(client=client, lock=asyncio.Lock(), last_name=name)
                self._connections[address] = managed

        # Lock por dispositivo para evitar conectar dos veces a la vez
        async with managed.lock:
            # Si ya está conectado, listo
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
        Retorna si estaba conectado en el momento de desconectar.
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
        addrs = await self.get_connections()
        for a in addrs:
            try:
                await self.disconnect(a)
            except Exception:
                pass


manager = BLEConnectionManager()


@api.on_event("shutdown")
async def shutdown_event():
    """
    Al apagar el servidor, intenta cerrar todas las conexiones persistentes.
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
    nombre + address.
    """
    devices = await BleakScanner.discover(timeout=timeout)

    results: List[BLEDeviceOut] = []
    seen = set()

    for d in devices:
        addr = getattr(d, "address", None)
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

    matches = []
    for d in devices:
        dname = (d.name or "").strip().lower()
        if dname == target:
            matches.append(d)

    if not matches:
        return BLEDeviceNotFound(message="Dispositivo no encontrado")

    # Orden: mejor RSSI primero (más cerca de 0 es mejor). None al final.
    def rssi_sort_key(dev):
        rssi = getattr(dev, "rssi", None)
        return (rssi is None, -(rssi if rssi is not None else -9999))

    matches.sort(key=rssi_sort_key)

    attempted_addresses: List[str] = []
    errors: List[str] = []

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

    return BLEConnectPersistentFailed(
        message="No se pudo conectar a ningún dispositivo con ese nombre",
        name=name,
        attempted_addresses=attempted_addresses,
        errors=errors or None
    )


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
    Devuelve si el dispositivo está conectado (según el manager).
    """
    return BLEConnectionStatus(
        address=address,
        is_connected=await manager.is_connected(address)
    )


@api.get("/ble/connections", response_model=BLEConnectionsList)
async def list_connections():
    """
    Lista todas las conexiones persistentes (addresses) registradas en el manager
    y su estado actual.
    """
    addrs = await manager.get_connections()
    statuses: List[BLEConnectionStatus] = []
    for a in addrs:
        statuses.append(BLEConnectionStatus(address=a, is_connected=await manager.is_connected(a)))
    return BLEConnectionsList(connections=statuses)


# Ejemplos de uso:
#   POST /ble/connect/persistent?name=MiSensor&scan_timeout=6&connect_timeout=12
#   GET  /ble/connections
#   GET  /ble/status?address=AA:BB:CC:DD:EE:FF
#   POST /ble/disconnect?address=AA:BB:CC:DD:EE:FF
