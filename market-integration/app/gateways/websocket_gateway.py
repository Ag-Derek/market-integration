"""
Real-time delivery layer. Knows nothing about market data specifically —
its only job is tracking connected clients and pushing whatever it's
given to all of them, cleaning up any that have dropped.
"""

import asyncio

from fastapi import WebSocket


class WebSocketGateway:

    def __init__(self):
        self.clients: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.clients.add(websocket)
        print(f"Client connected. Total clients: {len(self.clients)}")

    async def disconnect(self, websocket: WebSocket) -> None:
        self.clients.discard(websocket)
        print(f"Client disconnected. Total clients: {len(self.clients)}")

    async def broadcast(self, data) -> None:
        # Fan out concurrently -- sequential awaits here would let one
        # slow/stalled client delay delivery to every other client and,
        # since the caller awaits this before pulling the next item off
        # the feed, back up ingestion for all symbols.
        payload = data.model_dump(mode="json")
        results = await asyncio.gather(
            *(client.send_json(payload) for client in self.clients),
            return_exceptions=True,
        )

        disconnected = [
            client
            for client, result in zip(self.clients, results)
            if isinstance(result, Exception)
        ]
        for client in disconnected:
            await self.disconnect(client)
