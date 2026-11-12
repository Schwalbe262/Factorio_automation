from __future__ import annotations

import json
import socket
import struct
import time
from dataclasses import dataclass
from typing import Any


SERVERDATA_RESPONSE_VALUE = 0
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_AUTH = 3


class RconError(RuntimeError):
    pass


@dataclass(frozen=True)
class RconPacket:
    request_id: int
    packet_type: int
    body: str


class FactorioRconClient:
    def __init__(self, host: str, port: int, password: str, timeout: float = 10.0) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self._socket: socket.socket | None = None
        self._request_id = 1000

    def __enter__(self) -> "FactorioRconClient":
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def connect(self) -> None:
        self.close()
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        self._socket = sock
        request_id = self._next_id()
        self._send(request_id, SERVERDATA_AUTH, self.password)
        deadline = time.monotonic() + self.timeout
        authenticated = False
        while time.monotonic() < deadline:
            packet = self._read()
            if packet.request_id == -1:
                raise RconError("RCON authentication failed")
            if packet.request_id == request_id:
                authenticated = True
                break
        if not authenticated:
            raise RconError("RCON authentication timed out")

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    def execute(self, command: str, drain_seconds: float = 0.15) -> str:
        if self._socket is None:
            self.connect()
        request_id = self._next_id()
        self._send(request_id, SERVERDATA_EXECCOMMAND, command)
        chunks: list[str] = []
        deadline = time.monotonic() + self.timeout
        first_response_at: float | None = None
        original_timeout = self._socket.gettimeout()
        try:
            while time.monotonic() < deadline:
                try:
                    packet = self._read()
                except socket.timeout:
                    if first_response_at is not None:
                        break
                    raise
                if packet.request_id != request_id:
                    continue
                if packet.body:
                    chunks.append(packet.body)
                    first_response_at = first_response_at or time.monotonic()
                if first_response_at is not None and time.monotonic() - first_response_at >= drain_seconds:
                    break
                if first_response_at is not None:
                    self._socket.settimeout(drain_seconds)
        finally:
            self._socket.settimeout(original_timeout)
        return "".join(chunks).strip()

    def execute_json_command(self, name: str, parameter: dict[str, Any] | str | None = None) -> dict[str, Any]:
        if isinstance(parameter, dict):
            parameter_text = json.dumps(parameter, separators=(",", ":"))
        elif parameter is None:
            parameter_text = ""
        else:
            parameter_text = str(parameter)
        command = f"/{name}"
        if parameter_text:
            command += f" {parameter_text}"
        response = self.execute(command)
        if "Unknown command" in response:
            fallback = name
            if parameter_text:
                fallback += f" {parameter_text}"
            response = self.execute(fallback)
        return parse_json_response(response)

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _send(self, request_id: int, packet_type: int, body: str) -> None:
        if self._socket is None:
            raise RconError("RCON socket is not connected")
        body_bytes = body.encode("utf-8")
        packet = struct.pack("<iii", len(body_bytes) + 10, request_id, packet_type)
        packet += body_bytes + b"\x00\x00"
        self._socket.sendall(packet)

    def _read(self) -> RconPacket:
        if self._socket is None:
            raise RconError("RCON socket is not connected")
        size_data = _recv_exact(self._socket, 4)
        (size,) = struct.unpack("<i", size_data)
        if size < 10:
            raise RconError(f"invalid RCON packet size: {size}")
        payload = _recv_exact(self._socket, size)
        request_id, packet_type = struct.unpack("<ii", payload[:8])
        body = payload[8:-2].decode("utf-8", errors="replace")
        return RconPacket(request_id=request_id, packet_type=packet_type, body=body)


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise RconError("RCON connection closed")
        chunks.extend(chunk)
    return bytes(chunks)


def parse_json_response(response: str) -> dict[str, Any]:
    response = response.strip()
    if not response:
        raise RconError("empty RCON response")
    for line in response.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    try:
        parsed = json.loads(response)
    except json.JSONDecodeError as exc:
        raise RconError(f"RCON response did not contain JSON: {response[:300]}") from exc
    if not isinstance(parsed, dict):
        raise RconError("RCON JSON response must be an object")
    return parsed
