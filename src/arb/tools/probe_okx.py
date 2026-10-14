"""Print the first N messages from OKX's public books channel, to check protocol assumptions.

uv run arb-probe-okx --inst BTC-USDC --n 30
"""

import argparse
import asyncio
import json

from websockets.asyncio.client import connect

URL = "wss://ws.okx.com:8443/ws/v5/public"


async def main(inst: str, n: int, channel: str) -> None:
  async with connect(URL, max_size=32 * 1024 * 1024, ping_interval=None) as ws:
    await ws.send(json.dumps({"op": "subscribe", "args": [{"channel": channel, "instId": inst}]}))
    for i in range(n):
      raw = await asyncio.wait_for(ws.recv(), 15)
      msg = json.loads(raw)
      if "data" in msg:
        d = msg["data"][0]
        summary = (
          f"action={msg.get('action')} seqId={d.get('seqId')} prevSeqId={d.get('prevSeqId')} "
          f"bids={len(d.get('bids', []))} asks={len(d.get('asks', []))} checksum={d.get('checksum')}"
        )
      else:
        summary = raw[:300]
      print(f"{i:3d} {summary}")


def cli() -> None:
  ap = argparse.ArgumentParser()
  ap.add_argument("--inst", default="BTC-USDC")
  ap.add_argument("--channel", default="books")
  ap.add_argument("--n", type=int, default=30)
  a = ap.parse_args()
  asyncio.run(main(a.inst, a.n, a.channel))


if __name__ == "__main__":
  cli()
