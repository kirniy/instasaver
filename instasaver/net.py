"""Public-only, DNS-pinned HTTP proxy for all downloader requests."""
import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


def validate_url(url):
    u = urlsplit(url)
    if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password:
        raise ValueError('Нужна обычная ссылка http:// или https://.')
    if u.port not in (None, 80, 443) or any(ord(c) < 33 for c in url):
        raise ValueError('Эту ссылку нельзя открыть.')
    host = u.hostname.lower().rstrip('.')
    if host == 'localhost' or host.endswith(('.local', '.internal', '.localhost')):
        raise ValueError('Локальные адреса не поддерживаются.')
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not ip.is_global:
            raise ValueError('Локальные адреса не поддерживаются.')
    return url


async def public_addresses(host, port):
    if port not in (80, 443):
        raise ValueError('Port blocked')
    addresses = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
        raise ValueError('Non-public destination blocked')
    return sorted(set(x[4][0] for x in addresses),key=lambda value:ipaddress.ip_address(value).version)


async def public_address(host,port):
    return (await public_addresses(host,port))[0]


async def pipe(reader, writer):
    while data := await reader.read(65536):
        writer.write(data)
        await writer.drain()


async def proxy_client(reader, writer):
    remote = None
    try:
        header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 20)
        lines = header.decode('latin1').split('\r\n')
        method, target, version = lines[0].split(' ', 2)
        if method == 'CONNECT':
            u = urlsplit('https://' + target)
            host, port = u.hostname, u.port or 443
        else:
            validate_url(target)
            u = urlsplit(target)
            if u.scheme != 'http':
                raise ValueError('TLS must use CONNECT')
            host, port = u.hostname, u.port or 80
        for address in await public_addresses(host,port):
            try:
                rr,remote=await asyncio.wait_for(asyncio.open_connection(address,port),6)
                break
            except (OSError,asyncio.TimeoutError):
                continue
        if remote is None:
            raise OSError('Destination unavailable')
        if method == 'CONNECT':
            writer.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
        else:
            path = (u.path or '/') + ('?' + u.query if u.query else '')
            headers = [l for l in lines[1:] if l and not l.lower().startswith(('proxy-', 'connection:', 'host:'))]
            request = f'{method} {path} {version}\r\nHost: {u.netloc}\r\n' + '\r\n'.join(headers) + '\r\nConnection: close\r\n\r\n'
            remote.write(request.encode('latin1'))
            await remote.drain()
        await writer.drain()
        tasks = [asyncio.create_task(pipe(reader, remote)), asyncio.create_task(pipe(rr, writer))]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED, timeout=1800)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    except (Exception, asyncio.CancelledError):
        pass
    finally:
        writer.close()
        if remote:
            remote.close()


async def start_proxy():
    server = await asyncio.start_server(proxy_client, '127.0.0.1', 0, limit=65536)
    return server, f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
