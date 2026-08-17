"""
Real-time delivery layer. Knows nothing about market data specifically —
its only job is tracking connected clients and pushing whatever it's
given to all of them, cleaning up any that have dropped.
"""

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
        disconnected = []

        for client in self.clients:
            try:
                await client.send_json(data.model_dump(mode="json"))
            except Exception:
                disconnected.append(client)

        for client in disconnected:
            await self.disconnect(client)
