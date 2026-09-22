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
        #
        # Snapshot self.clients once: a client's own handler task can call
        # disconnect() concurrently while we're suspended on gather() below
        # (each websocket connection runs in its own task). Re-reading
        # self.clients afterwards would zip a possibly-mutated set against
        # results computed from the original one, pairing the wrong client
        # with the wrong outcome.
        payload = data.model_dump(mode="json")
        clients = list(self.clients)
        results = await asyncio.gather(
            *(client.send_json(payload) for client in clients),
            return_exceptions=True,
        )

        disconnected = [
            client
            for client, result in zip(clients, results)
            if isinstance(result, Exception)
        ]
        for client in disconnected:
            await self.disconnect(client)
