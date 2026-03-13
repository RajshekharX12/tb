import asyncio
import httpx
import logging
import os
import base64
from typing import Callable, Awaitable, Optional

log = logging.getLogger("ton_watcher")


class TonPaymentWatcher:
    API_URL = "https://toncenter.com/api/v2/getTransactions"

    def __init__(self, redis, wallet: Optional[str] = None, poll_interval: float = 2.0):
        self.redis = redis
        self.wallet = wallet or os.getenv("TON_WALLET")
        self.api_key = os.getenv("TONCENTER_KEY")

        if not self.wallet:
            raise ValueError("TON_WALLET env variable missing")

        self.poll_interval = poll_interval
        self.running = False

        limits = httpx.Limits(
            max_connections=50,
            max_keepalive_connections=20,
            keepalive_expiry=30,
        )

        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(10),
            limits=limits,
            http2=True,
        )

        self.last_lt_key = f"ton:last_lt:{self.wallet}"
        self.last_hash_key = f"ton:last_hash:{self.wallet}"

    async def _get_state(self):
        lt = await self.redis.get(self.last_lt_key)
        tx_hash = await self.redis.get(self.last_hash_key)

        if lt:
            lt = int(lt)

        if tx_hash:
            tx_hash = tx_hash.decode()

        return lt, tx_hash

    async def _save_state(self, lt: int, tx_hash: str):
        await self.redis.set(self.last_lt_key, lt)
        await self.redis.set(self.last_hash_key, tx_hash)

    async def _fetch_transactions(self, limit=20):
        headers = {}
        params = {
            "address": self.wallet,
            "limit": limit,
        }

        if self.api_key:
            headers["X-API-Key"] = self.api_key

        r = await self.client.get(self.API_URL, params=params, headers=headers)
        r.raise_for_status()

        data = r.json()

        if not data.get("ok"):
            raise RuntimeError("TONCenter returned error")

        return data["result"]

    async def _tx_lock(self, tx_hash: str):
        key = f"ton:txlock:{tx_hash}"
        return await self.redis.set(key, "1", nx=True, ex=86400)

    def _extract_comment(self, msg):
        try:
            if not msg:
                return None

            data = msg.get("msg_data")
            if not data:
                return None

            if data["@type"] == "msg.dataText":
                return data.get("text")

            if data["@type"] == "msg.dataRaw":
                decoded = base64.b64decode(data["body"])
                return decoded.decode(errors="ignore")

        except Exception:
            return None

    def _extract_amount(self, msg):
        value = int(msg.get("value", 0))
        return value / 1e9

    async def _process_tx(self, tx, callback):
        try:
            tx_hash = tx["transaction_id"]["hash"]
            lt = int(tx["transaction_id"]["lt"])

            in_msg = tx.get("in_msg")

            if not in_msg:
                return None

            if not await self._tx_lock(tx_hash):
                return None

            amount = self._extract_amount(in_msg)
            comment = self._extract_comment(in_msg)

            await callback(amount, comment, tx_hash)

            await self._save_state(lt, tx_hash)

            return lt

        except Exception:
            log.exception("TX processing failed")

    async def _bootstrap(self):
        """
        Prevent historical replay on first start
        """
        last_lt, _ = await self._get_state()

        if last_lt:
            return last_lt

        txs = await self._fetch_transactions(limit=1)

        if not txs:
            return None

        tx = txs[0]

        lt = int(tx["transaction_id"]["lt"])
        tx_hash = tx["transaction_id"]["hash"]

        await self._save_state(lt, tx_hash)

        log.info("Watcher bootstrap complete at lt=%s", lt)

        return lt

    async def start(self, callback: Callable[[float, str, str], Awaitable[None]]):
        self.running = True

        last_lt = await self._bootstrap()

        log.info("TON watcher running for %s", self.wallet)

        backoff = 2

        while self.running:
            try:
                txs = await self._fetch_transactions()

                if not txs:
                    await asyncio.sleep(self.poll_interval)
                    continue

                txs.sort(key=lambda x: int(x["transaction_id"]["lt"]))

                for tx in txs:
                    lt = int(tx["transaction_id"]["lt"])

                    if last_lt and lt <= last_lt:
                        continue

                    new_lt = await self._process_tx(tx, callback)

                    if new_lt:
                        last_lt = new_lt

                backoff = 2
                await asyncio.sleep(self.poll_interval)

            except httpx.HTTPError as e:
                log.error("TON API HTTP error: %s", e)

                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

            except Exception:
                log.exception("TON watcher error")

                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def stop(self):
        self.running = False
        await self.client.aclose()
