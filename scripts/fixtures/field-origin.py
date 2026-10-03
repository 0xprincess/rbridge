#!/usr/bin/env python3
"""Disposable field-test origin: real TCP, HTTP, HTTPS, streaming and WebSockets."""
import asyncio
import json
import ssl

from aiohttp import web


async def echo(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def app(request):
    if request.host.startswith("app.") and request.headers.get("Authorization") != "Bearer application-owned":
        return web.Response(status=401, text="application authentication required")
    if request.path == "/ws":
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            if message.type == web.WSMsgType.TEXT:
                await ws.send_str(message.data)
        return ws
    if request.path == "/stream":
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for index in range(3):
            await response.write(f"data: {index}\n\n".encode())
            await asyncio.sleep(0.5)
        await response.write_eof()
        return response
    return web.Response(text=json.dumps({"fixture": "frp-field-origin", "headers": dict(request.headers)}),
                        content_type="application/json")


async def main():
    application = web.Application()
    application.router.add_route("*", "/{path:.*}", app)
    runner = web.AppRunner(application)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 18080).start()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain("/opt/frp-field/origin.crt", "/opt/frp-field/origin.key")
    await web.TCPSite(runner, "127.0.0.1", 18443, ssl_context=context).start()
    server = await asyncio.start_server(echo, "127.0.0.1", 18081)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
