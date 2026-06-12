from __future__ import annotations

import socket


def lan_ipv4_addresses() -> list[str]:
    addresses: list[str] = []

    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            _add_lan_address(addresses, info[4][0])
    except OSError:
        pass

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            _add_lan_address(addresses, sock.getsockname()[0])
    except OSError:
        pass

    return addresses


def dashboard_urls(host: str, port: int, route: str, base_url: str | None = None) -> list[str]:
    normalized_route = route if route.startswith("/") else f"/{route}"
    if base_url:
        return [f"{base_url.rstrip('/')}{normalized_route}"]

    if host in {"0.0.0.0", "::", ""}:
        hosts = lan_ipv4_addresses()
        if not hosts:
            hosts = ["127.0.0.1"]
    else:
        hosts = [host]

    return [f"http://{item}:{port}{normalized_route}" for item in hosts]


def _add_lan_address(addresses: list[str], address: str) -> None:
    if not address:
        return
    if address.startswith("127.") or address.startswith("169.254."):
        return
    if address not in addresses:
        addresses.append(address)
