import asyncio
import base64
import httpx
import redis.asyncio as redis
import logging
import os
import time

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ton_engine")

API = "https://toncenter.com/api/v3"


class TonPaymentEngine:

    def __init__(self, redis_client):

        self.redis = redis_client

        self.client = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=0,
                keepalive_expiry=0
            ),
            timeout=10
        )

    async def masterchain_seqno(self):

        r = await self.client.get(f"{API}/masterchainInfo")
        r.raise_for_status()

        return r.json()["last"]["seqno"]

    async def block_transactions(self, seqno):

        r = await self.client.get(
            f"{API}/transactionsByMasterchainBlock",
            params={"seqno": seqno}
        )

        r.raise_for_status()

        return r.json()["transactions"]

    async def tx_seen(self, tx_hash):

        return await self.redis.set(
            f"ton:tx:seen:{tx_hash}",
            1,
            nx=True,
            ex=365 * 86400
        )

    async def tracked_wallet(self, wallet):

        return await self.redis.sismember(
            "ton:wallets",
            wallet
        )

    def extract_comment(self, msg):

        data = msg.get("msg_data")

        if not data:
            return None

        try:

            if data["@type"] == "msg.dataText":

                return base64.b64decode(
                    data.get("text", "")
                ).decode(errors="ignore").strip()

            if data["@type"] == "msg.dataRaw":

                body = base64.b64decode(
                    data.get("body", "")
                )

                if body[:4] == b"\x00\x00\x00\x00":
                    body = body[4:]

                return body.decode(errors="ignore").strip()

        except Exception:
            return None

    async def process_tx(self, tx):

        in_msg = tx.get("in_msg")

        if not in_msg:
            return

        wallet = in_msg.get("destination")

        if not wallet:
            return

        if not await self.tracked_wallet(wallet):
            return

        tx_hash = base64.b64decode(tx["hash"]).hex()

        if not await self.tx_seen(tx_hash):
            return

        value = int(in_msg.get("value") or 0)

        if value <= 0:
            return

        amount = value / 1e9

        comment = self.extract_comment(in_msg)

        await self.redis.xadd(
            "ton:payments",
            {
                "wallet": wallet,
                "amount": amount,
                "comment": comment or "",
                "tx": tx_hash,
                "ts": int(time.time())
            }
        )

    async def scan_block(self, seqno):

        txs = await self.block_transactions(seqno)

        tasks = []

        for tx in txs:
            tasks.append(self.process_tx(tx))

        await asyncio.gather(*tasks)

    async def run(self):

        last = await self.redis.get("ton:block:last")

        if last:
            last = int(last)
        else:
            last = await self.masterchain_seqno()

        while True:

            latest = await self.masterchain_seqno()

            while last < latest:

                last += 1

                lock = await self.redis.set(
                    f"ton:block:lock:{last}",
                    1,
                    nx=True,
                    ex=60
                )

                if not lock:
                    continue

                log.info("Scanning block %s", last)

                await self.scan_block(last)

                await self.redis.set("ton:block:last", last)

            await asyncio.sleep(1)
