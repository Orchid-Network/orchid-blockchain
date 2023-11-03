from __future__ import annotations

import dataclasses
import logging
import traceback
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

import aiohttp
from blspy import AugSchemeMPL

from chia.types.blockchain_format.coin import Coin
from chia.types.coin_spend import CoinSpend
from chia.types.spend_bundle import SpendBundle
from chia.util.json_util import obj_to_response
from chia.wallet.conditions import Condition, ConditionValidTimes, conditions_from_json_dicts, parse_timelock_info
from chia.wallet.trade_record import TradeRecord
from chia.wallet.trading.offer import Offer
from chia.wallet.transaction_record import TransactionRecord
from chia.wallet.util.transaction_type import TransactionType
from chia.wallet.util.tx_config import TXConfig, TXConfigLoader

log = logging.getLogger(__name__)


def wrap_http_handler(f) -> Callable:
    async def inner(request) -> aiohttp.web.Response:
        request_data = await request.json()
        try:
            res_object = await f(request_data)
            if res_object is None:
                res_object = {}
            if "success" not in res_object:
                res_object["success"] = True
        except Exception as e:
            tb = traceback.format_exc()
            log.warning(f"Error while handling message: {tb}")
            if len(e.args) > 0:
                res_object = {"success": False, "error": f"{e.args[0]}", "traceback": f"{tb}"}
            else:
                res_object = {"success": False, "error": f"{e}"}

        return obj_to_response(res_object)

    return inner


def tx_endpoint(
    push: bool = False, merge_spends: bool = True
) -> Callable[[Callable[..., Coroutine[Any, Any, Dict[str, Any]]]], Callable[..., Coroutine[Any, Any, Dict[str, Any]]]]:
    def _inner(
        func: Callable[..., Coroutine[Any, Any, Dict[str, Any]]],
    ) -> Callable[..., Coroutine[Any, Any, Dict[str, Any]]]:
        async def rpc_endpoint(self, request: Dict[str, Any], *args, **kwargs) -> Dict[str, Any]:
            assert self.service.logged_in_fingerprint is not None
            tx_config_loader: TXConfigLoader = TXConfigLoader.from_json_dict(request)

            # Some backwards compat fill-ins
            if tx_config_loader.excluded_coin_ids is None:
                tx_config_loader = tx_config_loader.override(
                    excluded_coin_ids=request.get("exclude_coin_ids"),
                )
            if tx_config_loader.excluded_coin_amounts is None:
                tx_config_loader = tx_config_loader.override(
                    excluded_coin_amounts=request.get("exclude_coin_amounts"),
                )
            if tx_config_loader.excluded_coin_ids is None:
                excluded_coins: Optional[List[Coin]] = request.get("exclude_coins", request.get("excluded_coins"))
                if excluded_coins is not None:
                    tx_config_loader = tx_config_loader.override(
                        excluded_coin_ids=[Coin.from_json_dict(c).name() for c in excluded_coins],
                    )

            tx_config: TXConfig = tx_config_loader.autofill(
                constants=self.service.wallet_state_manager.constants,
                config=self.service.wallet_state_manager.config,
                logged_in_fingerprint=self.service.logged_in_fingerprint,
            )

            extra_conditions: Tuple[Condition, ...] = tuple()
            if "extra_conditions" in request:
                extra_conditions = tuple(conditions_from_json_dicts(request["extra_conditions"]))
            extra_conditions = (*extra_conditions, *ConditionValidTimes.from_json_dict(request).to_conditions())

            valid_times: ConditionValidTimes = parse_timelock_info(extra_conditions)
            if (
                valid_times.max_secs_after_created is not None
                or valid_times.min_secs_since_created is not None
                or valid_times.max_blocks_after_created is not None
                or valid_times.min_blocks_since_created is not None
            ):
                raise ValueError("Relative timelocks are not currently supported in the RPC")

            response: Dict[str, Any] = await func(
                self,
                request,
                *args,
                tx_config=tx_config,
                extra_conditions=extra_conditions,
                **kwargs,
            )

            if func.__name__ == "create_new_wallet" and "transactions" not in response:
                # unfortunately, this API isn't solely a tx endpoint
                return response
            elif func.__name__ == "nft_mint_bulk":
                # TODO: This API should return transaction records
                return response

            tx_records: List[TransactionRecord] = [
                TransactionRecord.from_json_dict_convenience(tx) for tx in response["transactions"]
            ]
            unsigned_txs = await self.service.wallet_state_manager.gather_signing_info(tx_records)

            if request.get("jsonify_unsigned_txs", False):
                response["unsigned_txs"] = [tx.to_json_dict() for tx in unsigned_txs]
            else:
                response["unsigned_txs"] = [bytes(tx.as_program()).hex() for tx in unsigned_txs]

            new_txs: List[TransactionRecord] = []
            if request.get("sign", self.service.config.get("auto_sign_txs", True)):
                new_txs, signing_responses = await self.service.wallet_state_manager.sign_transactions(
                    tx_records, response.get("signing_responses", []), "signing_responses" in response
                )
                response["transactions"] = [
                    TransactionRecord.to_json_dict_convenience(tx, self.service.config) for tx in new_txs
                ]
                response["signing_responses"] = [bytes(r.as_program()).hex() for r in signing_responses]
            else:
                new_txs = tx_records  # pragma: no cover

            if request.get("push", push):
                await self.service.wallet_state_manager.add_pending_transactions(
                    new_txs, merge_spends=merge_spends, sign=False
                )

            # Some backwards compatibility code
            if "transaction" in response:
                if (
                    func.__name__ == "create_new_wallet"
                    and request["wallet_type"] == "pool_wallet"
                    or func.__name__ == "pw_join_pool"
                    or func.__name__ == "pw_self_pool"
                    or func.__name__ == "pw_absorb_rewards"
                ):
                    # Theses RPCs return not "convenience" for some reason
                    response["transaction"] = new_txs[0].to_json_dict()
                else:
                    response["transaction"] = response["transactions"][0]
            if "tx_record" in response:
                response["tx_record"] = response["transactions"][0]
            if "fee_transaction" in response and response["fee_transaction"] is not None:
                # Theses RPCs return not "convenience" for some reason
                response["fee_transaction"] = new_txs[1].to_json_dict()
            if "transaction_id" in response:
                response["transaction_id"] = new_txs[0].name
            if "transaction_ids" in response:
                response["transaction_ids"] = [
                    tx.name.hex() for tx in new_txs if tx.type == TransactionType.OUTGOING_CLAWBACK.value
                ]
            if "spend_bundle" in response:
                response["spend_bundle"] = SpendBundle.aggregate(
                    [tx.spend_bundle for tx in new_txs if tx.spend_bundle is not None]
                )
            if "signed_txs" in response:
                response["signed_txs"] = response["transactions"]
            if "signed_tx" in response:
                response["signed_tx"] = response["transactions"][0]
            if "tx" in response:
                if func.__name__ == "send_notification":
                    response["tx"] = response["transactions"][0]
                else:
                    response["tx"] = new_txs[0].to_json_dict()
            if "tx_id" in response:
                response["tx_id"] = new_txs[0].name
            if "trade_record" in response:
                old_offer: Offer = Offer.from_bech32(response["offer"])
                signed_coin_spends: List[CoinSpend] = [
                    coin_spend
                    for tx in new_txs
                    if tx.spend_bundle is not None
                    for coin_spend in tx.spend_bundle.coin_spends
                ]
                involved_coins: List[Coin] = [spend.coin for spend in signed_coin_spends]
                signed_coin_spends.extend(
                    [spend for spend in old_offer._bundle.coin_spends if spend.coin not in involved_coins]
                )
                new_offer_bundle: SpendBundle = SpendBundle(
                    signed_coin_spends,
                    AugSchemeMPL.aggregate(
                        [tx.spend_bundle.aggregated_signature for tx in new_txs if tx.spend_bundle is not None]
                    ),
                )
                new_offer: Offer = Offer(old_offer.requested_payments, new_offer_bundle, old_offer.driver_dict)
                response["offer"] = new_offer.to_bech32()
                old_trade_record: TradeRecord = TradeRecord.from_json_dict_convenience(
                    response["trade_record"], bytes(old_offer).hex()
                )
                new_trade: TradeRecord = dataclasses.replace(
                    old_trade_record,
                    offer=bytes(new_offer),
                    trade_id=new_offer.name(),
                )
                response["trade_record"] = new_trade.to_json_dict_convenience()
                if (
                    await self.service.wallet_state_manager.trade_manager.trade_store.get_trade_record(
                        old_trade_record.trade_id
                    )
                    is not None
                ):
                    await self.service.wallet_state_manager.trade_manager.trade_store.delete_trade_record(
                        old_trade_record.trade_id
                    )
                    await self.service.wallet_state_manager.trade_manager.save_trade(new_trade, new_offer)
                for tx in await self.service.wallet_state_manager.tx_store.get_transactions_by_trade_id(
                    old_trade_record.trade_id
                ):
                    await self.service.wallet_state_manager.tx_store.add_transaction_record(
                        dataclasses.replace(tx, trade_id=new_trade.trade_id)
                    )

            return response

        return rpc_endpoint

    return _inner
