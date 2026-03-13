import asyncio
import logging
import httpx
import time
import base64

from config import TON_WALLET, TONCENTER_API_KEY, TON_ENABLED
from hybrid.plugins.ton_pay import (
    match_ton_payment,
    process_matched_ton_payment,
    get_ton_order,
    handle_ton_payment_confirmed,
)

log = logging.getLogger("rtce")


class TonPaymentEngine:
    API_URL = "https://toncenter.com/api/v3/transactions"

    def __init__(self, redis_client, bot_client):
        self.redis = redis_client
        self.bot_client = bot_client
        self.running = False

        self.client = httpx.AsyncClient(
            http2=True,
            timeout=10,
            limits=httpx.Limits(
                max_keepalive_connections=0,
                keepalive_expiry=0
            ),
        )

        self.last_lt_key = f"ton:last_lt:{TON_WALLET}"
        self.last_hash_key = f"ton:last_hash:{TON_WALLET}"

    async def _get_state(self):
        lt = await self.redis.get(self.last_lt_key)
        tx_hash = await self.redis.get(self.last_hash_key)

        if lt:
            lt = int(lt)

        if tx_hash:
            tx_hash = tx_hash.decode() if isinstance(tx_hash, bytes) else tx_hash

        return lt, tx_hash

    async def _save_state(self, lt, tx_hash):
        await self.redis.set(self.last_lt_key, lt)
        await self.redis.set(self.last_hash_key, tx_hash)

    async def _fetch_transactions(self):
        headers = {}
        if TONCENTER_API_KEY:
            headers["X-API-Key"] = TONCENTER_API_KEY

        params = {
            "account": TON_WALLET,
            "limit": 20,
            "sort": "desc",
        }

        r = await self.client.get(self.API_URL, params=params, headers=headers)

        if r.status_code == 429:
            log.warning("[RTCE] Rate limited — sleeping 10s")
            await asyncio.sleep(10)
            return []

        r.raise_for_status()

        data = r.json()
        return data.get("transactions", [])

    def _extract_comment(self, msg):
        try:
            content = msg.get("message_content")
            if not content:
                return None

            decoded = content.get("decoded")
            if decoded and isinstance(decoded, dict):
                comment = decoded.get("comment")
                if comment:
                    return comment.strip()

            body = content.get("body")
            if body:
                raw = base64.b64decode(body)

                if len(raw) >= 4 and raw[:4] == b"\x00\x00\x00\x00":
                    raw = raw[4:]

                return raw.decode("utf-8", errors="ignore").strip()

        except Exception:
            return None

        return None

    async def _process_tx(self, tx):
        try:
            tx_hash = tx.get("hash")
            lt = int(tx.get("lt"))

            if not tx_hash:
                return None

            processed = await self.redis.sismember("ton_tx:processed", tx_hash)
            if processed:
                return None

            in_msg = tx.get("in_msg")
            if not in_msg:
                return None

            dest = in_msg.get("destination")
            if dest != TON_WALLET:
                return None

            value = int(in_msg.get("value") or 0)
            if value <= 0:
                return None

            amount = value / 1e9
            comment = self._extract_comment(in_msg)

            log.info(
                "[RTCE] Payment detected: amount=%.4f comment=%s hash=%s",
                amount,
                comment or "",
                tx_hash,
            )

            active_orders = await self.redis.smembers("ton_orders:active")
            if not active_orders:
                return lt

            order_id = await match_ton_payment(
                {"tx_hash": tx_hash, "amount": amount, "comment": comment}
            )

            if not order_id:
                return lt

            await self.redis.sadd("ton_tx:processed", tx_hash)
            await self._save_state(lt, tx_hash)

            await process_matched_ton_payment(
                order_id, tx_hash, amount, self.bot_client
            )

            order = await get_ton_order(order_id)

            if order and order.get("status") == "paid":
                await handle_ton_payment_confirmed(self.bot_client, order)

                log.info(
                    "[RTCE] Payment confirmed: order=%s amount=%.4f",
                    order_id,
                    amount,
                )

            return lt

        except Exception:
            log.exception("[RTCE] Transaction processing failed")

    async def start(self):
        if not TON_ENABLED:
            log.info("[RTCE] TON payments disabled — engine not started")
            return

        self.running = True
        backoff = 2

        last_lt, _ = await self._get_state()

        log.info("[RTCE] TON payment engine started")

        while self.running:
            try:
                txs = await self._fetch_transactions()

                if not txs:
                    await asyncio.sleep(3)
                    backoff = 2
                    continue

                txs.sort(key=lambda x: int(x.get("lt", 0)))

                for tx in txs:
                    lt = int(tx.get("lt", 0))
                    if last_lt and lt <= last_lt:
                        continue
                    try:
                        new_lt = await self._process_tx(tx)
                        if new_lt and (not last_lt or new_lt > last_lt):
                            last_lt = new_lt
                    except Exception:
                        log.exception("[RTCE] tx processing failed")

                backoff = 2
                await asyncio.sleep(3)

            except Exception:
                log.exception("[RTCE] Engine error — backing off %ss", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def stop(self):
        self.running = False
        await self.client.aclose()
        log.info("[RTCE] TON payment engine stopped")
