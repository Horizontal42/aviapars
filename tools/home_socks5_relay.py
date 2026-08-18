"""Minimal SOCKS5 relay (RFC 1928, CONNECT only, no auth) — stdlib only.

Run on the admin's own home machine (NOT the VDS) to give the bot's VDS-hosted
adapters an optional residential egress. Binds to this machine's Tailscale IP by
default, so it's reachable only from inside the tailnet (encrypted, peer-
authenticated by Tailscale itself) — never exposed to the public internet.

Why this exists: `pobeda_direct`'s carrier WAF blocks every datacenter/VPN-range
IP tried (the VDS itself, three different commercial VPN exits) but not a genuine
residential ISP address — see ARCHITECTURE.md. Point `pobeda_proxy_url` in
config.yaml at `socks5://<this machine's tailscale ip>:<port>` to route just that
one adapter's traffic through here; nothing else in the roster uses it.

Usage: python tools/home_socks5_relay.py [--host IP] [--port PORT]
Defaults to `tailscale ip -4` (auto-detected) and port 11080.
No auto-start is wired up anywhere — start/stop by hand as needed.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import socket
import struct
import subprocess


def _detect_tailscale_ip() -> str:
    out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, check=True)
    ip = out.stdout.strip()
    if not ip:
        raise RuntimeError("tailscale ip -4 returned nothing — is tailscaled up and assigned an IPv4?")
    return ip


def _is_forwardable(addr: str) -> bool:
    """Refuses loopback/private/link-local/reserved destinations — this relay exists to reach
    public carrier sites over a residential egress, not to pivot into the admin's home LAN
    (router admin panels, other LAN hosts) from whatever's on the other end of the tailnet."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True  # a hostname — resolved and re-checked at connect time by open_connection
    return not (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast)


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        _ver, nmethods = await reader.readexactly(2)
        await reader.readexactly(nmethods)
        writer.write(b"\x05\x00")  # no-auth selected
        await writer.drain()

        _ver, _cmd, _rsv, atyp = await reader.readexactly(4)
        if atyp == 1:  # IPv4
            addr = socket.inet_ntoa(await reader.readexactly(4))
        elif atyp == 3:  # domain name
            length = (await reader.readexactly(1))[0]
            addr = (await reader.readexactly(length)).decode()
        elif atyp == 4:  # IPv6
            addr = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
        else:
            writer.close()
            return
        port = struct.unpack("!H", await reader.readexactly(2))[0]

        if _cmd != 1:  # CONNECT only — docstring promise, now actually enforced
            writer.write(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")  # command not supported
            await writer.drain()
            writer.close()
            return

        try:
            loop = asyncio.get_event_loop()
            infos = await loop.getaddrinfo(addr, port, type=socket.SOCK_STREAM)
        except OSError:
            infos = []
        resolved = {info[4][0] for info in infos}
        if not resolved or any(not _is_forwardable(ip) for ip in resolved):
            writer.write(b"\x05\x02\x00\x01\x00\x00\x00\x00\x00\x00")  # connection not allowed
            await writer.drain()
            writer.close()
            return

        try:
            remote_reader, remote_writer = await asyncio.wait_for(asyncio.open_connection(addr, port), timeout=15)
        except Exception:
            writer.write(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            writer.close()
            return

        writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        await writer.drain()

        async def pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            try:
                while True:
                    data = await r.read(65536)
                    if not data:
                        break
                    w.write(data)
                    await w.drain()
            except Exception:
                pass
            finally:
                w.close()

        await asyncio.gather(pipe(reader, remote_writer), pipe(remote_reader, writer), return_exceptions=True)
    except Exception:
        pass
    finally:
        writer.close()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=None, help="bind address (default: this machine's tailscale ip)")
    parser.add_argument("--port", type=int, default=11080)
    args = parser.parse_args()

    host = args.host or _detect_tailscale_ip()
    if not host or host in ("0.0.0.0", "::"):
        raise SystemExit(f"refusing to bind {host!r} — this relay is tailnet-only, never public")
    server = await asyncio.start_server(_handle, host, args.port)
    print(f"socks5 relay listening on {host}:{args.port}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
