"""Asynchronous HTTP & WebSocket server for the Sesame Hardware Simulation GUI."""

import asyncio
import json
import logging
import os
from aiohttp import web
from .device_firmware import SimulatedSesame6Pro, SimulatedSesameTouch2Pro

logger = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "web")


class SimulationServer:
    """Hosts the Web GUI and WebSocket bridge for the virtual hardware simulation."""

    def __init__(
        self,
        lock: SimulatedSesame6Pro | None = None,
        keypad: SimulatedSesameTouch2Pro | None = None,
        host: str = "127.0.0.1",
        port: int = 8088,
    ) -> None:
        self.lock = lock
        self.keypad = keypad
        self.host = host
        self.port = port
        self.app = web.Application()
        self.sockets: set[web.WebSocketResponse] = set()
        self.event_log: list[dict] = []

        # Cross-link keypad and lock so unlocking keypad unlocks lock
        if self.lock and self.keypad:
            self.keypad.linked_lock = self.lock

        self._setup_routes()
        self._setup_device_listeners()

    def _setup_routes(self) -> None:
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/ws", self.handle_ws)
        self.app.router.add_get("/api/state", self.handle_get_state)
        self.app.router.add_post("/api/lock/manual_angle", self.handle_manual_angle)
        self.app.router.add_post("/api/lock/lock", self.handle_lock_cmd)
        self.app.router.add_post("/api/lock/unlock", self.handle_unlock_cmd)
        self.app.router.add_post("/api/keypad/press", self.handle_key_press)
        self.app.router.add_post("/api/keypad/fingerprint", self.handle_fingerprint)
        self.app.router.add_post("/api/keypad/card", self.handle_card)
        self.app.router.add_static("/static", STATIC_DIR)

    def _setup_device_listeners(self) -> None:
        if self.lock:
            self.lock.add_state_listener(lambda state: self.broadcast_state("lock", state))
        if self.keypad:
            self.keypad.add_state_listener(lambda state: self.broadcast_state("keypad", state))

    async def handle_index(self, request: web.Request) -> web.Response:
        index_path = os.path.join(STATIC_DIR, "index.html")
        if os.path.exists(index_path):
            return web.FileResponse(index_path)
        return web.Response(text="Simulation Web GUI index.html missing", status=404)

    async def handle_get_state(self, request: web.Request) -> web.Response:
        state = {
            "lock": self.lock.get_state() if self.lock else None,
            "keypad": self.keypad.get_state() if self.keypad else None,
            "log": self.event_log[-50:],
        }
        return web.json_response(state)

    async def handle_manual_angle(self, request: web.Request) -> web.Response:
        if not self.lock:
            return web.json_response({"error": "No lock simulated"}, status=400)
        data = await request.json()
        angle = float(data.get("angle", 0.0))
        self.lock.set_manual_angle(angle)
        self.log_event("Sesame 6 Pro", f"Manual thumbturn turned to {angle:.1f}°")
        return web.json_response({"status": "ok", "angle": self.lock.current_angle})

    async def handle_lock_cmd(self, request: web.Request) -> web.Response:
        if not self.lock:
            return web.json_response({"error": "No lock simulated"}, status=400)
        self.lock.lock()
        self.log_event("Sesame 6 Pro", "Lock command issued via GUI")
        return web.json_response({"status": "ok"})

    async def handle_unlock_cmd(self, request: web.Request) -> web.Response:
        if not self.lock:
            return web.json_response({"error": "No lock simulated"}, status=400)
        self.lock.unlock()
        self.log_event("Sesame 6 Pro", "Unlock command issued via GUI")
        return web.json_response({"status": "ok"})

    async def handle_key_press(self, request: web.Request) -> web.Response:
        if not self.keypad:
            return web.json_response({"error": "No keypad simulated"}, status=400)
        data = await request.json()
        key = str(data.get("key", ""))
        self.keypad.press_key(key)
        self.log_event("Touch 2 Pro", f"Keypad button '{key}' pressed")
        return web.json_response({"status": "ok", "buffer": self.keypad.keypad_input})

    async def handle_fingerprint(self, request: web.Request) -> web.Response:
        if not self.keypad:
            return web.json_response({"error": "No keypad simulated"}, status=400)
        data = await request.json()
        match = bool(data.get("match", True))
        self.keypad.scan_fingerprint(matched=match)
        self.log_event("Touch 2 Pro", f"Fingerprint sensor scanned (matched={match})")
        return web.json_response({"status": "ok"})

    async def handle_card(self, request: web.Request) -> web.Response:
        if not self.keypad:
            return web.json_response({"error": "No keypad simulated"}, status=400)
        data = await request.json()
        uid = str(data.get("uid", "E004010203040506"))
        self.keypad.scan_card(uid)
        self.log_event("Touch 2 Pro", f"NFC Card scanned (UID={uid})")
        return web.json_response({"status": "ok"})

    def log_event(self, source: str, message: str) -> None:
        import datetime
        item = {
            "time": datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "source": source,
            "message": message,
        }
        self.event_log.append(item)
        if len(self.event_log) > 200:
            self.event_log.pop(0)
        asyncio.create_task(self.broadcast({"type": "log", "data": item}))

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.add(ws)
        logger.info("GUI WebSocket client connected (%d active)", len(self.sockets))

        # Send initial full snapshot
        state = {
            "type": "init",
            "lock": self.lock.get_state() if self.lock else None,
            "keypad": self.keypad.get_state() if self.keypad else None,
            "log": self.event_log[-50:],
        }
        await ws.send_str(json.dumps(state))

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        await self._process_ws_command(data)
                    except Exception as err:
                        logger.error("Error processing WS msg: %s", err)
        finally:
            self.sockets.discard(ws)
            logger.info("GUI WebSocket client disconnected (%d remaining)", len(self.sockets))

        return ws

    async def _process_ws_command(self, data: dict) -> None:
        cmd = data.get("action")
        if cmd == "manual_angle" and self.lock:
            self.lock.set_manual_angle(float(data.get("angle", 0.0)))
        elif cmd == "lock" and self.lock:
            self.lock.lock()
        elif cmd == "unlock" and self.lock:
            self.lock.unlock()
        elif cmd == "key_press" and self.keypad:
            self.keypad.press_key(str(data.get("key", "")))
        elif cmd == "fingerprint" and self.keypad:
            self.keypad.scan_fingerprint(bool(data.get("match", True)))
        elif cmd == "card" and self.keypad:
            self.keypad.scan_card(str(data.get("uid", "E004010203040506")))

    def broadcast_state(self, device_type: str, state: dict) -> None:
        msg = {"type": "state_update", "device": device_type, "state": state}
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.broadcast(msg))
        except RuntimeError:
            pass

    async def broadcast(self, message: dict) -> None:
        if not self.sockets:
            return
        msg_str = json.dumps(message)
        dead = set()
        for ws in self.sockets:
            try:
                await ws.send_str(msg_str)
            except Exception:
                dead.add(ws)
        self.sockets.difference_update(dead)

    async def start(self) -> web.AppRunner:
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        logger.info("==========================================================")
        logger.info("  SESAME HARDWARE SIMULATOR GUI ACTIVE")
        logger.info("  Open http://%s:%d in your browser", self.host, self.port)
        logger.info("==========================================================")
        return runner
