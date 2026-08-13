"""Import external statistics into Home Assistant via WebSocket API."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import websockets


@dataclass(frozen=True)
class StatisticEntry:
    start: datetime  # tz-aware; HA requires timezone
    state: float  # value for this interval (kWh for energy)
    sum: float  # cumulative since epoch of the source


class HAClient:
    """Async client for Home Assistant's `recorder/import_statistics` WebSocket message.

    Two constructor entry points:
      - `for_supervisor()` — inside an add-on container, uses
        `ws://supervisor/core/websocket` + SUPERVISOR_TOKEN env var.
      - `HAClient(url, token)` — direct, e.g. `ws://homeassistant.local:8123/api/websocket`.
    """

    def __init__(self, url: str, token: str):
        self._url = url
        self._token = token
        self._msg_id = 0
        self._ws: websockets.ClientConnection | None = None

    @classmethod
    def for_supervisor(cls) -> "HAClient":
        import os

        token = os.environ["SUPERVISOR_TOKEN"]
        return cls("ws://supervisor/core/websocket", token)

    async def __aenter__(self) -> "HAClient":
        self._ws = await websockets.connect(self._url, max_size=32 * 1024 * 1024)
        hello = json.loads(await self._ws.recv())
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"Unexpected HA hello: {hello}")
        await self._ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        auth_result = json.loads(await self._ws.recv())
        if auth_result.get("type") != "auth_ok":
            raise RuntimeError(f"HA auth failed: {auth_result}")
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def _call(self, payload: dict[str, Any]) -> dict[str, Any]:
        assert self._ws is not None, "Use as async context manager"
        msg_id = self._next_id()
        payload = {"id": msg_id, **payload}
        await self._ws.send(json.dumps(payload))
        while True:
            reply = json.loads(await self._ws.recv())
            if reply.get("id") == msg_id:
                if not reply.get("success", False):
                    raise RuntimeError(f"HA WS call failed: {reply.get('error')}")
                return reply

    async def import_statistics(
        self,
        *,
        statistic_id: str,
        name: str,
        unit: str,
        source: str,
        stats: list[StatisticEntry],
    ) -> None:
        """Import a batch of statistics. Idempotent by (statistic_id, start)."""
        if ":" not in statistic_id:
            raise ValueError("External statistic_id must contain ':' (e.g. 'pacificpower:electric_consumption')")

        await self._call(
            {
                "type": "recorder/import_statistics",
                "metadata": {
                    "has_mean": False,
                    "has_sum": True,
                    "name": name,
                    "source": source,
                    "statistic_id": statistic_id,
                    "unit_of_measurement": unit,
                },
                "stats": [
                    {
                        "start": s.start.isoformat(),
                        "state": s.state,
                        "sum": s.sum,
                    }
                    for s in stats
                ],
            }
        )

    async def clear_statistics(self, statistic_ids: list[str]) -> None:
        """Delete all stored points for the given external statistic ids.
        Used before a fresh backfill so mixed-granularity leftovers don't
        double-count."""
        if not statistic_ids:
            return
        await self._call({
            "type": "recorder/clear_statistics",
            "statistic_ids": statistic_ids,
        })

    async def touch_heartbeat(self, entity_id: str) -> None:
        """Set an input_datetime helper to now. Used as a heartbeat so a
        template binary_sensor + alert in HA can detect a stale importer.
        No-op (logs only) if the helper doesn't exist — user hasn't set
        it up yet."""
        from datetime import datetime, timezone
        try:
            await self._call({
                "type": "call_service",
                "domain": "input_datetime",
                "service": "set_datetime",
                "target": {"entity_id": entity_id},
                "service_data": {
                    "datetime": datetime.now(timezone.utc).astimezone().isoformat(),
                },
            })
        except Exception as e:
            # Very common: helper not created yet. Don't fail the import.
            import logging
            logging.getLogger(__name__).info(
                "heartbeat skipped (%s not found or service failed): %s",
                entity_id, e,
            )

    async def list_statistic_ids(self, statistic_type: str = "sum") -> list[dict[str, Any]]:
        """List known statistic ids (for verification)."""
        reply = await self._call(
            {
                "type": "recorder/list_statistic_ids",
                "statistic_type": statistic_type,
            }
        )
        return reply.get("result", [])
