"""Print the first N messages from Coinbase's public feed, to check protocol assumptions.

uv run arb-probe-cb --product BTC-USDC --n 30
"""

import argparse
import asyncio
import json

from websockets.asyncio.client import connect

URL = "wss://advanced-trade-ws.coinbase.com"


async def main(product: str, n: int) -> None:
  async with connect(URL, max_size=32 * 1024 * 1024) as ws:
    await ws.send(json.dumps({"type": "subscribe", "channel": "level2", "product_ids": [product]}))
    await ws.send(json.dumps({"type": "subscribe", "channel": "heartbeats"}))
    for i in range(n):
      raw = await asyncio.wait_for(ws.recv(), 15)
      msg = json.loads(raw)
      events = msg.get("events") or []
      if msg.get("channel") == "l2_data":
        summary = [(e.get("type"), e.get("product_id"), len(e.get("updates", []))) for e in events]
      else:
        summary = raw[:300]
      print(f"{i:3d} ch={msg.get('channel')} seq={msg.get('sequence_num')} ts={msg.get('timestamp')} {summary}")


def cli() -> None:
  ap = argparse.ArgumentParser()
  ap.add_argument("--product", default="BTC-USDC")
  ap.add_argument("--n", type=int, default=30)
  a = ap.parse_args()
  asyncio.run(main(a.product, a.n))


if __name__ == "__main__":
  cli()
