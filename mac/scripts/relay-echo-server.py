#!/usr/bin/env python3
"""Loopback server for the Speakeasy relay call contract (Grok Voice transport).

Run: python3 relay-echo-server.py [port]   (needs the `websockets` package)
Then: Speakeasy --relay-smoke ws://127.0.0.1:<port>/voice/interactions/vi_smoke/audio

Echoes the app's mic PCM (binary frames, PCM16LE 24 kHz mono) back as assistant audio after
0.5 s, sends {"type":"speech_started"} every 1.5 s (barge-in: the app must flush playback) and
{"type":"response_done"} after it, and answers {"type":"end"} with {"type":"closed"}. Checks the
bearer header is present. Prints a summary per connection.
"""
import asyncio
import json
import sys
import time

import websockets


async def handle(ws):
    headers = getattr(ws, "request", None)
    headers = headers.headers if headers is not None else ws.request_headers
    auth = headers.get("Authorization", "")
    path = getattr(getattr(ws, "request", None), "path", None) or getattr(ws, "path", "")
    stats = {"path": path, "bearer": auth.startswith("Bearer "), "rx_bytes": 0, "rx_frames": 0,
             "tx_bytes": 0, "tx_frames": 0, "control_in": [], "speech_started_sent": 0}
    if not auth.startswith("Bearer "):
        await ws.close(code=4401, reason="no bearer")
        return
    started = time.monotonic()
    done = asyncio.Event()

    async def echo_later(chunk):
        await asyncio.sleep(0.5)
        if done.is_set():
            return
        try:
            await ws.send(chunk)
            stats["tx_bytes"] += len(chunk)
            stats["tx_frames"] += 1
        except websockets.ConnectionClosed:
            pass

    async def barge_in():
        while not done.is_set():
            await asyncio.sleep(1.5)
            if done.is_set():
                return
            try:
                await ws.send(json.dumps({"type": "speech_started"}))
                stats["speech_started_sent"] += 1
                await asyncio.sleep(0.1)
                await ws.send(json.dumps({"type": "response_done"}))
            except websockets.ConnectionClosed:
                return

    timer = asyncio.create_task(barge_in())
    try:
        async for message in ws:
            if isinstance(message, bytes):
                stats["rx_bytes"] += len(message)
                stats["rx_frames"] += 1
                asyncio.create_task(echo_later(message))
            else:
                stats["control_in"].append(message)
                if json.loads(message).get("type") == "end":
                    done.set()
                    await ws.send(json.dumps({"type": "closed", "reason": "ended"}))
                    await ws.close()
                    break
    except websockets.ConnectionClosed:
        pass
    finally:
        done.set()
        timer.cancel()
        stats["seconds"] = round(time.monotonic() - started, 2)
        print("relay-echo " + json.dumps(stats), flush=True)


async def main(port):
    async with websockets.serve(handle, "127.0.0.1", port, max_size=4 << 20):
        print(f"relay-echo listening on ws://127.0.0.1:{port}", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 8799))
