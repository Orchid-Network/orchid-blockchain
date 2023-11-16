from __future__ import annotations

import dataclasses
import json
import logging
import zlib
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, Set, Tuple, Union

from chia_rs import AugSchemeMPL, G1Element, G2Element, PrivateKey
from clvm_tools.binutils import assemble

from chia.consensus.block_rewards import calculate_base_farmer_reward
from chia.data_layer.data_layer_errors import LauncherCoinNotFoundError
from chia.data_layer.data_layer_util import dl_verify_proof
from chia.data_layer.data_layer_wallet import DataLayerWallet
from chia.pools.pool_wallet import PoolWallet
from chia.pools.pool_wallet_info import FARMING_TO_POOL, PoolState, PoolWalletInfo, create_pool_state
from chia.protocols.protocol_message_types import ProtocolMessageTypes
from chia.protocols.wallet_protocol import CoinState
from chia.rpc.rpc_server import Endpoint, EndpointResult, default_get_connections
from chia.rpc.util import tx_endpoint
from chia.server.outbound_message import NodeType, make_msg
from chia.server.ws_connection import WSChiaConnection
from chia.simulator.simulator_protocol import FarmNewBlockProtocol
from chia.types.announcement import Announcement
from chia.types.blockchain_format.coin import Coin, coin_as_list
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_record import CoinRecord
from chia.types.coin_spend import CoinSpend
from chia.types.signing_mode import CHIP_0002_SIGN_MESSAGE_PREFIX, SigningMode
from chia.types.spend_bundle import SpendBundle
from chia.util.bech32m import decode_puzzle_hash, encode_puzzle_hash
from chia.util.byte_types import hexstr_to_bytes
from chia.util.config import load_config, str2bool
from chia.util.errors import KeychainIsLocked
from chia.util.ints import uint16, uint32, uint64
from chia.util.keychain import bytes_to_mnemonic, generate_mnemonic
from chia.util.misc import UInt32Range
from chia.util.path import path_from_root
from chia.util.streamable import Streamable, streamable
from chia.util.ws_message import WsRpcMessage, create_payload_dict
from chia.wallet.cat_wallet.cat_constants import DEFAULT_CATS
from chia.wallet.cat_wallet.cat_info import CRCATInfo
from chia.wallet.cat_wallet.cat_wallet import CATWallet
from chia.wallet.cat_wallet.dao_cat_info import LockedCoinInfo
from chia.wallet.cat_wallet.dao_cat_wallet import DAOCATWallet
from chia.wallet.conditions import Condition
from chia.wallet.dao_wallet.dao_info import DAORules
from chia.wallet.dao_wallet.dao_utils import (
    generate_mint_proposal_innerpuz,
    generate_simple_proposal_innerpuz,
    generate_update_proposal_innerpuz,
    get_treasury_rules_from_puzzle,
)
from chia.wallet.dao_wallet.dao_wallet import DAOWallet
from chia.wallet.derive_keys import (
    MAX_POOL_WALLETS,
    master_sk_to_farmer_sk,
    master_sk_to_pool_sk,
    master_sk_to_singleton_owner_sk,
    match_address_to_sk,
)
from chia.wallet.did_wallet import did_wallet_puzzles
from chia.wallet.did_wallet.did_info import DIDCoinData, DIDInfo
from chia.wallet.did_wallet.did_wallet import DIDWallet
from chia.wallet.did_wallet.did_wallet_puzzles import (
    DID_INNERPUZ_MOD,
    did_program_to_metadata,
    match_did_puzzle,
    metadata_to_program,
)
from chia.wallet.nft_wallet import nft_puzzles
from chia.wallet.nft_wallet.nft_info import NFTCoinInfo, NFTInfo
from chia.wallet.nft_wallet.nft_puzzles import get_metadata_and_phs
from chia.wallet.nft_wallet.nft_wallet import NFTWallet
from chia.wallet.nft_wallet.uncurry_nft import UncurriedNFT
from chia.wallet.notification_store import Notification
from chia.wallet.outer_puzzles import AssetType
from chia.wallet.payment import Payment
from chia.wallet.puzzle_drivers import PuzzleInfo, Solver
from chia.wallet.puzzles.clawback.metadata import AutoClaimSettings, ClawbackMetadata
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_hash_for_synthetic_public_key
from chia.wallet.singleton import (
    SINGLETON_LAUNCHER_PUZZLE_HASH,
    create_singleton_puzzle,
    get_inner_puzzle_from_singleton,
)
from chia.wallet.trade_record import TradeRecord
from chia.wallet.trading.offer import Offer
from chia.wallet.transaction_record import TransactionRecord
from chia.wallet.uncurried_puzzle import uncurry_puzzle
from chia.wallet.util.address_type import AddressType, is_valid_address
from chia.wallet.util.compute_hints import compute_spend_hints_and_additions
from chia.wallet.util.compute_memos import compute_memos
from chia.wallet.util.query_filter import HashFilter, TransactionTypeFilter
from chia.wallet.util.transaction_type import CLAWBACK_INCOMING_TRANSACTION_TYPES, TransactionType
from chia.wallet.util.tx_config import DEFAULT_TX_CONFIG, CoinSelectionConfig, CoinSelectionConfigLoader, TXConfig
from chia.wallet.util.wallet_sync_utils import fetch_coin_spend_for_coin_state
from chia.wallet.util.wallet_types import CoinType, WalletType
from chia.wallet.vc_wallet.cr_cat_drivers import ProofsChecker
from chia.wallet.vc_wallet.cr_cat_wallet import CRCATWallet
from chia.wallet.vc_wallet.vc_store import VCProofs
from chia.wallet.vc_wallet.vc_wallet import VCWallet
from chia.wallet.wallet import Wallet
from chia.wallet.wallet_coin_record import WalletCoinRecord
from chia.wallet.wallet_coin_store import CoinRecordOrder, GetCoinRecords, unspent_range
from chia.wallet.wallet_info import WalletInfo
from chia.wallet.wallet_node import WalletNode
from chia.wallet.wallet_protocol import WalletProtocol

# Timeout for response from wallet/full node for sending a transaction
TIMEOUT = 30
MAX_DERIVATION_INDEX_DELTA = 1000
MAX_NFT_CHUNK_SIZE = 25

log = logging.getLogger(__name__)


class WalletRpcApi:
    max_get_coin_records_limit: ClassVar[uint32] = uint32(1000)
    max_get_coin_records_filter_items: ClassVar[uint32] = uint32(1000)

    def __init__(self, wallet_node: WalletNode):
        assert wallet_node is not None
        self.service = wallet_node
        self.service_name = "chia_wallet"

    def get_routes(self) -> Dict[str, Endpoint]:
        return {
            # Key management
            "/log_in": self.log_in,
            "/get_logged_in_fingerprint": self.get_logged_in_fingerprint,
            "/get_public_keys": self.get_public_keys,
            "/get_private_key": self.get_private_key,
            "/generate_mnemonic": self.generate_mnemonic,
            "/add_key": self.add_key,
            "/delete_key": self.delete_key,
            "/check_delete_key": self.check_delete_key,
            "/delete_all_keys": self.delete_all_keys,
            # Wallet node
            "/set_wallet_resync_on_startup": self.set_wallet_resync_on_startup,
            "/get_sync_status": self.get_sync_status,
            "/get_height_info": self.get_height_info,
            "/push_tx": self.push_tx,
            "/push_transactions": self.push_transactions,
            "/farm_block": self.farm_block,  # Only when node simulator is running
            "/get_timestamp_for_height": self.get_timestamp_for_height,
            "/set_auto_claim": self.set_auto_claim,
            "/get_auto_claim": self.get_auto_claim,
            # this function is just here for backwards-compatibility. It will probably
            # be removed in the future
            "/get_initial_freeze_period": self.get_initial_freeze_period,
            "/get_network_info": self.get_network_info,
            # Wallet management
            "/get_wallets": self.get_wallets,
            "/create_new_wallet": self.create_new_wallet,
            # Wallet
            "/get_wallet_balance": self.get_wallet_balance,
            "/get_wallet_balances": self.get_wallet_balances,
            "/get_transaction": self.get_transaction,
            "/get_transactions": self.get_transactions,
            "/get_transaction_count": self.get_transaction_count,
            "/get_next_address": self.get_next_address,
            "/send_transaction": self.send_transaction,
            "/send_transaction_multi": self.send_transaction_multi,
            "/spend_clawback_coins": self.spend_clawback_coins,
            "/get_coin_records": self.get_coin_records,
            "/get_farmed_amount": self.get_farmed_amount,
            "/create_signed_transaction": self.create_signed_transaction,
            "/delete_unconfirmed_transactions": self.delete_unconfirmed_transactions,
            "/select_coins": self.select_coins,
            "/get_spendable_coins": self.get_spendable_coins,
            "/get_coin_records_by_names": self.get_coin_records_by_names,
            "/get_current_derivation_index": self.get_current_derivation_index,
            "/extend_derivation_index": self.extend_derivation_index,
            "/get_notifications": self.get_notifications,
            "/delete_notifications": self.delete_notifications,
            "/send_notification": self.send_notification,
            "/sign_message_by_address": self.sign_message_by_address,
            "/sign_message_by_id": self.sign_message_by_id,
            "/verify_signature": self.verify_signature,
            "/get_transaction_memo": self.get_transaction_memo,
            # CATs and trading
            "/cat_set_name": self.cat_set_name,
            "/cat_asset_id_to_name": self.cat_asset_id_to_name,
            "/cat_get_name": self.cat_get_name,
            "/get_stray_cats": self.get_stray_cats,
            "/cat_spend": self.cat_spend,
            "/cat_get_asset_id": self.cat_get_asset_id,
            "/create_offer_for_ids": self.create_offer_for_ids,
            "/get_offer_summary": self.get_offer_summary,
            "/check_offer_validity": self.check_offer_validity,
            "/take_offer": self.take_offer,
            "/get_offer": self.get_offer,
            "/get_all_offers": self.get_all_offers,
            "/get_offers_count": self.get_offers_count,
            "/cancel_offer": self.cancel_offer,
            "/cancel_offers": self.cancel_offers,
            "/get_cat_list": self.get_cat_list,
            # DID Wallet
            "/did_set_wallet_name": self.did_set_wallet_name,
            "/did_get_wallet_name": self.did_get_wallet_name,
            "/did_update_recovery_ids": self.did_update_recovery_ids,
            "/did_update_metadata": self.did_update_metadata,
            "/did_get_pubkey": self.did_get_pubkey,
            "/did_get_did": self.did_get_did,
            "/did_recovery_spend": self.did_recovery_spend,
            "/did_get_recovery_list": self.did_get_recovery_list,
            "/did_get_metadata": self.did_get_metadata,
            "/did_create_attest": self.did_create_attest,
            "/did_get_information_needed_for_recovery": self.did_get_information_needed_for_recovery,
            "/did_get_current_coin_info": self.did_get_current_coin_info,
            "/did_create_backup_file": self.did_create_backup_file,
            "/did_transfer_did": self.did_transfer_did,
            "/did_message_spend": self.did_message_spend,
            "/did_get_info": self.did_get_info,
            "/did_find_lost_did": self.did_find_lost_did,
            # DAO Wallets
            "/dao_get_proposals": self.dao_get_proposals,
            "/dao_create_proposal": self.dao_create_proposal,
            "/dao_parse_proposal": self.dao_parse_proposal,
            "/dao_vote_on_proposal": self.dao_vote_on_proposal,
            "/dao_get_treasury_balance": self.dao_get_treasury_balance,
            "/dao_get_treasury_id": self.dao_get_treasury_id,
            "/dao_get_rules": self.dao_get_rules,
            "/dao_close_proposal": self.dao_close_proposal,
            "/dao_exit_lockup": self.dao_exit_lockup,
            "/dao_adjust_filter_level": self.dao_adjust_filter_level,
            "/dao_add_funds_to_treasury": self.dao_add_funds_to_treasury,
            "/dao_send_to_lockup": self.dao_send_to_lockup,
            "/dao_get_proposal_state": self.dao_get_proposal_state,
            "/dao_free_coins_from_finished_proposals": self.dao_free_coins_from_finished_proposals,
            # NFT Wallet
            "/nft_mint_nft": self.nft_mint_nft,
            "/nft_count_nfts": self.nft_count_nfts,
            "/nft_get_nfts": self.nft_get_nfts,
            "/nft_get_by_did": self.nft_get_by_did,
            "/nft_set_nft_did": self.nft_set_nft_did,
            "/nft_set_nft_status": self.nft_set_nft_status,
            "/nft_get_wallet_did": self.nft_get_wallet_did,
            "/nft_get_wallets_with_dids": self.nft_get_wallets_with_dids,
            "/nft_get_info": self.nft_get_info,
            "/nft_transfer_nft": self.nft_transfer_nft,
            "/nft_add_uri": self.nft_add_uri,
            "/nft_calculate_royalties": self.nft_calculate_royalties,
            "/nft_mint_bulk": self.nft_mint_bulk,
            "/nft_set_did_bulk": self.nft_set_did_bulk,
            "/nft_transfer_bulk": self.nft_transfer_bulk,
            # Pool Wallet
            "/pw_join_pool": self.pw_join_pool,
            "/pw_self_pool": self.pw_self_pool,
            "/pw_absorb_rewards": self.pw_absorb_rewards,
            "/pw_status": self.pw_status,
            # DL Wallet
            "/create_new_dl": self.create_new_dl,
            "/dl_track_new": self.dl_track_new,
            "/dl_stop_tracking": self.dl_stop_tracking,
            "/dl_latest_singleton": self.dl_latest_singleton,
            "/dl_singletons_by_root": self.dl_singletons_by_root,
            "/dl_update_root": self.dl_update_root,
            "/dl_update_multiple": self.dl_update_multiple,
            "/dl_history": self.dl_history,
            "/dl_owned_singletons": self.dl_owned_singletons,
            "/dl_get_mirrors": self.dl_get_mirrors,
            "/dl_new_mirror": self.dl_new_mirror,
            "/dl_delete_mirror": self.dl_delete_mirror,
            "/dl_verify_proof": self.dl_verify_proof,
            # Verified Credential
            "/vc_mint": self.vc_mint,
            "/vc_get": self.vc_get,
            "/vc_get_list": self.vc_get_list,
            "/vc_spend": self.vc_spend,
            "/vc_add_proofs": self.vc_add_proofs,
            "/vc_get_proofs_for_root": self.vc_get_proofs_for_root,
            "/vc_revoke": self.vc_revoke,
            # CR-CATs
            "/crcat_approve_pending": self.crcat_approve_pending,
        }

    def get_connections(self, request_node_type: Optional[NodeType]) -> List[Dict[str, Any]]:
        return default_get_connections(server=self.service.server, request_node_type=request_node_type)

    async def _state_changed(self, change: str, change_data: Optional[Dict[str, Any]]) -> List[WsRpcMessage]:
        """
        Called by the WalletNode or WalletStateManager when something has changed in the wallet. This
        gives us an opportunity to send notifications to all connected clients via WebSocket.
        """
        payloads = []
        if change in {"sync_changed", "coin_added", "add_connection", "close_connection"}:
            # Metrics is the only current consumer for this event
            payloads.append(create_payload_dict(change, change_data, self.service_name, "metrics"))

        payloads.append(create_payload_dict("state_changed", change_data, self.service_name, "wallet_ui"))

        return payloads

    async def _stop_wallet(self) -> None:
        """
        Stops a currently running wallet/key, which allows starting the wallet with a new key.
        Each key has it's own wallet database.
        """
        if self.service is not None:
            self.service._close()
            await self.service._await_closed(shutting_down=False)

    async def _convert_tx_puzzle_hash(self, tx: TransactionRecord) -> TransactionRecord:
        return dataclasses.replace(
            tx,
            to_puzzle_hash=(
                await self.service.wallet_state_manager.convert_puzzle_hash(tx.wallet_id, tx.to_puzzle_hash)
            ),
        )

    async def get_latest_singleton_coin_spend(
        self, peer: WSChiaConnection, coin_id: bytes32, latest: bool = True
    ) -> Tuple[CoinSpend, CoinState]:
        coin_state_list: List[CoinState] = await self.service.wallet_state_manager.wallet_node.get_coin_state(
            [coin_id], peer=peer
        )
        if coin_state_list is None or len(coin_state_list) < 1:
            raise ValueError(f"Coin record 0x{coin_id.hex()} not found")
        coin_state: CoinState = coin_state_list[0]
        if latest:
            # Find the unspent coin
            while coin_state.spent_height is not None:
                coin_state_list = await self.service.wallet_state_manager.wallet_node.fetch_children(
                    coin_state.coin.name(), peer=peer
                )
                odd_coin = None
                for coin in coin_state_list:
                    if coin.coin.amount % 2 == 1:
                        if odd_coin is not None:
                            raise ValueError("This is not a singleton, multiple children coins found.")
                        odd_coin = coin
                if odd_coin is None:
                    raise ValueError("Cannot find child coin, please wait then retry.")
                coin_state = odd_coin
        # Get parent coin
        parent_coin_state_list: List[CoinState] = await self.service.wallet_state_manager.wallet_node.get_coin_state(
            [coin_state.coin.parent_coin_info], peer=peer
        )
        if parent_coin_state_list is None or len(parent_coin_state_list) < 1:
            raise ValueError(f"Parent coin record 0x{coin_state.coin.parent_coin_info.hex()} not found")
        parent_coin_state: CoinState = parent_coin_state_list[0]
        coin_spend = await fetch_coin_spend_for_coin_state(parent_coin_state, peer)
        return coin_spend, coin_state

    ##########################################################################################
    # Key management
    ##########################################################################################

    async def log_in(self, request: Dict[str, Any]) -> EndpointResult:
        """
        Logs in the wallet with a specific key.
        """

        fingerprint = request["fingerprint"]
        if self.service.logged_in_fingerprint == fingerprint:
            return {"fingerprint": fingerprint}

        await self._stop_wallet()
        started = await self.service._start_with_fingerprint(fingerprint)
        if started is True:
            return {"fingerprint": fingerprint}

        return {"success": False, "error": "Unknown Error"}

    async def get_logged_in_fingerprint(self, request: Dict[str, Any]) -> EndpointResult:
        return {"fingerprint": self.service.logged_in_fingerprint}

    async def get_public_keys(self, request: Dict[str, Any]) -> EndpointResult:
        try:
            fingerprints = [
                sk.get_g1().get_fingerprint() for (sk, seed) in await self.service.keychain_proxy.get_all_private_keys()
            ]
        except KeychainIsLocked:
            return {"keyring_is_locked": True}
        except Exception as e:
            raise Exception(
                "Error while getting keys.  If the issue persists, restart all services."
                f"  Original error: {type(e).__name__}: {e}"
            ) from e
        else:
            return {"public_key_fingerprints": fingerprints}

    async def _get_private_key(self, fingerprint: int) -> Tuple[Optional[PrivateKey], Optional[bytes]]:
        try:
            all_keys = await self.service.keychain_proxy.get_all_private_keys()
            for sk, seed in all_keys:
                if sk.get_g1().get_fingerprint() == fingerprint:
                    return sk, seed
        except Exception as e:
            log.error(f"Failed to get private key by fingerprint: {e}")
        return None, None

    async def get_private_key(self, request: Dict[str, Any]) -> EndpointResult:
        fingerprint = request["fingerprint"]
        sk, seed = await self._get_private_key(fingerprint)
        if sk is not None:
            s = bytes_to_mnemonic(seed) if seed is not None else None
            return {
                "private_key": {
                    "fingerprint": fingerprint,
                    "sk": bytes(sk).hex(),
                    "pk": bytes(sk.get_g1()).hex(),
                    "farmer_pk": bytes(master_sk_to_farmer_sk(sk).get_g1()).hex(),
                    "pool_pk": bytes(master_sk_to_pool_sk(sk).get_g1()).hex(),
                    "seed": s,
                },
            }
        return {"success": False, "private_key": {"fingerprint": fingerprint}}

    async def generate_mnemonic(self, request: Dict[str, Any]) -> EndpointResult:
        return {"mnemonic": generate_mnemonic().split(" ")}

    async def add_key(self, request: Dict[str, Any]) -> EndpointResult:
        if "mnemonic" not in request:
            raise ValueError("Mnemonic not in request")

        # Adding a key from 24 word mnemonic
        mnemonic = request["mnemonic"]
        try:
            sk = await self.service.keychain_proxy.add_private_key(" ".join(mnemonic))
        except KeyError as e:
            return {
                "success": False,
                "error": f"The word '{e.args[0]}' is incorrect.'",
                "word": e.args[0],
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

        fingerprint = sk.get_g1().get_fingerprint()
        await self._stop_wallet()

        # Makes sure the new key is added to config properly
        started = False
        try:
            await self.service.keychain_proxy.check_keys(self.service.root_path)
        except Exception as e:
            log.error(f"Failed to check_keys after adding a new key: {e}")
        started = await self.service._start_with_fingerprint(fingerprint=fingerprint)
        if started is True:
            return {"fingerprint": fingerprint}
        raise ValueError("Failed to start")

    async def delete_key(self, request: Dict[str, Any]) -> EndpointResult:
        await self._stop_wallet()
        fingerprint = request["fingerprint"]
        try:
            await self.service.keychain_proxy.delete_key_by_fingerprint(fingerprint)
        except Exception as e:
            log.error(f"Failed to delete key by fingerprint: {e}")
            return {"success": False, "error": str(e)}
        path = path_from_root(
            self.service.root_path,
            f"{self.service.config['database_path']}-{fingerprint}",
        )
        if path.exists():
            path.unlink()
        return {}

    async def _check_key_used_for_rewards(
        self, new_root: Path, sk: PrivateKey, max_ph_to_search: int
    ) -> Tuple[bool, bool]:
        """Checks if the given key is used for either the farmer rewards or pool rewards
        returns a tuple of two booleans
        The first is true if the key is used as the Farmer rewards, otherwise false
        The second is true if the key is used as the Pool rewards, otherwise false
        Returns both false if the key cannot be found with the given fingerprint
        """
        if sk is None:
            return False, False

        config: Dict[str, Any] = load_config(new_root, "config.yaml")
        farmer_target = config["farmer"].get("xch_target_address")
        pool_target = config["pool"].get("xch_target_address")
        address_to_check: List[bytes32] = [decode_puzzle_hash(farmer_target), decode_puzzle_hash(pool_target)]

        found_addresses: Set[bytes32] = match_address_to_sk(sk, address_to_check, max_ph_to_search)

        found_farmer = address_to_check[0] in found_addresses
        found_pool = address_to_check[1] in found_addresses

        return found_farmer, found_pool

    async def check_delete_key(self, request: Dict[str, Any]) -> EndpointResult:
        """Check the key use prior to possible deletion
        checks whether key is used for either farm or pool rewards
        checks if any wallets have a non-zero balance
        """
        used_for_farmer: bool = False
        used_for_pool: bool = False
        walletBalance: bool = False

        fingerprint = request["fingerprint"]
        max_ph_to_search = request.get("max_ph_to_search", 100)
        sk, _ = await self._get_private_key(fingerprint)
        if sk is not None:
            used_for_farmer, used_for_pool = await self._check_key_used_for_rewards(
                self.service.root_path, sk, max_ph_to_search
            )

            if self.service.logged_in_fingerprint != fingerprint:
                await self._stop_wallet()
                await self.service._start_with_fingerprint(fingerprint=fingerprint)

            wallets: List[WalletInfo] = await self.service.wallet_state_manager.get_all_wallet_info_entries()
            for w in wallets:
                wallet = self.service.wallet_state_manager.wallets[w.id]
                unspent = await self.service.wallet_state_manager.coin_store.get_unspent_coins_for_wallet(w.id)
                balance = await wallet.get_confirmed_balance(unspent)
                pending_balance = await wallet.get_unconfirmed_balance(unspent)

                if (balance + pending_balance) > 0:
                    walletBalance = True
                    break

        return {
            "fingerprint": fingerprint,
            "used_for_farmer_rewards": used_for_farmer,
            "used_for_pool_rewards": used_for_pool,
            "wallet_balance": walletBalance,
        }

    async def delete_all_keys(self, request: Dict[str, Any]) -> EndpointResult:
        await self._stop_wallet()
        try:
            await self.service.keychain_proxy.delete_all_keys()
        except Exception as e:
            log.error(f"Failed to delete all keys: {e}")
            return {"success": False, "error": str(e)}
        path = path_from_root(self.service.root_path, self.service.config["database_path"])
        if path.exists():
            path.unlink()
        return {}

    ##########################################################################################
    # Wallet Node
    ##########################################################################################
    async def set_wallet_resync_on_startup(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        Resync the current logged in wallet. The transaction and offer records will be kept.
        :param request: optionally pass in `enable` as bool to enable/disable resync
        :return:
        """
        assert self.service.wallet_state_manager is not None
        try:
            enable = bool(request.get("enable", True))
        except ValueError:
            raise ValueError("Please provide a boolean value for `enable` parameter in request")
        fingerprint = self.service.logged_in_fingerprint
        if fingerprint is not None:
            self.service.set_resync_on_startup(fingerprint, enable)
        else:
            raise ValueError("You need to login into wallet to use this RPC call")
        return {"success": True}

    async def get_sync_status(self, request: Dict[str, Any]) -> EndpointResult:
        sync_mode = self.service.wallet_state_manager.sync_mode
        has_pending_queue_items = self.service.new_peak_queue.has_pending_data_process_items()
        syncing = sync_mode or has_pending_queue_items
        synced = await self.service.wallet_state_manager.synced()
        return {"synced": synced, "syncing": syncing, "genesis_initialized": True}

    async def get_height_info(self, request: Dict[str, Any]) -> EndpointResult:
        height = await self.service.wallet_state_manager.blockchain.get_finished_sync_up_to()
        return {"height": height}

    async def get_network_info(self, request: Dict[str, Any]) -> EndpointResult:
        network_name = self.service.config["selected_network"]
        address_prefix = self.service.config["network_overrides"]["config"][network_name]["address_prefix"]
        return {"network_name": network_name, "network_prefix": address_prefix}

    async def push_tx(self, request: Dict[str, Any]) -> EndpointResult:
        nodes = self.service.server.get_connections(NodeType.FULL_NODE)
        if len(nodes) == 0:
            raise ValueError("Wallet is not currently connected to any full node peers")
        await self.service.push_tx(SpendBundle.from_bytes(hexstr_to_bytes(request["spend_bundle"])))
        return {}

    async def push_transactions(self, request: Dict[str, Any]) -> EndpointResult:
        wallet = self.service.wallet_state_manager.main_wallet

        txs: List[TransactionRecord] = []
        for transaction_hexstr in request["transactions"]:
            tx = TransactionRecord.from_bytes(hexstr_to_bytes(transaction_hexstr))
            txs.append(tx)

        async with self.service.wallet_state_manager.lock:
            for tx in txs:
                await wallet.push_transaction(tx)

        return {}

    async def farm_block(self, request: Dict[str, Any]) -> EndpointResult:
        raw_puzzle_hash = decode_puzzle_hash(request["address"])
        msg = make_msg(ProtocolMessageTypes.farm_new_block, FarmNewBlockProtocol(raw_puzzle_hash))

        await self.service.server.send_to_all([msg], NodeType.FULL_NODE)
        return {}

    async def get_timestamp_for_height(self, request: Dict[str, Any]) -> EndpointResult:
        return {"timestamp": await self.service.get_timestamp_for_height(uint32(request["height"]))}

    async def set_auto_claim(self, request: Dict[str, Any]) -> EndpointResult:
        """
        Set auto claim merkle coins config
        :param request: Example {"enable": true, "tx_fee": 100000, "min_amount": 0, "batch_size": 50}
        :return:
        """
        return self.service.set_auto_claim(AutoClaimSettings.from_json_dict(request))

    async def get_auto_claim(self, request: Dict[str, Any]) -> EndpointResult:
        """
        Get auto claim merkle coins config
        :param request: None
        :return:
        """
        auto_claim_settings = AutoClaimSettings.from_json_dict(
            self.service.wallet_state_manager.config.get("auto_claim", {})
        )
        return auto_claim_settings.to_json_dict()

    ##########################################################################################
    # Wallet Management
    ##########################################################################################

    async def get_wallets(self, request: Dict[str, Any]) -> EndpointResult:
        include_data: bool = request.get("include_data", True)
        wallet_type: Optional[WalletType] = None
        if "type" in request:
            wallet_type = WalletType(request["type"])

        wallets: List[WalletInfo] = await self.service.wallet_state_manager.get_all_wallet_info_entries(wallet_type)
        if not include_data:
            result: List[WalletInfo] = []
            for wallet in wallets:
                result.append(WalletInfo(wallet.id, wallet.name, wallet.type, ""))
            wallets = result
        response: EndpointResult = {"wallets": wallets}
        if include_data:
            response = {
                "wallets": [
                    wallet
                    if wallet.type != WalletType.CRCAT
                    else {
                        **wallet.to_json_dict(),
                        "authorized_providers": [
                            p.hex() for p in CRCATInfo.from_bytes(bytes.fromhex(wallet.data)).authorized_providers
                        ],
                        "flags_needed": CRCATInfo.from_bytes(bytes.fromhex(wallet.data)).proofs_checker.flags,
                    }
                    for wallet in response["wallets"]
                ]
            }
        if self.service.logged_in_fingerprint is not None:
            response["fingerprint"] = self.service.logged_in_fingerprint
        return response

    @tx_endpoint
    async def create_new_wallet(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        wallet_state_manager = self.service.wallet_state_manager

        if await self.service.wallet_state_manager.synced() is False:
            raise ValueError("Wallet needs to be fully synced.")
        main_wallet = wallet_state_manager.main_wallet
        fee = uint64(request.get("fee", 0))

        if request["wallet_type"] == "cat_wallet":
            # If not provided, the name will be autogenerated based on the tail hash.
            name = request.get("name", None)
            if request["mode"] == "new":
                if request.get("test", False):
                    async with self.service.wallet_state_manager.lock:
                        cat_wallet: CATWallet = await CATWallet.create_new_cat_wallet(
                            wallet_state_manager,
                            main_wallet,
                            {"identifier": "genesis_by_id"},
                            uint64(request["amount"]),
                            tx_config,
                            fee,
                            name,
                        )
                        asset_id = cat_wallet.get_asset_id()
                    self.service.wallet_state_manager.state_changed("wallet_created")
                    return {"type": cat_wallet.type(), "asset_id": asset_id, "wallet_id": cat_wallet.id()}
                else:
                    raise ValueError(
                        "Support for this RPC mode has been dropped."
                        " Please use the CAT Admin Tool @ https://github.com/Chia-Network/CAT-admin-tool instead."
                    )

            elif request["mode"] == "existing":
                async with self.service.wallet_state_manager.lock:
                    cat_wallet = await CATWallet.get_or_create_wallet_for_cat(
                        wallet_state_manager, main_wallet, request["asset_id"], name
                    )
                return {"type": cat_wallet.type(), "asset_id": request["asset_id"], "wallet_id": cat_wallet.id()}

            else:  # undefined mode
                pass

        elif request["wallet_type"] == "did_wallet":
            if request["did_type"] == "new":
                backup_dids = []
                num_needed = 0
                for d in request["backup_dids"]:
                    backup_dids.append(decode_puzzle_hash(d))
                if len(backup_dids) > 0:
                    num_needed = uint64(request["num_of_backup_ids_needed"])
                metadata: Dict[str, str] = {}
                if "metadata" in request:
                    if type(request["metadata"]) is dict:
                        metadata = request["metadata"]

                async with self.service.wallet_state_manager.lock:
                    did_wallet_name: str = request.get("wallet_name", None)
                    if did_wallet_name is not None:
                        did_wallet_name = did_wallet_name.strip()
                    did_wallet: DIDWallet = await DIDWallet.create_new_did_wallet(
                        wallet_state_manager,
                        main_wallet,
                        uint64(request["amount"]),
                        backup_dids,
                        uint64(num_needed),
                        metadata,
                        did_wallet_name,
                        uint64(request.get("fee", 0)),
                    )

                    my_did_id = encode_puzzle_hash(
                        bytes32.fromhex(did_wallet.get_my_DID()), AddressType.DID.hrp(self.service.config)
                    )
                    nft_wallet_name = did_wallet_name
                    if nft_wallet_name is not None:
                        nft_wallet_name = f"{nft_wallet_name} NFT Wallet"
                    await NFTWallet.create_new_nft_wallet(
                        wallet_state_manager,
                        main_wallet,
                        bytes32.fromhex(did_wallet.get_my_DID()),
                        nft_wallet_name,
                    )
                return {
                    "success": True,
                    "type": did_wallet.type(),
                    "my_did": my_did_id,
                    "wallet_id": did_wallet.id(),
                }

            elif request["did_type"] == "recovery":
                async with self.service.wallet_state_manager.lock:
                    did_wallet = await DIDWallet.create_new_did_wallet_from_recovery(
                        wallet_state_manager, main_wallet, request["backup_data"]
                    )
                assert did_wallet.did_info.temp_coin is not None
                assert did_wallet.did_info.temp_puzhash is not None
                assert did_wallet.did_info.temp_pubkey is not None
                my_did = did_wallet.get_my_DID()
                coin_name = did_wallet.did_info.temp_coin.name().hex()
                coin_list = coin_as_list(did_wallet.did_info.temp_coin)
                newpuzhash = did_wallet.did_info.temp_puzhash
                pubkey = did_wallet.did_info.temp_pubkey
                return {
                    "success": True,
                    "type": did_wallet.type(),
                    "my_did": my_did,
                    "wallet_id": did_wallet.id(),
                    "coin_name": coin_name,
                    "coin_list": coin_list,
                    "newpuzhash": newpuzhash.hex(),
                    "pubkey": pubkey.hex(),
                    "backup_dids": did_wallet.did_info.backup_ids,
                    "num_verifications_required": did_wallet.did_info.num_of_backup_ids_needed,
                }
            else:  # undefined did_type
                pass
        elif request["wallet_type"] == "dao_wallet":
            name = request.get("name", None)
            mode = request.get("mode", None)
            if mode == "new":
                dao_rules_json = request.get("dao_rules", None)
                if dao_rules_json:
                    dao_rules = DAORules.from_json_dict(dao_rules_json)
                else:
                    raise ValueError("DAO rules must be specified for wallet creation")
                async with self.service.wallet_state_manager.lock:
                    dao_wallet = await DAOWallet.create_new_dao_and_wallet(
                        wallet_state_manager,
                        main_wallet,
                        uint64(request.get("amount_of_cats", None)),
                        dao_rules,
                        tx_config,
                        uint64(request.get("filter_amount", 1)),
                        name,
                        uint64(request.get("fee", 0)),
                        uint64(request.get("fee_for_cat", 0)),
                    )
            elif mode == "existing":
                # async with self.service.wallet_state_manager.lock:
                dao_wallet = await DAOWallet.create_new_dao_wallet_for_existing_dao(
                    wallet_state_manager,
                    main_wallet,
                    bytes32.from_hexstr(request.get("treasury_id", None)),
                    uint64(request.get("filter_amount", 1)),
                    name,
                )
            return {
                "success": True,
                "type": dao_wallet.type(),
                "wallet_id": dao_wallet.id(),
                "treasury_id": dao_wallet.dao_info.treasury_id,
                "cat_wallet_id": dao_wallet.dao_info.cat_wallet_id,
                "dao_cat_wallet_id": dao_wallet.dao_info.dao_cat_wallet_id,
            }
        elif request["wallet_type"] == "nft_wallet":
            for wallet in self.service.wallet_state_manager.wallets.values():
                did_id: Optional[bytes32] = None
                if "did_id" in request and request["did_id"] is not None:
                    did_id = decode_puzzle_hash(request["did_id"])
                if wallet.type() == WalletType.NFT:
                    assert isinstance(wallet, NFTWallet)
                    if wallet.get_did() == did_id:
                        log.info("NFT wallet already existed, skipping.")
                        return {
                            "success": True,
                            "type": wallet.type(),
                            "wallet_id": wallet.id(),
                        }

            async with self.service.wallet_state_manager.lock:
                nft_wallet: NFTWallet = await NFTWallet.create_new_nft_wallet(
                    wallet_state_manager, main_wallet, did_id, request.get("name", None)
                )
            return {
                "success": True,
                "type": nft_wallet.type(),
                "wallet_id": nft_wallet.id(),
            }
        elif request["wallet_type"] == "pool_wallet":
            if request["mode"] == "new":
                if "initial_target_state" not in request:
                    raise AttributeError("Daemon didn't send `initial_target_state`. Try updating the daemon.")

                owner_puzzle_hash: bytes32 = await self.service.wallet_state_manager.main_wallet.get_puzzle_hash(True)

                from chia.pools.pool_wallet_info import initial_pool_state_from_dict

                async with self.service.wallet_state_manager.lock:
                    # We assign a pseudo unique id to each pool wallet, so that each one gets its own deterministic
                    # owner and auth keys. The public keys will go on the blockchain, and the private keys can be found
                    # using the root SK and trying each index from zero. The indexes are not fully unique though,
                    # because the PoolWallet is not created until the tx gets confirmed on chain. Therefore if we
                    # make multiple pool wallets at the same time, they will have the same ID.
                    max_pwi = 1
                    for _, wallet in self.service.wallet_state_manager.wallets.items():
                        if wallet.type() == WalletType.POOLING_WALLET:
                            assert isinstance(wallet, PoolWallet)
                            pool_wallet_index = await wallet.get_pool_wallet_index()
                            if pool_wallet_index > max_pwi:
                                max_pwi = pool_wallet_index

                    if max_pwi + 1 >= (MAX_POOL_WALLETS - 1):
                        raise ValueError(f"Too many pool wallets ({max_pwi}), cannot create any more on this key.")

                    owner_sk: PrivateKey = master_sk_to_singleton_owner_sk(
                        self.service.wallet_state_manager.private_key, uint32(max_pwi + 1)
                    )
                    owner_pk: G1Element = owner_sk.get_g1()

                    initial_target_state = initial_pool_state_from_dict(
                        request["initial_target_state"], owner_pk, owner_puzzle_hash
                    )
                    assert initial_target_state is not None

                    try:
                        delayed_address = None
                        if "p2_singleton_delayed_ph" in request:
                            delayed_address = bytes32.from_hexstr(request["p2_singleton_delayed_ph"])

                        tr, p2_singleton_puzzle_hash, launcher_id = await PoolWallet.create_new_pool_wallet_transaction(
                            wallet_state_manager,
                            main_wallet,
                            initial_target_state,
                            tx_config,
                            fee,
                            request.get("p2_singleton_delay_time", None),
                            delayed_address,
                            extra_conditions=extra_conditions,
                        )
                    except Exception as e:
                        raise ValueError(str(e))
                    return {
                        "total_fee": fee * 2,
                        "transaction": tr,
                        "launcher_id": launcher_id.hex(),
                        "p2_singleton_puzzle_hash": p2_singleton_puzzle_hash.hex(),
                    }
            elif request["mode"] == "recovery":
                raise ValueError("Need upgraded singleton for on-chain recovery")

        else:  # undefined wallet_type
            pass

        # TODO: rework this function to report detailed errors for each error case
        return {"success": False, "error": "invalid request"}

    ##########################################################################################
    # Wallet
    ##########################################################################################

    async def _get_wallet_balance(self, wallet_id: uint32) -> Dict[str, Any]:
        wallet = self.service.wallet_state_manager.wallets[wallet_id]
        balance = await self.service.get_balance(wallet_id)
        wallet_balance = balance.to_json_dict()
        wallet_balance["wallet_id"] = wallet_id
        wallet_balance["wallet_type"] = wallet.type()
        if self.service.logged_in_fingerprint is not None:
            wallet_balance["fingerprint"] = self.service.logged_in_fingerprint
        if wallet.type() in {WalletType.CAT, WalletType.CRCAT}:
            assert isinstance(wallet, CATWallet)
            wallet_balance["asset_id"] = wallet.get_asset_id()
            if wallet.type() == WalletType.CRCAT:
                assert isinstance(wallet, CRCATWallet)
                wallet_balance["pending_approval_balance"] = await wallet.get_pending_approval_balance()

        return wallet_balance

    async def get_wallet_balance(self, request: Dict[str, Any]) -> EndpointResult:
        wallet_id = uint32(int(request["wallet_id"]))
        wallet_balance = await self._get_wallet_balance(wallet_id)
        return {"wallet_balance": wallet_balance}

    async def get_wallet_balances(self, request: Dict[str, Any]) -> EndpointResult:
        try:
            wallet_ids: List[uint32] = [uint32(int(wallet_id)) for wallet_id in request["wallet_ids"]]
        except (TypeError, KeyError):
            wallet_ids = list(self.service.wallet_state_manager.wallets.keys())
        wallet_balances: Dict[uint32, Dict[str, Any]] = {}
        for wallet_id in wallet_ids:
            wallet_balances[wallet_id] = await self._get_wallet_balance(wallet_id)
        return {"wallet_balances": wallet_balances}

    async def get_transaction(self, request: Dict[str, Any]) -> EndpointResult:
        transaction_id: bytes32 = bytes32(hexstr_to_bytes(request["transaction_id"]))
        tr: Optional[TransactionRecord] = await self.service.wallet_state_manager.get_transaction(transaction_id)
        if tr is None:
            raise ValueError(f"Transaction 0x{transaction_id.hex()} not found")

        return {
            "transaction": (await self._convert_tx_puzzle_hash(tr)).to_json_dict_convenience(self.service.config),
            "transaction_id": tr.name,
        }

    async def get_transaction_memo(self, request: Dict[str, Any]) -> EndpointResult:
        transaction_id: bytes32 = bytes32(hexstr_to_bytes(request["transaction_id"]))
        tr: Optional[TransactionRecord] = await self.service.wallet_state_manager.get_transaction(transaction_id)
        if tr is None:
            raise ValueError(f"Transaction 0x{transaction_id.hex()} not found")
        if tr.spend_bundle is None or len(tr.spend_bundle.coin_spends) == 0:
            if tr.type == uint32(TransactionType.INCOMING_TX.value):
                # Fetch incoming tx coin spend
                peer = self.service.get_full_node_peer()
                assert len(tr.additions) == 1
                coin_state_list: List[CoinState] = await self.service.wallet_state_manager.wallet_node.get_coin_state(
                    [tr.additions[0].parent_coin_info], peer=peer
                )
                assert len(coin_state_list) == 1
                coin_spend = await fetch_coin_spend_for_coin_state(coin_state_list[0], peer)
                tr = dataclasses.replace(tr, spend_bundle=SpendBundle([coin_spend], G2Element()))
            else:
                raise ValueError(f"Transaction 0x{transaction_id.hex()} doesn't have any coin spend.")
        assert tr.spend_bundle is not None
        memos: Dict[bytes32, List[bytes]] = compute_memos(tr.spend_bundle)
        response = {}
        # Convert to hex string
        for coin_id, memo_list in memos.items():
            response[coin_id.hex()] = [memo.hex() for memo in memo_list]
        return {transaction_id.hex(): response}

    async def get_transactions(self, request: Dict[str, Any]) -> EndpointResult:
        wallet_id = int(request["wallet_id"])

        start = request.get("start", 0)
        end = request.get("end", 50)
        sort_key = request.get("sort_key", None)
        reverse = request.get("reverse", False)

        to_address = request.get("to_address", None)
        to_puzzle_hash: Optional[bytes32] = None
        if to_address is not None:
            to_puzzle_hash = decode_puzzle_hash(to_address)
        type_filter = None
        if "type_filter" in request:
            type_filter = TransactionTypeFilter.from_json_dict(request["type_filter"])

        transactions = await self.service.wallet_state_manager.tx_store.get_transactions_between(
            wallet_id,
            start,
            end,
            sort_key=sort_key,
            reverse=reverse,
            to_puzzle_hash=to_puzzle_hash,
            type_filter=type_filter,
            confirmed=request.get("confirmed", None),
        )
        tx_list = []
        # Format for clawback transactions
        for tr in transactions:
            try:
                tx = (await self._convert_tx_puzzle_hash(tr)).to_json_dict_convenience(self.service.config)
                tx_list.append(tx)
                if tx["type"] not in CLAWBACK_INCOMING_TRANSACTION_TYPES:
                    continue
                coin: Coin = tr.additions[0]
                record: Optional[WalletCoinRecord] = await self.service.wallet_state_manager.coin_store.get_coin_record(
                    coin.name()
                )
                assert record is not None, f"Cannot find coin record for type {tx['type']} transaction {tx['name']}"
                tx["metadata"] = record.parsed_metadata().to_json_dict()
                tx["metadata"]["coin_id"] = coin.name().hex()
                tx["metadata"]["spent"] = record.spent
            except Exception:
                log.exception(f"Failed to get transaction {tr.name}.")
        return {
            "transactions": tx_list,
            "wallet_id": wallet_id,
        }

    async def get_transaction_count(self, request: Dict[str, Any]) -> EndpointResult:
        wallet_id = int(request["wallet_id"])
        type_filter = None
        if "type_filter" in request:
            type_filter = TransactionTypeFilter.from_json_dict(request["type_filter"])
        count = await self.service.wallet_state_manager.tx_store.get_transaction_count_for_wallet(
            wallet_id, confirmed=request.get("confirmed", None), type_filter=type_filter
        )
        return {
            "count": count,
            "wallet_id": wallet_id,
        }

    # this function is just here for backwards-compatibility. It will probably
    # be removed in the future
    async def get_initial_freeze_period(self, request: Dict[str, Any]) -> EndpointResult:
        # Mon May 03 2021 17:00:00 GMT+0000
        return {"INITIAL_FREEZE_END_TIMESTAMP": 1620061200}

    async def get_next_address(self, request: Dict[str, Any]) -> EndpointResult:
        """
        Returns a new address
        """
        if request["new_address"] is True:
            create_new = True
        else:
            create_new = False
        wallet_id = uint32(int(request["wallet_id"]))
        wallet = self.service.wallet_state_manager.wallets[wallet_id]
        selected = self.service.config["selected_network"]
        prefix = self.service.config["network_overrides"]["config"][selected]["address_prefix"]
        if wallet.type() == WalletType.STANDARD_WALLET:
            assert isinstance(wallet, Wallet)
            raw_puzzle_hash = await wallet.get_puzzle_hash(create_new)
            address = encode_puzzle_hash(raw_puzzle_hash, prefix)
        elif wallet.type() in {WalletType.CAT, WalletType.CRCAT}:
            assert isinstance(wallet, CATWallet)
            raw_puzzle_hash = await wallet.standard_wallet.get_puzzle_hash(create_new)
            address = encode_puzzle_hash(raw_puzzle_hash, prefix)
        else:
            raise ValueError(f"Wallet type {wallet.type()} cannot create puzzle hashes")

        return {
            "wallet_id": wallet_id,
            "address": address,
        }

    @tx_endpoint
    async def send_transaction(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        if await self.service.wallet_state_manager.synced() is False:
            raise ValueError("Wallet needs to be fully synced before sending transactions")

        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=Wallet)

        if not isinstance(request["amount"], int) or not isinstance(request["fee"], int):
            raise ValueError("An integer amount or fee is required (too many decimals)")
        amount: uint64 = uint64(request["amount"])
        address = request["address"]
        selected_network = self.service.config["selected_network"]
        expected_prefix = self.service.config["network_overrides"]["config"][selected_network]["address_prefix"]
        if address[0 : len(expected_prefix)] != expected_prefix:
            raise ValueError("Unexpected Address Prefix")
        puzzle_hash: bytes32 = decode_puzzle_hash(address)

        memos: List[bytes] = []
        if "memos" in request:
            memos = [mem.encode("utf-8") for mem in request["memos"]]

        fee: uint64 = uint64(request.get("fee", 0))

        async with self.service.wallet_state_manager.lock:
            [tx] = await wallet.generate_signed_transaction(
                amount,
                puzzle_hash,
                tx_config,
                fee,
                memos=memos,
                puzzle_decorator_override=request.get("puzzle_decorator", None),
                extra_conditions=extra_conditions,
            )
            await wallet.push_transaction(tx)

        # Transaction may not have been included in the mempool yet. Use get_transaction to check.
        return {
            "transaction": tx.to_json_dict_convenience(self.service.config),
            "transaction_id": tx.name,
        }

    async def send_transaction_multi(self, request: Dict[str, Any]) -> EndpointResult:
        if await self.service.wallet_state_manager.synced() is False:
            raise ValueError("Wallet needs to be fully synced before sending transactions")

        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.wallets[wallet_id]

        async with self.service.wallet_state_manager.lock:
            if wallet.type() in {WalletType.CAT, WalletType.CRCAT}:
                assert isinstance(wallet, CATWallet)
                transaction = (await self.cat_spend(request, hold_lock=False))["transaction"]
            else:
                transaction = (await self.create_signed_transaction(request, hold_lock=False))["signed_tx"]
            tr = TransactionRecord.from_json_dict_convenience(transaction)
            if wallet.type() not in {WalletType.CAT, WalletType.CRCAT}:
                assert isinstance(wallet, Wallet)
                await wallet.push_transaction(tr)

        # Transaction may not have been included in the mempool yet. Use get_transaction to check.
        return {"transaction": transaction, "transaction_id": tr.name}

    @tx_endpoint
    async def spend_clawback_coins(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        """Spend clawback coins that were sent (to claw them back) or received (to claim them).

        :param coin_ids: list of coin ids to be spent
        :param batch_size: number of coins to spend per bundle
        :param fee: transaction fee in mojos
        :return:
        """
        if "coin_ids" not in request:
            raise ValueError("Coin IDs are required.")
        coin_ids: List[bytes32] = [bytes32.from_hexstr(coin) for coin in request["coin_ids"]]
        tx_fee: uint64 = uint64(request.get("fee", 0))
        # Get inner puzzle
        coin_records = await self.service.wallet_state_manager.coin_store.get_coin_records(
            coin_id_filter=HashFilter.include(coin_ids),
            coin_type=CoinType.CLAWBACK,
            wallet_type=WalletType.STANDARD_WALLET,
            spent_range=UInt32Range(stop=uint32(0)),
        )

        coins: Dict[Coin, ClawbackMetadata] = {}
        batch_size = request.get(
            "batch_size", self.service.wallet_state_manager.config.get("auto_claim", {}).get("batch_size", 50)
        )
        tx_id_list: List[bytes] = []
        for coin_id, coin_record in coin_records.coin_id_to_record.items():
            try:
                metadata = coin_record.parsed_metadata()
                assert isinstance(metadata, ClawbackMetadata)
                coins[coin_record.coin] = metadata
                if len(coins) >= batch_size:
                    tx_id_list.extend(
                        await self.service.wallet_state_manager.spend_clawback_coins(
                            coins, tx_fee, tx_config, request.get("force", False), extra_conditions=extra_conditions
                        )
                    )
                    coins = {}
            except Exception as e:
                log.error(f"Failed to spend clawback coin {coin_id.hex()}: %s", e)
        if len(coins) > 0:
            tx_id_list.extend(
                await self.service.wallet_state_manager.spend_clawback_coins(
                    coins, tx_fee, tx_config, request.get("force", False), extra_conditions=extra_conditions
                )
            )
        return {
            "success": True,
            "transaction_ids": [tx.hex() for tx in tx_id_list],
        }

    async def delete_unconfirmed_transactions(self, request: Dict[str, Any]) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        if wallet_id not in self.service.wallet_state_manager.wallets:
            raise ValueError(f"Wallet id {wallet_id} does not exist")
        if await self.service.wallet_state_manager.synced() is False:
            raise ValueError("Wallet needs to be fully synced.")

        async with self.service.wallet_state_manager.db_wrapper.writer():
            await self.service.wallet_state_manager.tx_store.delete_unconfirmed_transactions(wallet_id)
            wallet = self.service.wallet_state_manager.wallets[wallet_id]
            if wallet.type() == WalletType.POOLING_WALLET.value:
                assert isinstance(wallet, PoolWallet)
                wallet.target_state = None
            return {}

    async def select_coins(
        self,
        request: Dict[str, Any],
    ) -> EndpointResult:
        assert self.service.logged_in_fingerprint is not None
        cs_config_loader: CoinSelectionConfigLoader = CoinSelectionConfigLoader.from_json_dict(request)

        # Some backwards compat fill-ins
        if cs_config_loader.excluded_coin_ids is None:
            excluded_coins: Optional[List[Coin]] = request.get("excluded_coins", request.get("exclude_coins"))
            if excluded_coins is not None:
                cs_config_loader = cs_config_loader.override(
                    excluded_coin_ids=[Coin.from_json_dict(c).name() for c in excluded_coins],
                )

        cs_config: CoinSelectionConfig = cs_config_loader.autofill(
            constants=self.service.wallet_state_manager.constants,
        )

        if await self.service.wallet_state_manager.synced() is False:
            raise ValueError("Wallet needs to be fully synced before selecting coins")

        amount = uint64(request["amount"])
        wallet_id = uint32(request["wallet_id"])

        wallet = self.service.wallet_state_manager.wallets[wallet_id]
        async with self.service.wallet_state_manager.lock:
            selected_coins = await wallet.select_coins(amount, cs_config)

        return {"coins": [coin.to_json_dict() for coin in selected_coins]}

    async def get_spendable_coins(self, request: Dict[str, Any]) -> EndpointResult:
        if await self.service.wallet_state_manager.synced() is False:
            raise ValueError("Wallet needs to be fully synced before getting all coins")

        wallet_id = uint32(request["wallet_id"])
        min_coin_amount = uint64(request.get("min_coin_amount", 0))
        max_coin_amount: uint64 = uint64(request.get("max_coin_amount", 0))
        if max_coin_amount == 0:
            max_coin_amount = uint64(self.service.wallet_state_manager.constants.MAX_COIN_AMOUNT)
        excluded_coin_amounts: Optional[List[uint64]] = request.get("excluded_coin_amounts")
        if excluded_coin_amounts is not None:
            excluded_coin_amounts = [uint64(a) for a in excluded_coin_amounts]
        else:
            excluded_coin_amounts = []
        excluded_coins_input: Optional[Dict[str, Dict[str, Any]]] = request.get("excluded_coins")
        if excluded_coins_input is not None:
            excluded_coins = [Coin.from_json_dict(json_coin) for json_coin in excluded_coins_input]
        else:
            excluded_coins = []
        excluded_coin_ids_input: Optional[List[str]] = request.get("excluded_coin_ids")
        if excluded_coin_ids_input is not None:
            excluded_coin_ids = [bytes32.from_hexstr(hex_id) for hex_id in excluded_coin_ids_input]
        else:
            excluded_coin_ids = []
        state_mgr = self.service.wallet_state_manager
        wallet = state_mgr.wallets[wallet_id]
        async with state_mgr.lock:
            all_coin_records = await state_mgr.coin_store.get_unspent_coins_for_wallet(wallet_id)
            if wallet.type() in {WalletType.CAT, WalletType.CRCAT}:
                assert isinstance(wallet, CATWallet)
                spendable_coins: List[WalletCoinRecord] = await wallet.get_cat_spendable_coins(all_coin_records)
            else:
                spendable_coins = list(await state_mgr.get_spendable_coins_for_wallet(wallet_id, all_coin_records))

            # Now we get the unconfirmed transactions and manually derive the additions and removals.
            unconfirmed_transactions: List[TransactionRecord] = await state_mgr.tx_store.get_unconfirmed_for_wallet(
                wallet_id
            )
            unconfirmed_removal_ids: Dict[bytes32, uint64] = {
                coin.name(): transaction.created_at_time
                for transaction in unconfirmed_transactions
                for coin in transaction.removals
            }
            unconfirmed_additions: List[Coin] = [
                coin
                for transaction in unconfirmed_transactions
                for coin in transaction.additions
                if await state_mgr.does_coin_belong_to_wallet(coin, wallet_id)
            ]
            valid_spendable_cr: List[CoinRecord] = []
            unconfirmed_removals: List[CoinRecord] = []
            for coin_record in all_coin_records:
                if coin_record.name() in unconfirmed_removal_ids:
                    unconfirmed_removals.append(coin_record.to_coin_record(unconfirmed_removal_ids[coin_record.name()]))
            for coin_record in spendable_coins:  # remove all the unconfirmed coins, exclude coins and dust.
                if coin_record.name() in unconfirmed_removal_ids:
                    continue
                if coin_record.coin in excluded_coins:
                    continue
                if coin_record.name() in excluded_coin_ids:
                    continue
                if coin_record.coin.amount < min_coin_amount or coin_record.coin.amount > max_coin_amount:
                    continue
                if coin_record.coin.amount in excluded_coin_amounts:
                    continue
                c_r = await state_mgr.get_coin_record_by_wallet_record(coin_record)
                assert c_r is not None and c_r.coin == coin_record.coin  # this should never happen
                valid_spendable_cr.append(c_r)

        return {
            "confirmed_records": [cr.to_json_dict() for cr in valid_spendable_cr],
            "unconfirmed_removals": [cr.to_json_dict() for cr in unconfirmed_removals],
            "unconfirmed_additions": [coin.to_json_dict() for coin in unconfirmed_additions],
        }

    async def get_coin_records_by_names(self, request: Dict[str, Any]) -> EndpointResult:
        if await self.service.wallet_state_manager.synced() is False:
            raise ValueError("Wallet needs to be fully synced before finding coin information")

        if "names" not in request:
            raise ValueError("Names not in request")
        coin_ids = [bytes32.from_hexstr(name) for name in request["names"]]
        kwargs: Dict[str, Any] = {
            "coin_id_filter": HashFilter.include(coin_ids),
        }

        confirmed_range = UInt32Range()
        if "start_height" in request:
            confirmed_range = dataclasses.replace(confirmed_range, start=uint32(request["start_height"]))
        if "end_height" in request:
            confirmed_range = dataclasses.replace(confirmed_range, stop=uint32(request["end_height"]))
        if confirmed_range != UInt32Range():
            kwargs["confirmed_range"] = confirmed_range

        if "include_spent_coins" in request and not str2bool(request["include_spent_coins"]):
            kwargs["spent_range"] = unspent_range

        async with self.service.wallet_state_manager.lock:
            coin_records: List[CoinRecord] = await self.service.wallet_state_manager.get_coin_records_by_coin_ids(
                **kwargs
            )
            missed_coins: List[str] = [
                "0x" + c_id.hex() for c_id in coin_ids if c_id not in [cr.name for cr in coin_records]
            ]
            if missed_coins:
                raise ValueError(f"Coin ID's: {missed_coins} not found.")

        return {"coin_records": [cr.to_json_dict() for cr in coin_records]}

    async def get_current_derivation_index(self, request: Dict[str, Any]) -> Dict[str, Any]:
        assert self.service.wallet_state_manager is not None

        index: Optional[uint32] = await self.service.wallet_state_manager.puzzle_store.get_last_derivation_path()

        return {"success": True, "index": index}

    async def extend_derivation_index(self, request: Dict[str, Any]) -> Dict[str, Any]:
        assert self.service.wallet_state_manager is not None

        # Require a new max derivation index
        if "index" not in request:
            raise ValueError("Derivation index is required")

        # Require that the wallet is fully synced
        synced = await self.service.wallet_state_manager.synced()
        if synced is False:
            raise ValueError("Wallet needs to be fully synced before extending derivation index")

        index = uint32(request["index"])
        current: Optional[uint32] = await self.service.wallet_state_manager.puzzle_store.get_last_derivation_path()

        # Additional sanity check that the wallet is synced
        if current is None:
            raise ValueError("No current derivation record found, unable to extend index")

        # Require that the new index is greater than the current index
        if index <= current:
            raise ValueError(f"New derivation index must be greater than current index: {current}")

        if index - current > MAX_DERIVATION_INDEX_DELTA:
            raise ValueError(
                "Too many derivations requested. "
                f"Use a derivation index less than {current + MAX_DERIVATION_INDEX_DELTA + 1}"
            )

        # Since we've bumping the derivation index without having found any new puzzles, we want
        # to preserve the current last used index, so we call create_more_puzzle_hashes with
        # mark_existing_as_used=False
        await self.service.wallet_state_manager.create_more_puzzle_hashes(
            from_zero=False, mark_existing_as_used=False, up_to_index=index, num_additional_phs=0
        )

        updated: Optional[uint32] = await self.service.wallet_state_manager.puzzle_store.get_last_derivation_path()
        updated_index = updated if updated is not None else None

        return {"success": True, "index": updated_index}

    async def get_notifications(self, request: Dict[str, Any]) -> EndpointResult:
        ids: Optional[List[str]] = request.get("ids", None)
        start: Optional[int] = request.get("start", None)
        end: Optional[int] = request.get("end", None)
        if ids is None:
            notifications: List[
                Notification
            ] = await self.service.wallet_state_manager.notification_manager.notification_store.get_all_notifications(
                pagination=(start, end)
            )
        else:
            notifications = (
                await self.service.wallet_state_manager.notification_manager.notification_store.get_notifications(
                    [bytes32.from_hexstr(id) for id in ids]
                )
            )

        return {
            "notifications": [
                {
                    "id": notification.coin_id.hex(),
                    "message": notification.message.hex(),
                    "amount": notification.amount,
                    "height": notification.height,
                }
                for notification in notifications
            ]
        }

    async def delete_notifications(self, request: Dict[str, Any]) -> EndpointResult:
        ids: Optional[List[str]] = request.get("ids", None)
        if ids is None:
            await self.service.wallet_state_manager.notification_manager.notification_store.delete_all_notifications()
        else:
            await self.service.wallet_state_manager.notification_manager.notification_store.delete_notifications(
                [bytes32.from_hexstr(id) for id in ids]
            )

        return {}

    @tx_endpoint
    async def send_notification(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        tx: TransactionRecord = await self.service.wallet_state_manager.notification_manager.send_new_notification(
            bytes32.from_hexstr(request["target"]),
            bytes.fromhex(request["message"]),
            uint64(request["amount"]),
            tx_config,
            request.get("fee", uint64(0)),
            extra_conditions=extra_conditions,
        )
        await self.service.wallet_state_manager.add_pending_transaction(tx)
        return {"tx": tx.to_json_dict_convenience(self.service.config)}

    async def verify_signature(self, request: Dict[str, Any]) -> EndpointResult:
        """
        Given a public key, message and signature, verify if it is valid.
        :param request:
        :return:
        """
        input_message: str = request["message"]
        signing_mode_str: Optional[str] = request.get("signing_mode")
        # Default to BLS_MESSAGE_AUGMENTATION_HEX_INPUT as this RPC was originally designed to verify
        # signatures made by `chia keys sign`, which uses BLS_MESSAGE_AUGMENTATION_HEX_INPUT
        if signing_mode_str is None:
            signing_mode = SigningMode.BLS_MESSAGE_AUGMENTATION_HEX_INPUT
        else:
            try:
                signing_mode = SigningMode(signing_mode_str)
            except ValueError:
                raise ValueError(f"Invalid signing mode: {signing_mode_str!r}")

        if signing_mode == SigningMode.CHIP_0002:
            # CHIP-0002 message signatures are made over the tree hash of:
            #   ("Chia Signed Message", message)
            message_to_verify: bytes = Program.to((CHIP_0002_SIGN_MESSAGE_PREFIX, input_message)).get_tree_hash()
        elif signing_mode == SigningMode.BLS_MESSAGE_AUGMENTATION_HEX_INPUT:
            # Message is expected to be a hex string
            message_to_verify = hexstr_to_bytes(input_message)
        elif signing_mode == SigningMode.BLS_MESSAGE_AUGMENTATION_UTF8_INPUT:
            # Message is expected to be a UTF-8 string
            message_to_verify = bytes(input_message, "utf-8")
        else:
            raise ValueError(f"Unsupported signing mode: {signing_mode_str!r}")

        # Verify using the BLS message augmentation scheme
        is_valid = AugSchemeMPL.verify(
            G1Element.from_bytes(hexstr_to_bytes(request["pubkey"])),
            message_to_verify,
            G2Element.from_bytes(hexstr_to_bytes(request["signature"])),
        )
        if "address" in request:
            # For signatures made by the sign_message_by_address/sign_message_by_id
            # endpoints, the "address" field should contain the p2_address of the NFT/DID
            # that was used to sign the message.
            puzzle_hash: bytes32 = decode_puzzle_hash(request["address"])
            if puzzle_hash != puzzle_hash_for_synthetic_public_key(
                G1Element.from_bytes(hexstr_to_bytes(request["pubkey"]))
            ):
                return {"isValid": False, "error": "Public key doesn't match the address"}
        if is_valid:
            return {"isValid": is_valid}
        else:
            return {"isValid": False, "error": "Signature is invalid."}

    async def sign_message_by_address(self, request: Dict[str, Any]) -> EndpointResult:
        """
        Given a derived P2 address, sign the message by its private key.
        :param request:
        :return:
        """
        puzzle_hash: bytes32 = decode_puzzle_hash(request["address"])
        is_hex: bool = request.get("is_hex", False)
        if isinstance(is_hex, str):
            is_hex = True if is_hex.lower() == "true" else False
        safe_mode: bool = request.get("safe_mode", True)
        if isinstance(safe_mode, str):
            safe_mode = True if safe_mode.lower() == "true" else False
        mode: SigningMode = SigningMode.CHIP_0002
        if is_hex and safe_mode:
            mode = SigningMode.CHIP_0002_HEX_INPUT
        elif not is_hex and not safe_mode:
            mode = SigningMode.BLS_MESSAGE_AUGMENTATION_UTF8_INPUT
        elif is_hex and not safe_mode:
            mode = SigningMode.BLS_MESSAGE_AUGMENTATION_HEX_INPUT
        pubkey, signature = await self.service.wallet_state_manager.main_wallet.sign_message(
            request["message"], puzzle_hash, mode
        )
        return {
            "success": True,
            "pubkey": str(pubkey),
            "signature": str(signature),
            "signing_mode": mode.value,
        }

    async def sign_message_by_id(self, request: Dict[str, Any]) -> EndpointResult:
        """
        Given a NFT/DID ID, sign the message by the P2 private key.
        :param request:
        :return:
        """
        entity_id: bytes32 = decode_puzzle_hash(request["id"])
        selected_wallet: Optional[WalletProtocol[Any]] = None
        is_hex: bool = request.get("is_hex", False)
        if isinstance(is_hex, str):
            is_hex = True if is_hex.lower() == "true" else False
        safe_mode: bool = request.get("safe_mode", True)
        if isinstance(safe_mode, str):
            safe_mode = True if safe_mode.lower() == "true" else False
        mode: SigningMode = SigningMode.CHIP_0002
        if is_hex and safe_mode:
            mode = SigningMode.CHIP_0002_HEX_INPUT
        elif not is_hex and not safe_mode:
            mode = SigningMode.BLS_MESSAGE_AUGMENTATION_UTF8_INPUT
        elif is_hex and not safe_mode:
            mode = SigningMode.BLS_MESSAGE_AUGMENTATION_HEX_INPUT
        if is_valid_address(request["id"], {AddressType.DID}, self.service.config):
            for wallet in self.service.wallet_state_manager.wallets.values():
                if wallet.type() == WalletType.DECENTRALIZED_ID.value:
                    assert isinstance(wallet, DIDWallet)
                    assert wallet.did_info.origin_coin is not None
                    if wallet.did_info.origin_coin.name() == entity_id:
                        selected_wallet = wallet
                        break
            if selected_wallet is None:
                return {"success": False, "error": f"DID for {entity_id.hex()} doesn't exist."}
            assert isinstance(selected_wallet, DIDWallet)
            pubkey, signature = await selected_wallet.sign_message(request["message"], mode)
            latest_coin_id = (await selected_wallet.get_coin()).name()
        elif is_valid_address(request["id"], {AddressType.NFT}, self.service.config):
            target_nft: Optional[NFTCoinInfo] = None
            for wallet in self.service.wallet_state_manager.wallets.values():
                if wallet.type() == WalletType.NFT.value:
                    assert isinstance(wallet, NFTWallet)
                    nft: Optional[NFTCoinInfo] = await wallet.get_nft(entity_id)
                    if nft is not None:
                        selected_wallet = wallet
                        target_nft = nft
                        break
            if selected_wallet is None or target_nft is None:
                return {"success": False, "error": f"NFT for {entity_id.hex()} doesn't exist."}

            assert isinstance(selected_wallet, NFTWallet)
            pubkey, signature = await selected_wallet.sign_message(request["message"], target_nft, mode)
            latest_coin_id = target_nft.coin.name()
        else:
            return {"success": False, "error": f'Unknown ID type, {request["id"]}'}

        return {
            "success": True,
            "pubkey": str(pubkey),
            "signature": str(signature),
            "latest_coin_id": latest_coin_id.hex() if latest_coin_id is not None else None,
            "signing_mode": mode.value,
        }

    ##########################################################################################
    # CATs and Trading
    ##########################################################################################

    async def get_cat_list(self, request: Dict[str, Any]) -> EndpointResult:
        return {"cat_list": list(DEFAULT_CATS.values())}

    async def cat_set_name(self, request: Dict[str, Any]) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=CATWallet)
        await wallet.set_name(str(request["name"]))
        return {"wallet_id": wallet_id}

    async def cat_get_name(self, request: Dict[str, Any]) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=CATWallet)
        name: str = wallet.get_name()
        return {"wallet_id": wallet_id, "name": name}

    async def get_stray_cats(self, request: Dict[str, Any]) -> EndpointResult:
        """
        Get a list of all unacknowledged CATs
        :param request: RPC request
        :return: A list of unacknowledged CATs
        """
        cats = await self.service.wallet_state_manager.interested_store.get_unacknowledged_tokens()
        return {"stray_cats": cats}

    @tx_endpoint
    async def cat_spend(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
        hold_lock: bool = True,
    ) -> EndpointResult:
        if await self.service.wallet_state_manager.synced() is False:
            raise ValueError("Wallet needs to be fully synced.")
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=CATWallet)

        amounts: List[uint64] = []
        puzzle_hashes: List[bytes32] = []
        memos: List[List[bytes]] = []
        additions: Optional[List[Dict[str, Any]]] = request.get("additions")
        if not isinstance(request["fee"], int) or (additions is None and not isinstance(request["amount"], int)):
            raise ValueError("An integer amount or fee is required (too many decimals)")
        if additions is not None:
            for addition in additions:
                receiver_ph = bytes32.from_hexstr(addition["puzzle_hash"])
                if len(receiver_ph) != 32:
                    raise ValueError(f"Address must be 32 bytes. {receiver_ph.hex()}")
                amount = uint64(addition["amount"])
                if amount > self.service.constants.MAX_COIN_AMOUNT:
                    raise ValueError(f"Coin amount cannot exceed {self.service.constants.MAX_COIN_AMOUNT}")
                amounts.append(amount)
                puzzle_hashes.append(receiver_ph)
                if "memos" in addition:
                    memos.append([mem.encode("utf-8") for mem in addition["memos"]])
        else:
            amounts.append(uint64(request["amount"]))
            puzzle_hashes.append(decode_puzzle_hash(request["inner_address"]))
            if "memos" in request:
                memos.append([mem.encode("utf-8") for mem in request["memos"]])
        coins: Optional[Set[Coin]] = None
        if "coins" in request and len(request["coins"]) > 0:
            coins = {Coin.from_json_dict(coin_json) for coin_json in request["coins"]}
        fee: uint64 = uint64(request.get("fee", 0))

        cat_discrepancy_params: Tuple[Optional[int], Optional[str], Optional[str]] = (
            request.get("extra_delta", None),
            request.get("tail_reveal", None),
            request.get("tail_solution", None),
        )
        cat_discrepancy: Optional[Tuple[int, Program, Program]] = None
        if cat_discrepancy_params != (None, None, None):
            if None in cat_discrepancy_params:
                raise ValueError("Specifying extra_delta, tail_reveal, or tail_solution requires specifying the others")
            else:
                assert cat_discrepancy_params[0] is not None
                assert cat_discrepancy_params[1] is not None
                assert cat_discrepancy_params[2] is not None
                cat_discrepancy = (
                    cat_discrepancy_params[0],  # mypy sanitization
                    Program.fromhex(cat_discrepancy_params[1]),
                    Program.fromhex(cat_discrepancy_params[2]),
                )
        if hold_lock:
            async with self.service.wallet_state_manager.lock:
                txs: List[TransactionRecord] = await wallet.generate_signed_transaction(
                    amounts,
                    puzzle_hashes,
                    tx_config,
                    fee,
                    cat_discrepancy=cat_discrepancy,
                    coins=coins,
                    memos=memos if memos else None,
                    extra_conditions=extra_conditions,
                )
                for tx in txs:
                    await wallet.standard_wallet.push_transaction(tx)
        else:
            txs = await wallet.generate_signed_transaction(
                amounts,
                puzzle_hashes,
                tx_config,
                fee,
                cat_discrepancy=cat_discrepancy,
                coins=coins,
                memos=memos if memos else None,
                extra_conditions=extra_conditions,
            )
            for tx in txs:
                await wallet.standard_wallet.push_transaction(tx)

        # Return the first transaction, which is expected to be the CAT spend. If a fee is
        # included, it is currently ordered after the CAT spend.
        return {
            "transaction": txs[0].to_json_dict_convenience(self.service.config),
            "transaction_id": txs[0].name,
        }

    async def cat_get_asset_id(self, request: Dict[str, Any]) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=CATWallet)
        asset_id: str = wallet.get_asset_id()
        return {"asset_id": asset_id, "wallet_id": wallet_id}

    async def cat_asset_id_to_name(self, request: Dict[str, Any]) -> EndpointResult:
        wallet = await self.service.wallet_state_manager.get_wallet_for_asset_id(request["asset_id"])
        if wallet is None:
            if request["asset_id"] in DEFAULT_CATS:
                return {"wallet_id": None, "name": DEFAULT_CATS[request["asset_id"]]["name"]}
            else:
                raise ValueError("The asset ID specified does not belong to a wallet")
        else:
            return {"wallet_id": wallet.id(), "name": (wallet.get_name())}

    @tx_endpoint
    async def create_offer_for_ids(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        offer: Dict[str, int] = request["offer"]
        fee: uint64 = uint64(request.get("fee", 0))
        validate_only: bool = request.get("validate_only", False)
        driver_dict_str: Optional[Dict[str, Any]] = request.get("driver_dict", None)
        marshalled_solver = request.get("solver")
        solver: Optional[Solver]
        if marshalled_solver is None:
            solver = None
        else:
            solver = Solver(info=marshalled_solver)

        # This driver_dict construction is to maintain backward compatibility where everything is assumed to be a CAT
        driver_dict: Dict[bytes32, PuzzleInfo] = {}
        if driver_dict_str is None:
            for key, amount in offer.items():
                if amount > 0:
                    try:
                        driver_dict[bytes32.from_hexstr(key)] = PuzzleInfo(
                            {"type": AssetType.CAT.value, "tail": "0x" + key}
                        )
                    except ValueError:
                        pass
        else:
            for key, value in driver_dict_str.items():
                driver_dict[bytes32.from_hexstr(key)] = PuzzleInfo(value)

        modified_offer: Dict[Union[int, bytes32], int] = {}
        for key in offer:
            try:
                modified_offer[bytes32.from_hexstr(key)] = offer[key]
            except ValueError:
                modified_offer[int(key)] = offer[key]

        async with self.service.wallet_state_manager.lock:
            result = await self.service.wallet_state_manager.trade_manager.create_offer_for_ids(
                modified_offer,
                tx_config,
                driver_dict,
                solver=solver,
                fee=fee,
                validate_only=validate_only,
                extra_conditions=extra_conditions,
            )
        if result[0]:
            success, trade_record, error = result
            return {
                "offer": Offer.from_bytes(trade_record.offer).to_bech32(),
                "trade_record": trade_record.to_json_dict_convenience(),
            }
        raise ValueError(result[2])

    async def get_offer_summary(self, request: Dict[str, Any]) -> EndpointResult:
        offer_hex: str = request["offer"]

        ###
        # This is temporary code, delete it when we no longer care about incorrectly parsing old offers
        # There's also temp code in test_wallet_rpc.py
        from chia.util.bech32m import bech32_decode, convertbits
        from chia.wallet.util.puzzle_compression import OFFER_MOD_OLD, decompress_object_with_puzzles

        hrpgot, data = bech32_decode(offer_hex, max_length=len(offer_hex))
        if data is None:
            raise ValueError("Invalid Offer")
        decoded = convertbits(list(data), 5, 8, False)
        decoded_bytes = bytes(decoded)
        try:
            decompressed_bytes = decompress_object_with_puzzles(decoded_bytes)
        except zlib.error:
            decompressed_bytes = decoded_bytes
        if bytes(OFFER_MOD_OLD) in decompressed_bytes:
            raise ValueError("Old offer format is no longer supported")
        ###

        offer = Offer.from_bech32(offer_hex)
        offered, requested, infos, valid_times = offer.summary()

        if request.get("advanced", False):
            response = {
                "summary": {
                    "offered": offered,
                    "requested": requested,
                    "fees": offer.fees(),
                    "infos": infos,
                    "valid_times": {
                        k: v
                        for k, v in valid_times.to_json_dict().items()
                        if k
                        not in (
                            "max_secs_after_created",
                            "min_secs_since_created",
                            "max_blocks_after_created",
                            "min_blocks_since_created",
                        )
                    },
                },
                "id": offer.name(),
            }
        else:
            response = {
                "summary": await self.service.wallet_state_manager.trade_manager.get_offer_summary(offer),
                "id": offer.name(),
            }

        # This is a bit of a hack in favor of returning some more manageable information about CR-CATs
        # A more general solution surely exists, but I'm not sure what it is right now
        return {
            **response,
            "summary": {
                **response["summary"],  # type: ignore[dict-item]
                "infos": {
                    key: {
                        **info,
                        "also": {
                            **info["also"],
                            "flags": ProofsChecker.from_program(
                                uncurry_puzzle(
                                    Program(assemble(info["also"]["proofs_checker"]))  # type: ignore[no-untyped-call]
                                )
                            ).flags,
                        },
                    }
                    if "also" in info and "proofs_checker" in info["also"]
                    else info
                    for key, info in response["summary"]["infos"].items()  # type: ignore[index]
                },
            },
        }

    async def check_offer_validity(self, request: Dict[str, Any]) -> EndpointResult:
        offer_hex: str = request["offer"]

        ###
        # This is temporary code, delete it when we no longer care about incorrectly parsing old offers
        # There's also temp code in test_wallet_rpc.py
        from chia.util.bech32m import bech32_decode, convertbits
        from chia.wallet.util.puzzle_compression import OFFER_MOD_OLD, decompress_object_with_puzzles

        hrpgot, data = bech32_decode(offer_hex, max_length=len(offer_hex))
        if data is None:
            raise ValueError("Invalid Offer")  # pragma: no cover
        decoded = convertbits(list(data), 5, 8, False)
        decoded_bytes = bytes(decoded)
        try:
            decompressed_bytes = decompress_object_with_puzzles(decoded_bytes)
        except zlib.error:
            decompressed_bytes = decoded_bytes
        if bytes(OFFER_MOD_OLD) in decompressed_bytes:
            raise ValueError("Old offer format is no longer supported")
        ###

        offer = Offer.from_bech32(offer_hex)
        peer = self.service.get_full_node_peer()
        return {
            "valid": (await self.service.wallet_state_manager.trade_manager.check_offer_validity(offer, peer)),
            "id": offer.name(),
        }

    @tx_endpoint
    async def take_offer(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        offer_hex: str = request["offer"]

        ###
        # This is temporary code, delete it when we no longer care about incorrectly parsing old offers
        # There's also temp code in test_wallet_rpc.py
        from chia.util.bech32m import bech32_decode, convertbits
        from chia.wallet.util.puzzle_compression import OFFER_MOD_OLD, decompress_object_with_puzzles

        hrpgot, data = bech32_decode(offer_hex, max_length=len(offer_hex))
        if data is None:
            raise ValueError("Invalid Offer")  # pragma: no cover
        decoded = convertbits(list(data), 5, 8, False)
        decoded_bytes = bytes(decoded)
        try:
            decompressed_bytes = decompress_object_with_puzzles(decoded_bytes)
        except zlib.error:
            decompressed_bytes = decoded_bytes
        if bytes(OFFER_MOD_OLD) in decompressed_bytes:
            raise ValueError("Old offer format is no longer supported")
        ###

        offer = Offer.from_bech32(offer_hex)
        fee: uint64 = uint64(request.get("fee", 0))
        maybe_marshalled_solver: Optional[Dict[str, Any]] = request.get("solver")
        solver: Optional[Solver]
        if maybe_marshalled_solver is None:
            solver = None
        else:
            solver = Solver(info=maybe_marshalled_solver)

        async with self.service.wallet_state_manager.lock:
            peer = self.service.get_full_node_peer()
            trade_record, tx_records = await self.service.wallet_state_manager.trade_manager.respond_to_offer(
                offer,
                peer,
                tx_config,
                fee=fee,
                solver=solver,
                extra_conditions=extra_conditions,
            )
        return {"trade_record": trade_record.to_json_dict_convenience()}

    async def get_offer(self, request: Dict[str, Any]) -> EndpointResult:
        trade_mgr = self.service.wallet_state_manager.trade_manager

        trade_id = bytes32.from_hexstr(request["trade_id"])
        file_contents: bool = request.get("file_contents", False)
        trade_record: Optional[TradeRecord] = await trade_mgr.get_trade_by_id(bytes32(trade_id))
        if trade_record is None:
            raise ValueError(f"No trade with trade id: {trade_id.hex()}")

        offer_to_return: bytes = trade_record.offer if trade_record.taken_offer is None else trade_record.taken_offer
        offer_value: Optional[str] = Offer.from_bytes(offer_to_return).to_bech32() if file_contents else None
        return {"trade_record": trade_record.to_json_dict_convenience(), "offer": offer_value}

    async def get_all_offers(self, request: Dict[str, Any]) -> EndpointResult:
        trade_mgr = self.service.wallet_state_manager.trade_manager

        start: int = request.get("start", 0)
        end: int = request.get("end", 10)
        exclude_my_offers: bool = request.get("exclude_my_offers", False)
        exclude_taken_offers: bool = request.get("exclude_taken_offers", False)
        include_completed: bool = request.get("include_completed", False)
        sort_key: Optional[str] = request.get("sort_key", None)
        reverse: bool = request.get("reverse", False)
        file_contents: bool = request.get("file_contents", False)

        all_trades = await trade_mgr.trade_store.get_trades_between(
            start,
            end,
            sort_key=sort_key,
            reverse=reverse,
            exclude_my_offers=exclude_my_offers,
            exclude_taken_offers=exclude_taken_offers,
            include_completed=include_completed,
        )
        result = []
        offer_values: Optional[List[str]] = [] if file_contents else None
        for trade in all_trades:
            result.append(trade.to_json_dict_convenience())
            if file_contents and offer_values is not None:
                offer_to_return: bytes = trade.offer if trade.taken_offer is None else trade.taken_offer
                offer_values.append(Offer.from_bytes(offer_to_return).to_bech32())

        return {"trade_records": result, "offers": offer_values}

    async def get_offers_count(self, request: Dict[str, Any]) -> EndpointResult:
        trade_mgr = self.service.wallet_state_manager.trade_manager

        (total, my_offers_count, taken_offers_count) = await trade_mgr.trade_store.get_trades_count()

        return {"total": total, "my_offers_count": my_offers_count, "taken_offers_count": taken_offers_count}

    @tx_endpoint
    async def cancel_offer(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        wsm = self.service.wallet_state_manager
        secure = request["secure"]
        trade_id = bytes32.from_hexstr(request["trade_id"])
        fee: uint64 = uint64(request.get("fee", 0))
        async with self.service.wallet_state_manager.lock:
            await wsm.trade_manager.cancel_pending_offers(
                [bytes32(trade_id)], tx_config, fee=fee, secure=secure, extra_conditions=extra_conditions
            )
        return {}

    @tx_endpoint
    async def cancel_offers(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        secure = request["secure"]
        batch_fee: uint64 = uint64(request.get("batch_fee", 0))
        batch_size = request.get("batch_size", 5)
        cancel_all = request.get("cancel_all", False)
        if cancel_all:
            asset_id = None
        else:
            asset_id = request.get("asset_id", "xch")

        start: int = 0
        end: int = start + batch_size
        trade_mgr = self.service.wallet_state_manager.trade_manager
        log.info(f"Start cancelling offers for  {'asset_id: ' + asset_id if asset_id is not None else 'all'} ...")
        # Traverse offers page by page
        key = None
        if asset_id is not None and asset_id != "xch":
            key = bytes32.from_hexstr(asset_id)
        while True:
            records: Dict[bytes32, TradeRecord] = {}
            trades = await trade_mgr.trade_store.get_trades_between(
                start,
                end,
                reverse=True,
                exclude_my_offers=False,
                exclude_taken_offers=True,
                include_completed=False,
            )
            for trade in trades:
                if cancel_all:
                    records[trade.trade_id] = trade
                    continue
                if trade.offer and trade.offer != b"":
                    offer = Offer.from_bytes(trade.offer)
                    if key in offer.arbitrage():
                        records[trade.trade_id] = trade
                        continue

            async with self.service.wallet_state_manager.lock:
                await trade_mgr.cancel_pending_offers(
                    list(records.keys()), tx_config, batch_fee, secure, records, extra_conditions=extra_conditions
                )
            log.info(f"Cancelled offers {start} to {end} ...")
            # If fewer records were returned than requested, we're done
            if len(trades) < batch_size:
                break
            start = end
            end += batch_size
        return {"success": True}

    ##########################################################################################
    # Distributed Identities
    ##########################################################################################

    async def did_set_wallet_name(self, request: Dict[str, Any]) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=DIDWallet)
        await wallet.set_name(str(request["name"]))
        return {"success": True, "wallet_id": wallet_id}

    async def did_get_wallet_name(self, request: Dict[str, Any]) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=DIDWallet)
        name: str = wallet.get_name()  # type: ignore[no-untyped-call]  # Missing hint in `did_wallet.py`
        return {"success": True, "wallet_id": wallet_id, "name": name}

    @tx_endpoint
    async def did_update_recovery_ids(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=DIDWallet)
        recovery_list = []
        success: bool = False
        for _ in request["new_list"]:
            recovery_list.append(decode_puzzle_hash(_))
        if "num_verifications_required" in request:
            new_amount_verifications_required = uint64(request["num_verifications_required"])
        else:
            new_amount_verifications_required = uint64(len(recovery_list))
        async with self.service.wallet_state_manager.lock:
            update_success = await wallet.update_recovery_list(recovery_list, new_amount_verifications_required)
            # Update coin with new ID info
            if update_success:
                spend_bundle = await wallet.create_update_spend(
                    tx_config, fee=uint64(request.get("fee", 0)), extra_conditions=extra_conditions
                )
                if spend_bundle is not None:
                    success = True
        return {"success": success}

    @tx_endpoint
    async def did_message_spend(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=DIDWallet)
        coin_announcements: Set[bytes] = set()
        for ca in request.get("coin_announcements", []):
            coin_announcements.add(bytes.fromhex(ca))
        puzzle_announcements: Set[bytes] = set()
        for pa in request.get("puzzle_announcements", []):
            puzzle_announcements.add(bytes.fromhex(pa))

        spend_bundle = (
            await wallet.create_message_spend(
                tx_config, coin_announcements, puzzle_announcements, extra_conditions=extra_conditions
            )
        ).spend_bundle
        return {"success": True, "spend_bundle": spend_bundle}

    async def did_get_info(self, request: Dict[str, Any]) -> EndpointResult:
        if "coin_id" not in request:
            return {"success": False, "error": "Coin ID is required."}
        coin_id = request["coin_id"]
        if coin_id.startswith(AddressType.DID.hrp(self.service.config)):
            coin_id = decode_puzzle_hash(coin_id)
        else:
            coin_id = bytes32.from_hexstr(coin_id)
        # Get coin state
        peer = self.service.get_full_node_peer()
        coin_spend, coin_state = await self.get_latest_singleton_coin_spend(peer, coin_id, request.get("latest", True))
        full_puzzle: Program = Program.from_bytes(bytes(coin_spend.puzzle_reveal))
        uncurried = uncurry_puzzle(full_puzzle)
        curried_args = match_did_puzzle(uncurried.mod, uncurried.args)
        if curried_args is None:
            return {"success": False, "error": "The coin is not a DID."}
        p2_puzzle, recovery_list_hash, num_verification, singleton_struct, metadata = curried_args
        uncurried_p2 = uncurry_puzzle(p2_puzzle)
        (public_key,) = uncurried_p2.args.as_iter()
        memos = compute_memos(SpendBundle([coin_spend], G2Element()))
        hints = []
        if coin_state.coin.name() in memos:
            for memo in memos[coin_state.coin.name()]:
                hints.append(memo.hex())
        return {
            "success": True,
            "did_id": encode_puzzle_hash(
                bytes32.from_hexstr(singleton_struct.rest().first().atom.hex()),
                AddressType.DID.hrp(self.service.config),
            ),
            "latest_coin": coin_state.coin.name().hex(),
            "p2_address": encode_puzzle_hash(p2_puzzle.get_tree_hash(), AddressType.XCH.hrp(self.service.config)),
            "public_key": public_key.atom.hex(),
            "recovery_list_hash": recovery_list_hash.atom.hex(),
            "num_verification": num_verification.as_int(),
            "metadata": did_program_to_metadata(metadata),
            "launcher_id": singleton_struct.rest().first().atom.hex(),
            "full_puzzle": full_puzzle,
            "solution": Program.from_bytes(bytes(coin_spend.solution)).as_python(),
            "hints": hints,
        }

    async def did_find_lost_did(self, request: Dict[str, Any]) -> EndpointResult:
        """
        Recover a missing or unspendable DID wallet by a coin id of the DID
        :param coin_id: It can be DID ID, launcher coin ID or any coin ID of the DID you want to find.
        The latest coin ID will take less time.
        :return:
        """
        if "coin_id" not in request:
            return {"success": False, "error": "DID coin ID is required."}
        coin_id = request["coin_id"]
        # Check if we have a DID wallet for this
        if coin_id.startswith(AddressType.DID.hrp(self.service.config)):
            coin_id = decode_puzzle_hash(coin_id)
        else:
            coin_id = bytes32.from_hexstr(coin_id)
        # Get coin state
        peer = self.service.get_full_node_peer()
        coin_spend, coin_state = await self.get_latest_singleton_coin_spend(peer, coin_id)
        full_puzzle: Program = Program.from_bytes(bytes(coin_spend.puzzle_reveal))
        uncurried = uncurry_puzzle(full_puzzle)
        curried_args = match_did_puzzle(uncurried.mod, uncurried.args)
        if curried_args is None:
            return {"success": False, "error": "The coin is not a DID."}
        p2_puzzle, recovery_list_hash, num_verification, singleton_struct, metadata = curried_args
        did_data: DIDCoinData = DIDCoinData(
            p2_puzzle,
            bytes32(recovery_list_hash.atom),
            uint16(num_verification.as_int()),
            singleton_struct,
            metadata,
            get_inner_puzzle_from_singleton(coin_spend.puzzle_reveal.to_program()),
            coin_state,
        )
        hinted_coins, _ = compute_spend_hints_and_additions(coin_spend)
        # Hint is required, if it doesn't have any hint then it should be invalid
        hint: Optional[bytes32] = None
        for hinted_coin in hinted_coins.values():
            if hinted_coin.coin.amount % 2 == 1 and hinted_coin.hint is not None:
                hint = hinted_coin.hint
                break
        if hint is None:
            # This is an invalid DID, check if we are owner
            derivation_record = (
                await self.service.wallet_state_manager.puzzle_store.get_derivation_record_for_puzzle_hash(
                    p2_puzzle.get_tree_hash()
                )
            )
        else:
            derivation_record = (
                await self.service.wallet_state_manager.puzzle_store.get_derivation_record_for_puzzle_hash(hint)
            )

        launcher_id = singleton_struct.rest().first().as_python()
        if derivation_record is None:
            return {"success": False, "error": f"This DID {launcher_id.hex()} is not belong to the connected wallet"}
        else:
            our_inner_puzzle: Program = self.service.wallet_state_manager.main_wallet.puzzle_for_pk(
                derivation_record.pubkey
            )
            did_puzzle = DID_INNERPUZ_MOD.curry(
                our_inner_puzzle, recovery_list_hash, num_verification, singleton_struct, metadata
            )
            full_puzzle = create_singleton_puzzle(did_puzzle, launcher_id)
            did_puzzle_empty_recovery = DID_INNERPUZ_MOD.curry(
                our_inner_puzzle, Program.to([]).get_tree_hash(), uint64(0), singleton_struct, metadata
            )
            # Check if we have the DID wallet
            did_wallet: Optional[DIDWallet] = None
            for wallet in self.service.wallet_state_manager.wallets.values():
                if isinstance(wallet, DIDWallet):
                    assert wallet.did_info.origin_coin is not None
                    if wallet.did_info.origin_coin.name() == launcher_id:
                        did_wallet = wallet
                        break

            full_puzzle_empty_recovery = create_singleton_puzzle(did_puzzle_empty_recovery, launcher_id)
            if full_puzzle.get_tree_hash() != coin_state.coin.puzzle_hash:
                if full_puzzle_empty_recovery.get_tree_hash() == coin_state.coin.puzzle_hash:
                    did_puzzle = did_puzzle_empty_recovery
                elif (
                    did_wallet is not None
                    and did_wallet.did_info.current_inner is not None
                    and create_singleton_puzzle(did_wallet.did_info.current_inner, launcher_id).get_tree_hash()
                    == coin_state.coin.puzzle_hash
                ):
                    # Check if the old wallet has the inner puzzle
                    did_puzzle = did_wallet.did_info.current_inner
                else:
                    # Try override
                    if "recovery_list_hash" in request:
                        recovery_list_hash = Program.from_bytes(bytes.fromhex(request["recovery_list_hash"]))
                    num_verification = request.get("num_verification", num_verification)
                    if "metadata" in request:
                        metadata = metadata_to_program(request["metadata"])
                    did_puzzle = DID_INNERPUZ_MOD.curry(
                        our_inner_puzzle, recovery_list_hash, num_verification, singleton_struct, metadata
                    )
                    full_puzzle = create_singleton_puzzle(did_puzzle, launcher_id)
                    matched = True
                    if full_puzzle.get_tree_hash() != coin_state.coin.puzzle_hash:
                        matched = False
                        # Brute force addresses
                        index = 0
                        derivation_record = await self.service.wallet_state_manager.puzzle_store.get_derivation_record(
                            uint32(index), uint32(1), False
                        )
                        while derivation_record is not None:
                            our_inner_puzzle = self.service.wallet_state_manager.main_wallet.puzzle_for_pk(
                                derivation_record.pubkey
                            )
                            did_puzzle = DID_INNERPUZ_MOD.curry(
                                our_inner_puzzle, recovery_list_hash, num_verification, singleton_struct, metadata
                            )
                            full_puzzle = create_singleton_puzzle(did_puzzle, launcher_id)
                            if full_puzzle.get_tree_hash() == coin_state.coin.puzzle_hash:
                                matched = True
                                break
                            index += 1
                            derivation_record = (
                                await self.service.wallet_state_manager.puzzle_store.get_derivation_record(
                                    uint32(index), uint32(1), False
                                )
                            )

                    if not matched:
                        return {
                            "success": False,
                            "error": f"Cannot recover DID {launcher_id.hex()}"
                            f" because the last spend updated recovery_list_hash/num_verification/metadata.",
                        }

            if did_wallet is None:
                # Create DID wallet
                response: List[CoinState] = await self.service.get_coin_state([launcher_id], peer=peer)
                if len(response) == 0:
                    return {"success": False, "error": f"Could not find the launch coin with ID: {launcher_id.hex()}"}
                launcher_coin: CoinState = response[0]
                did_wallet = await DIDWallet.create_new_did_wallet_from_coin_spend(
                    self.service.wallet_state_manager,
                    self.service.wallet_state_manager.main_wallet,
                    launcher_coin.coin,
                    did_puzzle,
                    coin_spend,
                    f"DID {encode_puzzle_hash(launcher_id, AddressType.DID.hrp(self.service.config))}",
                )
            else:
                assert did_wallet.did_info.current_inner is not None
                if did_wallet.did_info.current_inner.get_tree_hash() != did_puzzle.get_tree_hash():
                    # Inner DID puzzle doesn't match, we need to update the DID info
                    full_solution: Program = Program.from_bytes(bytes(coin_spend.solution))
                    inner_solution: Program = full_solution.rest().rest().first()
                    recovery_list: List[bytes32] = []
                    backup_required: int = num_verification.as_int()
                    if recovery_list_hash != Program.to([]).get_tree_hash():
                        try:
                            for did in inner_solution.rest().rest().rest().rest().rest().as_python():
                                recovery_list.append(did[0])
                        except Exception:
                            # We cannot recover the recovery list, but it's okay to leave it blank
                            pass
                    did_info: DIDInfo = DIDInfo(
                        did_wallet.did_info.origin_coin,
                        recovery_list,
                        uint64(backup_required),
                        [],
                        did_puzzle,
                        None,
                        None,
                        None,
                        False,
                        json.dumps(did_wallet_puzzles.did_program_to_metadata(metadata)),
                    )
                    await did_wallet.save_info(did_info)
                    await self.service.wallet_state_manager.update_wallet_puzzle_hashes(did_wallet.wallet_info.id)

            try:
                coin = await did_wallet.get_coin()
                if coin.name() == coin_state.coin.name():
                    return {"success": True, "latest_coin_id": coin.name().hex()}
            except RuntimeError:
                # We don't have any coin for this wallet, add the coin
                pass

            wallet_id = did_wallet.id()
            wallet_type = did_wallet.type()
            assert coin_state.created_height is not None
            coin_record: WalletCoinRecord = WalletCoinRecord(
                coin_state.coin, uint32(coin_state.created_height), uint32(0), False, False, wallet_type, wallet_id
            )
            await self.service.wallet_state_manager.coin_store.add_coin_record(coin_record, coin_state.coin.name())
            await did_wallet.coin_added(
                coin_state.coin,
                uint32(coin_state.created_height),
                peer,
                did_data,
            )
            return {"success": True, "latest_coin_id": coin_state.coin.name().hex()}

    @tx_endpoint
    async def did_update_metadata(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=DIDWallet)
        metadata: Dict[str, str] = {}
        if "metadata" in request and type(request["metadata"]) is dict:
            metadata = request["metadata"]
        async with self.service.wallet_state_manager.lock:
            update_success = await wallet.update_metadata(metadata)
            # Update coin with new ID info
            if update_success:
                spend_bundle = await wallet.create_update_spend(
                    tx_config, uint64(request.get("fee", 0)), extra_conditions=extra_conditions
                )
                if spend_bundle is not None:
                    return {"wallet_id": wallet_id, "success": True, "spend_bundle": spend_bundle}
                else:
                    return {"success": False, "error": "Couldn't create an update spend bundle."}
            else:
                return {"success": False, "error": f"Couldn't update metadata with input: {metadata}"}

    async def did_get_did(self, request: Dict[str, Any]) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=DIDWallet)
        my_did: str = encode_puzzle_hash(bytes32.fromhex(wallet.get_my_DID()), AddressType.DID.hrp(self.service.config))
        async with self.service.wallet_state_manager.lock:
            try:
                coin = await wallet.get_coin()
                return {"success": True, "wallet_id": wallet_id, "my_did": my_did, "coin_id": coin.name()}
            except RuntimeError:
                return {"success": True, "wallet_id": wallet_id, "my_did": my_did}

    async def did_get_recovery_list(self, request: Dict[str, Any]) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=DIDWallet)
        recovery_list = wallet.did_info.backup_ids
        recovery_dids = []
        for backup_id in recovery_list:
            recovery_dids.append(encode_puzzle_hash(backup_id, AddressType.DID.hrp(self.service.config)))
        return {
            "success": True,
            "wallet_id": wallet_id,
            "recovery_list": recovery_dids,
            "num_required": wallet.did_info.num_of_backup_ids_needed,
        }

    async def did_get_metadata(self, request: Dict[str, Any]) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=DIDWallet)
        metadata = json.loads(wallet.did_info.metadata)
        return {
            "success": True,
            "wallet_id": wallet_id,
            "metadata": metadata,
        }

    async def did_recovery_spend(self, request: Dict[str, Any]) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=DIDWallet)
        if len(request["attest_data"]) < wallet.did_info.num_of_backup_ids_needed:
            return {"success": False, "reason": "insufficient messages"}
        spend_bundle = None
        async with self.service.wallet_state_manager.lock:
            (
                info_list,
                message_spend_bundle,
            ) = await wallet.load_attest_files_for_recovery_spend(request["attest_data"])

            if "pubkey" in request:
                pubkey = G1Element.from_bytes(hexstr_to_bytes(request["pubkey"]))
            else:
                assert wallet.did_info.temp_pubkey is not None
                pubkey = G1Element.from_bytes(wallet.did_info.temp_pubkey)

            if "puzhash" in request:
                puzhash = bytes32.from_hexstr(request["puzhash"])
            else:
                assert wallet.did_info.temp_puzhash is not None
                puzhash = wallet.did_info.temp_puzhash

            assert wallet.did_info.temp_coin is not None
            spend_bundle = await wallet.recovery_spend(
                wallet.did_info.temp_coin,
                puzhash,
                info_list,
                pubkey,
                message_spend_bundle,
            )
        if spend_bundle:
            return {"success": True, "spend_bundle": spend_bundle}
        else:
            return {"success": False}

    async def did_get_pubkey(self, request: Dict[str, Any]) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=DIDWallet)
        pubkey = bytes((await wallet.wallet_state_manager.get_unused_derivation_record(wallet_id)).pubkey).hex()
        return {"success": True, "pubkey": pubkey}

    @tx_endpoint
    async def did_create_attest(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=DIDWallet)
        async with self.service.wallet_state_manager.lock:
            info = await wallet.get_info_for_recovery()
            coin = bytes32.from_hexstr(request["coin_name"])
            pubkey = G1Element.from_bytes(hexstr_to_bytes(request["pubkey"]))
            spend_bundle, attest_data = await wallet.create_attestment(
                coin,
                bytes32.from_hexstr(request["puzhash"]),
                pubkey,
                tx_config,
                extra_conditions=extra_conditions,
            )
        if info is not None and spend_bundle is not None:
            return {
                "success": True,
                "message_spend_bundle": bytes(spend_bundle).hex(),
                "info": [info[0].hex(), info[1].hex(), info[2]],
                "attest_data": attest_data,
            }
        else:
            return {"success": False}

    async def did_get_information_needed_for_recovery(self, request: Dict[str, Any]) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        did_wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=DIDWallet)
        my_did = encode_puzzle_hash(
            bytes32.from_hexstr(did_wallet.get_my_DID()), AddressType.DID.hrp(self.service.config)
        )
        assert did_wallet.did_info.temp_coin is not None
        coin_name = did_wallet.did_info.temp_coin.name().hex()
        return {
            "success": True,
            "wallet_id": wallet_id,
            "my_did": my_did,
            "coin_name": coin_name,
            "newpuzhash": did_wallet.did_info.temp_puzhash,
            "pubkey": did_wallet.did_info.temp_pubkey,
            "backup_dids": did_wallet.did_info.backup_ids,
        }

    async def did_get_current_coin_info(self, request: Dict[str, Any]) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        did_wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=DIDWallet)
        my_did = encode_puzzle_hash(
            bytes32.from_hexstr(did_wallet.get_my_DID()), AddressType.DID.hrp(self.service.config)
        )

        did_coin_threeple = await did_wallet.get_info_for_recovery()
        assert my_did is not None
        assert did_coin_threeple is not None
        return {
            "success": True,
            "wallet_id": wallet_id,
            "my_did": my_did,
            "did_parent": did_coin_threeple[0],
            "did_innerpuz": did_coin_threeple[1],
            "did_amount": did_coin_threeple[2],
        }

    async def did_create_backup_file(self, request: Dict[str, Any]) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        did_wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=DIDWallet)
        return {"wallet_id": wallet_id, "success": True, "backup_data": did_wallet.create_backup()}

    @tx_endpoint
    async def did_transfer_did(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        if await self.service.wallet_state_manager.synced() is False:
            raise ValueError("Wallet needs to be fully synced.")
        wallet_id = uint32(request["wallet_id"])
        did_wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=DIDWallet)
        puzzle_hash: bytes32 = decode_puzzle_hash(request["inner_address"])
        async with self.service.wallet_state_manager.lock:
            txs: TransactionRecord = await did_wallet.transfer_did(
                puzzle_hash,
                uint64(request.get("fee", 0)),
                request.get("with_recovery_info", True),
                tx_config,
                extra_conditions=extra_conditions,
            )

        return {
            "success": True,
            "transaction": txs.to_json_dict_convenience(self.service.config),
            "transaction_id": txs.name,
        }

    ##########################################################################################
    # DAO Wallet
    ##########################################################################################

    async def dao_adjust_filter_level(self, request: Dict[str, Any]) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        dao_wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=DAOWallet)
        await dao_wallet.adjust_filter_level(uint64(request["filter_level"]))
        return {
            "success": True,
            "dao_info": dao_wallet.dao_info,
        }

    @tx_endpoint
    async def dao_add_funds_to_treasury(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        push: bool = True,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        dao_wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=DAOWallet)
        funding_wallet_id = uint32(request["funding_wallet_id"])
        wallet_type = self.service.wallet_state_manager.wallets[funding_wallet_id].type()
        amount = request.get("amount")
        assert amount
        if wallet_type not in [WalletType.STANDARD_WALLET, WalletType.CAT]:  # pragma: no cover
            raise ValueError(f"Cannot fund a treasury with assets from a {wallet_type.name} wallet")
        funding_tx = await dao_wallet.create_add_funds_to_treasury_spend(
            uint64(amount),
            tx_config,
            fee=uint64(request.get("fee", 0)),
            funding_wallet_id=funding_wallet_id,
            extra_conditions=extra_conditions,
        )
        if push:
            await self.service.wallet_state_manager.add_pending_transaction(funding_tx)
        return {"success": True, "tx_id": funding_tx.name, "tx": funding_tx}

    async def dao_get_treasury_balance(self, request: Dict[str, Any]) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        dao_wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=DAOWallet)
        assert dao_wallet is not None
        asset_list = dao_wallet.dao_info.assets
        balances = {}
        for asset_id in asset_list:
            balance = await dao_wallet.get_balance_by_asset_type(asset_id=asset_id)
            if asset_id is None:
                balances["xch"] = balance
            else:
                balances[asset_id.hex()] = balance
        return {"success": True, "balances": balances}

    async def dao_get_treasury_id(self, request: Dict[str, Any]) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        dao_wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=DAOWallet)
        assert dao_wallet is not None
        treasury_id = dao_wallet.dao_info.treasury_id
        return {"treasury_id": treasury_id}

    async def dao_get_rules(self, request: Dict[str, Any]) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        dao_wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=DAOWallet)
        assert dao_wallet is not None
        rules = dao_wallet.dao_rules
        return {"rules": rules}

    @tx_endpoint
    async def dao_send_to_lockup(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        push: bool = True,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        dao_wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=DAOWallet)
        dao_cat_wallet = self.service.wallet_state_manager.get_wallet(
            id=dao_wallet.dao_info.dao_cat_wallet_id, required_type=DAOCATWallet
        )
        amount = uint64(request["amount"])
        fee = uint64(request.get("fee", 0))
        txs = await dao_cat_wallet.enter_dao_cat_voting_mode(
            amount,
            tx_config,
            fee=fee,
            extra_conditions=extra_conditions,
        )
        if push:
            for tx in txs:
                await self.service.wallet_state_manager.add_pending_transaction(tx)
        return {
            "success": True,
            "tx_id": txs[0].name,
            "txs": txs,
        }

    async def dao_get_proposals(self, request: Dict[str, Any]) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        include_closed = request.get("include_closed", True)
        dao_wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=DAOWallet)
        assert dao_wallet is not None
        proposal_list = dao_wallet.dao_info.proposals_list
        if not include_closed:
            proposal_list = [prop for prop in proposal_list if not prop.closed]
        dao_rules = get_treasury_rules_from_puzzle(dao_wallet.dao_info.current_treasury_innerpuz)
        return {
            "success": True,
            "proposals": proposal_list,
            "proposal_timelock": dao_rules.proposal_timelock,
            "soft_close_length": dao_rules.soft_close_length,
        }

    async def dao_get_proposal_state(self, request: Dict[str, Any]) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        dao_wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=DAOWallet)
        assert dao_wallet is not None
        state = await dao_wallet.get_proposal_state(bytes32.from_hexstr(request["proposal_id"]))
        return {"success": True, "state": state}

    @tx_endpoint
    async def dao_exit_lockup(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        push: bool = True,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        dao_wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=DAOWallet)
        assert dao_wallet is not None
        dao_cat_wallet = self.service.wallet_state_manager.get_wallet(
            id=dao_wallet.dao_info.dao_cat_wallet_id, required_type=DAOCATWallet
        )
        assert dao_cat_wallet is not None
        if request["coins"]:  # pragma: no cover
            coin_list = [Coin.from_json_dict(coin) for coin in request["coins"]]
            coins: List[LockedCoinInfo] = []
            for lci in dao_cat_wallet.dao_cat_info.locked_coins:
                if lci.coin in coin_list:
                    coins.append(lci)
        else:
            coins = []
            for lci in dao_cat_wallet.dao_cat_info.locked_coins:
                if lci.active_votes == []:
                    coins.append(lci)
        fee = uint64(request.get("fee", 0))
        exit_tx = await dao_cat_wallet.exit_vote_state(
            coins,
            tx_config,
            fee=fee,
            extra_conditions=extra_conditions,
        )
        if push:
            await self.service.wallet_state_manager.add_pending_transaction(exit_tx)
        return {"success": True, "tx_id": exit_tx.name, "tx": exit_tx}

    @tx_endpoint
    async def dao_create_proposal(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        push: bool = True,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        dao_wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=DAOWallet)
        assert dao_wallet is not None

        if request["proposal_type"] == "spend":
            amounts: List[uint64] = []
            puzzle_hashes: List[bytes32] = []
            asset_types: List[Optional[bytes32]] = []
            additions: Optional[List[Dict[str, Any]]] = request.get("additions")
            if additions is not None:
                for addition in additions:
                    if "asset_id" in addition:
                        asset_id = bytes32.from_hexstr(addition["asset_id"])
                    else:
                        asset_id = None
                    receiver_ph = bytes32.from_hexstr(addition["puzzle_hash"])
                    amount = uint64(addition["amount"])
                    amounts.append(amount)
                    puzzle_hashes.append(receiver_ph)
                    asset_types.append(asset_id)
            else:  # pragma: no cover
                amounts.append(uint64(request["amount"]))
                puzzle_hashes.append(decode_puzzle_hash(request["inner_address"]))
                if request["asset_id"] is not None:
                    asset_types.append(bytes32.from_hexstr(request["asset_id"]))
                else:
                    asset_types.append(None)
            proposed_puzzle = generate_simple_proposal_innerpuz(
                dao_wallet.dao_info.treasury_id, puzzle_hashes, amounts, asset_types
            )

        elif request["proposal_type"] == "update":
            rules = dao_wallet.dao_rules
            prop = request["new_dao_rules"]
            new_rules = DAORules(
                proposal_timelock=prop.get("proposal_timelock") or rules.proposal_timelock,
                soft_close_length=prop.get("soft_close_length") or rules.soft_close_length,
                attendance_required=prop.get("attendance_required") or rules.attendance_required,
                proposal_minimum_amount=prop.get("proposal_minimum_amount") or rules.proposal_minimum_amount,
                pass_percentage=prop.get("pass_percentage") or rules.pass_percentage,
                self_destruct_length=prop.get("self_destruct_length") or rules.self_destruct_length,
                oracle_spend_delay=prop.get("oracle_spend_delay") or rules.oracle_spend_delay,
            )

            current_innerpuz = dao_wallet.dao_info.current_treasury_innerpuz
            assert current_innerpuz is not None
            proposed_puzzle = await generate_update_proposal_innerpuz(current_innerpuz, new_rules)
        elif request["proposal_type"] == "mint":
            amount_of_cats = uint64(request["amount"])
            mint_address = decode_puzzle_hash(request["cat_target_address"])
            cat_wallet = self.service.wallet_state_manager.get_wallet(
                id=dao_wallet.dao_info.cat_wallet_id, required_type=CATWallet
            )
            proposed_puzzle = await generate_mint_proposal_innerpuz(
                dao_wallet.dao_info.treasury_id,
                cat_wallet.cat_info.limitations_program_hash,
                amount_of_cats,
                mint_address,
            )
        else:  # pragma: no cover
            return {"success": False, "error": "Unknown proposal type."}

        vote_amount = request.get("vote_amount")
        fee = uint64(request.get("fee", 0))
        proposal_tx = await dao_wallet.generate_new_proposal(
            proposed_puzzle,
            tx_config,
            vote_amount=vote_amount,
            fee=fee,
            extra_conditions=extra_conditions,
        )
        assert proposal_tx is not None
        await self.service.wallet_state_manager.add_pending_transaction(proposal_tx)
        assert isinstance(proposal_tx.removals, List)
        for coin in proposal_tx.removals:
            if coin.puzzle_hash == SINGLETON_LAUNCHER_PUZZLE_HASH:
                proposal_id = coin.name()
                break
        else:  # pragma: no cover
            raise ValueError("Could not find proposal ID in transaction")
        return {
            "success": True,
            "proposal_id": proposal_id,
            "tx_id": proposal_tx.name.hex(),
            "tx": proposal_tx,
        }

    @tx_endpoint
    async def dao_vote_on_proposal(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        push: bool = True,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        dao_wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=DAOWallet)
        assert dao_wallet is not None
        vote_amount = None
        if "vote_amount" in request:
            vote_amount = uint64(request["vote_amount"])
        fee = uint64(request.get("fee", 0))
        vote_tx = await dao_wallet.generate_proposal_vote_spend(
            bytes32.from_hexstr(request["proposal_id"]),
            vote_amount,
            request["is_yes_vote"],  # bool
            tx_config,
            fee,
            extra_conditions=extra_conditions,
        )
        assert vote_tx is not None
        if push:
            await self.service.wallet_state_manager.add_pending_transaction(vote_tx)
        return {"success": True, "tx_id": vote_tx.name, "tx": vote_tx}

    async def dao_parse_proposal(self, request: Dict[str, Any]) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        dao_wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=DAOWallet)
        assert dao_wallet is not None
        proposal_id = bytes32.from_hexstr(request["proposal_id"])
        proposal_dictionary = await dao_wallet.parse_proposal(proposal_id)
        assert proposal_dictionary is not None
        return {"success": True, "proposal_dictionary": proposal_dictionary}

    @tx_endpoint
    async def dao_close_proposal(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        push: bool = True,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        dao_wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=DAOWallet)
        assert dao_wallet is not None
        fee = uint64(request.get("fee", 0))
        if "genesis_id" in request:  # pragma: no cover
            genesis_id = bytes32.from_hexstr(request["genesis_id"])
        else:
            genesis_id = None
        self_destruct = request.get("self_destruct", None)
        tx = await dao_wallet.create_proposal_close_spend(
            bytes32.from_hexstr(request["proposal_id"]),
            tx_config,
            genesis_id,
            fee=fee,
            self_destruct=self_destruct,
            extra_conditions=extra_conditions,
        )
        assert tx is not None
        await self.service.wallet_state_manager.add_pending_transaction(tx)
        return {"success": True, "tx_id": tx.name, "tx": tx}

    @tx_endpoint
    async def dao_free_coins_from_finished_proposals(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        push: bool = True,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        fee = uint64(request.get("fee", 0))
        dao_wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=DAOWallet)
        assert dao_wallet is not None
        tx = await dao_wallet.free_coins_from_finished_proposals(
            tx_config,
            fee=fee,
            extra_conditions=extra_conditions,
        )
        assert tx is not None
        await self.service.wallet_state_manager.add_pending_transaction(tx)

        return {"success": True, "tx_id": tx.name, "tx": tx}

    ##########################################################################################
    # NFT Wallet
    ##########################################################################################
    @tx_endpoint
    async def nft_mint_nft(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        log.debug("Got minting RPC request: %s", request)
        wallet_id = uint32(request["wallet_id"])
        assert self.service.wallet_state_manager
        nft_wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=NFTWallet)
        royalty_address = request.get("royalty_address")
        royalty_amount = uint16(request.get("royalty_percentage", 0))
        if royalty_amount == 10000:
            raise ValueError("Royalty percentage cannot be 100%")
        if isinstance(royalty_address, str):
            royalty_puzhash = decode_puzzle_hash(royalty_address)
        elif royalty_address is None:
            royalty_puzhash = await nft_wallet.standard_wallet.get_new_puzzlehash()
        else:
            royalty_puzhash = royalty_address
        target_address = request.get("target_address")
        if isinstance(target_address, str):
            target_puzhash = decode_puzzle_hash(target_address)
        elif target_address is None:
            target_puzhash = await nft_wallet.standard_wallet.get_new_puzzlehash()
        else:
            target_puzhash = target_address
        if "uris" not in request:
            return {"success": False, "error": "Data URIs is required"}
        if not isinstance(request["uris"], list):
            return {"success": False, "error": "Data URIs must be a list"}
        if not isinstance(request.get("meta_uris", []), list):
            return {"success": False, "error": "Metadata URIs must be a list"}
        if not isinstance(request.get("license_uris", []), list):
            return {"success": False, "error": "License URIs must be a list"}
        metadata_list = [
            ("u", request["uris"]),
            ("h", hexstr_to_bytes(request["hash"])),
            ("mu", request.get("meta_uris", [])),
            ("lu", request.get("license_uris", [])),
            ("sn", uint64(request.get("edition_number", 1))),
            ("st", uint64(request.get("edition_total", 1))),
        ]
        if "meta_hash" in request and len(request["meta_hash"]) > 0:
            metadata_list.append(("mh", hexstr_to_bytes(request["meta_hash"])))
        if "license_hash" in request and len(request["license_hash"]) > 0:
            metadata_list.append(("lh", hexstr_to_bytes(request["license_hash"])))
        metadata = Program.to(metadata_list)
        fee = uint64(request.get("fee", 0))
        did_id = request.get("did_id", None)
        if did_id is not None:
            if did_id == "":
                did_id = b""
            else:
                did_id = decode_puzzle_hash(did_id)

        spend_bundle = await nft_wallet.generate_new_nft(
            metadata,
            tx_config,
            target_puzhash,
            royalty_puzhash,
            royalty_amount,
            did_id,
            fee,
            extra_conditions=extra_conditions,
        )
        nft_id = None
        assert spend_bundle is not None
        for cs in spend_bundle.coin_spends:
            if cs.coin.puzzle_hash == nft_puzzles.LAUNCHER_PUZZLE_HASH:
                nft_id = encode_puzzle_hash(cs.coin.name(), AddressType.NFT.hrp(self.service.config))
        return {"wallet_id": wallet_id, "success": True, "spend_bundle": spend_bundle, "nft_id": nft_id}

    async def nft_count_nfts(self, request: Dict[str, Any]) -> EndpointResult:
        wallet_id = request.get("wallet_id", None)
        count = 0
        if wallet_id is not None:
            try:
                nft_wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=NFTWallet)
            except KeyError:
                # wallet not found
                return {"success": False, "error": f"Wallet {wallet_id} not found."}
            count = await nft_wallet.get_nft_count()
        else:
            count = await self.service.wallet_state_manager.nft_store.count()
        return {"wallet_id": wallet_id, "success": True, "count": count}

    async def nft_get_nfts(self, request: Dict[str, Any]) -> EndpointResult:
        wallet_id = request.get("wallet_id", None)
        nfts: List[NFTCoinInfo] = []
        if wallet_id is not None:
            nft_wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=NFTWallet)
        else:
            nft_wallet = None
        try:
            start_index = int(request.get("start_index", 0))
        except (TypeError, ValueError):
            start_index = 0
        try:
            count = int(request.get("num", 50))
        except (TypeError, ValueError):
            count = 50
        nft_info_list = []
        if nft_wallet is not None:
            nfts = await nft_wallet.get_current_nfts(start_index=start_index, count=count)
        else:
            nfts = await self.service.wallet_state_manager.nft_store.get_nft_list(start_index=start_index, count=count)
        for nft in nfts:
            nft_info = await nft_puzzles.get_nft_info_from_puzzle(
                nft,
                self.service.wallet_state_manager.config,
                request.get("ignore_size_limit", False),
            )
            nft_info_list.append(nft_info)
        return {"wallet_id": wallet_id, "success": True, "nft_list": nft_info_list}

    @tx_endpoint
    async def nft_set_nft_did(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        nft_wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=NFTWallet)
        did_id = request.get("did_id", b"")
        if did_id != b"":
            did_id = decode_puzzle_hash(did_id)
        nft_coin_info = await nft_wallet.get_nft_coin_by_id(bytes32.from_hexstr(request["nft_coin_id"]))
        if not (
            await nft_puzzles.get_nft_info_from_puzzle(nft_coin_info, self.service.wallet_state_manager.config)
        ).supports_did:
            return {"success": False, "error": "The NFT doesn't support setting a DID."}

        fee = uint64(request.get("fee", 0))
        spend_bundle = await nft_wallet.set_nft_did(
            nft_coin_info,
            did_id,
            tx_config,
            fee=fee,
            extra_conditions=extra_conditions,
        )
        return {"wallet_id": wallet_id, "success": True, "spend_bundle": spend_bundle}

    @tx_endpoint
    async def nft_set_did_bulk(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        """
        Bulk set DID for NFTs across different wallets.
        accepted `request` dict keys:
         - required `nft_coin_list`: [{"nft_coin_id": COIN_ID/NFT_ID, "wallet_id": WALLET_ID},....]
         - optional `fee`, in mojos, defaults to 0
         - optional `did_id`, defaults to no DID, meaning it will reset the NFT's DID
        :param request:
        :return:
        """
        if len(request["nft_coin_list"]) > MAX_NFT_CHUNK_SIZE:
            return {"success": False, "error": f"You can only set {MAX_NFT_CHUNK_SIZE} NFTs at once"}
        did_id = request.get("did_id", b"")
        if did_id != b"":
            did_id = decode_puzzle_hash(did_id)
        nft_dict: Dict[uint32, List[NFTCoinInfo]] = {}
        tx_list: List[TransactionRecord] = []
        coin_ids = []
        nft_ids = []
        fee = uint64(request.get("fee", 0))

        nft_wallet: NFTWallet
        for nft_coin in request["nft_coin_list"]:
            if "nft_coin_id" not in nft_coin or "wallet_id" not in nft_coin:
                log.error(f"Cannot set DID for NFT :{nft_coin}, missing nft_coin_id or wallet_id.")
                continue
            wallet_id = uint32(nft_coin["wallet_id"])
            nft_wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=NFTWallet)
            nft_coin_id = nft_coin["nft_coin_id"]
            if nft_coin_id.startswith(AddressType.NFT.hrp(self.service.config)):
                nft_id = decode_puzzle_hash(nft_coin_id)
                nft_coin_info = await nft_wallet.get_nft(nft_id)
            else:
                nft_coin_id = bytes32.from_hexstr(nft_coin_id)
                nft_coin_info = await nft_wallet.get_nft_coin_by_id(nft_coin_id)
            assert nft_coin_info is not None
            if not (
                await nft_puzzles.get_nft_info_from_puzzle(nft_coin_info, self.service.wallet_state_manager.config)
            ).supports_did:
                log.warning(f"Skipping NFT {nft_coin_info.nft_id.hex()}, doesn't support setting a DID.")
                continue
            if wallet_id in nft_dict:
                nft_dict[wallet_id].append(nft_coin_info)
            else:
                nft_dict[wallet_id] = [nft_coin_info]
            nft_ids.append(nft_coin_info.nft_id)
        first = True
        for wallet_id, nft_list in nft_dict.items():
            nft_wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=NFTWallet)
            if not first:
                tx_list.extend(
                    await nft_wallet.set_bulk_nft_did(nft_list, did_id, tx_config, extra_conditions=extra_conditions)
                )
            else:
                tx_list.extend(
                    await nft_wallet.set_bulk_nft_did(
                        nft_list, did_id, tx_config, fee, nft_ids, extra_conditions=extra_conditions
                    )
                )
            for coin in nft_list:
                coin_ids.append(coin.coin.name())
            first = False
        spend_bundles: List[SpendBundle] = []
        refined_tx_list: List[TransactionRecord] = []
        for tx in tx_list:
            if tx.spend_bundle is not None:
                spend_bundles.append(tx.spend_bundle)
            refined_tx_list.append(dataclasses.replace(tx, spend_bundle=None))

        if len(spend_bundles) > 0:
            spend_bundle = SpendBundle.aggregate(spend_bundles)
            # Add all spend bundles to the first tx
            refined_tx_list[0] = dataclasses.replace(refined_tx_list[0], spend_bundle=spend_bundle)

            for tx in refined_tx_list:
                await self.service.wallet_state_manager.add_pending_transaction(tx)
            for id in coin_ids:
                await nft_wallet.update_coin_status(id, True)
            for wallet_id in nft_dict.keys():
                self.service.wallet_state_manager.state_changed("nft_coin_did_set", wallet_id)
            return {
                "wallet_id": list(nft_dict.keys()),
                "success": True,
                "spend_bundle": spend_bundle,
                "tx_num": len(refined_tx_list),
            }
        else:
            raise ValueError("Couldn't set DID on given NFT")

    @tx_endpoint
    async def nft_transfer_bulk(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        """
        Bulk transfer NFTs to an address.
        accepted `request` dict keys:
         - required `nft_coin_list`: [{"nft_coin_id": COIN_ID/NFT_ID, "wallet_id": WALLET_ID},....]
         - required `target_address`, Transfer NFTs to this address
         - optional `fee`, in mojos, defaults to 0
        :param request:
        :return:
        """
        if len(request["nft_coin_list"]) > MAX_NFT_CHUNK_SIZE:
            return {"success": False, "error": f"You can only transfer {MAX_NFT_CHUNK_SIZE} NFTs at once"}
        address = request["target_address"]
        if isinstance(address, str):
            puzzle_hash = decode_puzzle_hash(address)
        else:
            return dict(success=False, error="target_address parameter missing")
        nft_dict: Dict[uint32, List[NFTCoinInfo]] = {}
        tx_list: List[TransactionRecord] = []
        coin_ids = []
        fee = uint64(request.get("fee", 0))

        nft_wallet: NFTWallet
        for nft_coin in request["nft_coin_list"]:
            if "nft_coin_id" not in nft_coin or "wallet_id" not in nft_coin:
                log.error(f"Cannot transfer NFT :{nft_coin}, missing nft_coin_id or wallet_id.")
                continue
            wallet_id = uint32(nft_coin["wallet_id"])
            nft_wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=NFTWallet)
            nft_coin_id = nft_coin["nft_coin_id"]
            if nft_coin_id.startswith(AddressType.NFT.hrp(self.service.config)):
                nft_id = decode_puzzle_hash(nft_coin_id)
                nft_coin_info = await nft_wallet.get_nft(nft_id)
            else:
                nft_coin_id = bytes32.from_hexstr(nft_coin_id)
                nft_coin_info = await nft_wallet.get_nft_coin_by_id(nft_coin_id)
            assert nft_coin_info is not None
            if wallet_id in nft_dict:
                nft_dict[wallet_id].append(nft_coin_info)
            else:
                nft_dict[wallet_id] = [nft_coin_info]
        first = True
        for wallet_id, nft_list in nft_dict.items():
            nft_wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=NFTWallet)
            if not first:
                tx_list.extend(
                    await nft_wallet.bulk_transfer_nft(
                        nft_list, puzzle_hash, tx_config, extra_conditions=extra_conditions
                    )
                )
            else:
                tx_list.extend(
                    await nft_wallet.bulk_transfer_nft(
                        nft_list, puzzle_hash, tx_config, fee, extra_conditions=extra_conditions
                    )
                )
            for coin in nft_list:
                coin_ids.append(coin.coin.name())
            first = False
        spend_bundles: List[SpendBundle] = []
        refined_tx_list: List[TransactionRecord] = []
        for tx in tx_list:
            if tx.spend_bundle is not None:
                spend_bundles.append(tx.spend_bundle)
            refined_tx_list.append(dataclasses.replace(tx, spend_bundle=None))

        if len(spend_bundles) > 0:
            spend_bundle = SpendBundle.aggregate(spend_bundles)
            # Add all spend bundles to the first tx
            refined_tx_list[0] = dataclasses.replace(refined_tx_list[0], spend_bundle=spend_bundle)
            for tx in refined_tx_list:
                await self.service.wallet_state_manager.add_pending_transaction(tx)
            for id in coin_ids:
                await nft_wallet.update_coin_status(id, True)
            for wallet_id in nft_dict.keys():
                self.service.wallet_state_manager.state_changed("nft_coin_did_set", wallet_id)
            return {
                "wallet_id": list(nft_dict.keys()),
                "success": True,
                "spend_bundle": spend_bundle,
                "tx_num": len(refined_tx_list),
            }
        else:
            raise ValueError("Couldn't transfer given NFTs")

    async def nft_get_by_did(self, request: Dict[str, Any]) -> EndpointResult:
        did_id: Optional[bytes32] = None
        if "did_id" in request:
            did_id = decode_puzzle_hash(request["did_id"])
        for wallet in self.service.wallet_state_manager.wallets.values():
            if isinstance(wallet, NFTWallet) and wallet.get_did() == did_id:
                return {"wallet_id": wallet.wallet_id, "success": True}
        return {"error": f"Cannot find a NFT wallet DID = {did_id}", "success": False}

    async def nft_get_wallet_did(self, request: Dict[str, Any]) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        nft_wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=NFTWallet)
        did_bytes: Optional[bytes32] = nft_wallet.get_did()
        did_id = ""
        if did_bytes is not None:
            did_id = encode_puzzle_hash(did_bytes, AddressType.DID.hrp(self.service.config))
        return {"success": True, "did_id": None if len(did_id) == 0 else did_id}

    async def nft_get_wallets_with_dids(self, request: Dict[str, Any]) -> EndpointResult:
        all_wallets = self.service.wallet_state_manager.wallets.values()
        did_wallets_by_did_id: Dict[bytes32, uint32] = {}

        for wallet in all_wallets:
            if wallet.type() == WalletType.DECENTRALIZED_ID:
                assert isinstance(wallet, DIDWallet)
                if wallet.did_info.origin_coin is not None:
                    did_wallets_by_did_id[wallet.did_info.origin_coin.name()] = wallet.id()

        did_nft_wallets: List[Dict[str, Any]] = []
        for wallet in all_wallets:
            if isinstance(wallet, NFTWallet):
                nft_wallet_did: Optional[bytes32] = wallet.get_did()
                if nft_wallet_did is not None:
                    did_wallet_id: uint32 = did_wallets_by_did_id.get(nft_wallet_did, uint32(0))
                    if did_wallet_id == 0:
                        log.warning(f"NFT wallet {wallet.id()} has DID {nft_wallet_did.hex()} but no DID wallet")
                    else:
                        did_nft_wallets.append(
                            {
                                "wallet_id": wallet.id(),
                                "did_id": encode_puzzle_hash(nft_wallet_did, AddressType.DID.hrp(self.service.config)),
                                "did_wallet_id": did_wallet_id,
                            }
                        )
        return {"success": True, "nft_wallets": did_nft_wallets}

    async def nft_set_nft_status(self, request: Dict[str, Any]) -> EndpointResult:
        wallet_id: uint32 = uint32(request["wallet_id"])
        coin_id: bytes32 = bytes32.from_hexstr(request["coin_id"])
        status: bool = request["in_transaction"]
        assert self.service.wallet_state_manager is not None
        nft_wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=NFTWallet)
        await nft_wallet.update_coin_status(coin_id, status)
        return {"success": True}

    @tx_endpoint
    async def nft_transfer_nft(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        address = request["target_address"]
        if isinstance(address, str):
            puzzle_hash = decode_puzzle_hash(address)
        else:
            return dict(success=False, error="target_address parameter missing")
        nft_wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=NFTWallet)
        try:
            nft_coin_id = request["nft_coin_id"]
            if nft_coin_id.startswith(AddressType.NFT.hrp(self.service.config)):
                nft_id = decode_puzzle_hash(nft_coin_id)
                nft_coin_info = await nft_wallet.get_nft(nft_id)
            else:
                nft_coin_id = bytes32.from_hexstr(nft_coin_id)
                nft_coin_info = await nft_wallet.get_nft_coin_by_id(nft_coin_id)
            assert nft_coin_info is not None

            fee = uint64(request.get("fee", 0))
            txs = await nft_wallet.generate_signed_transaction(
                [uint64(nft_coin_info.coin.amount)],
                [puzzle_hash],
                tx_config,
                coins={nft_coin_info.coin},
                fee=fee,
                new_owner=b"",
                new_did_inner_hash=b"",
                extra_conditions=extra_conditions,
            )
            spend_bundle: Optional[SpendBundle] = None
            for tx in txs:
                if tx.spend_bundle is not None:
                    spend_bundle = tx.spend_bundle
                await self.service.wallet_state_manager.add_pending_transaction(tx)
            await nft_wallet.update_coin_status(nft_coin_info.coin.name(), True)
            return {"wallet_id": wallet_id, "success": True, "spend_bundle": spend_bundle}
        except Exception as e:
            log.exception(f"Failed to transfer NFT: {e}")
            return {"success": False, "error": str(e)}

    async def nft_get_info(self, request: Dict[str, Any]) -> EndpointResult:
        if "coin_id" not in request:
            return {"success": False, "error": "Coin ID is required."}
        coin_id = request["coin_id"]
        if coin_id.startswith(AddressType.NFT.hrp(self.service.config)):
            coin_id = decode_puzzle_hash(coin_id)
        else:
            try:
                coin_id = bytes32.from_hexstr(coin_id)
            except ValueError:
                return {"success": False, "error": f"Invalid Coin ID format for 'coin_id': {request['coin_id']!r}"}
        # Get coin state
        peer = self.service.get_full_node_peer()
        coin_spend, coin_state = await self.get_latest_singleton_coin_spend(peer, coin_id, request.get("latest", True))
        # convert to NFTInfo
        # Check if the metadata is updated
        full_puzzle: Program = Program.from_bytes(bytes(coin_spend.puzzle_reveal))

        uncurried_nft: Optional[UncurriedNFT] = UncurriedNFT.uncurry(*full_puzzle.uncurry())
        if uncurried_nft is None:
            return {"success": False, "error": "The coin is not a NFT."}
        metadata, p2_puzzle_hash = get_metadata_and_phs(uncurried_nft, coin_spend.solution)
        # Note: This is not the actual unspent NFT full puzzle.
        # There is no way to rebuild the full puzzle in a different wallet.
        # But it shouldn't have impact on generating the NFTInfo, since inner_puzzle is not used there.
        if uncurried_nft.supports_did:
            inner_puzzle = nft_puzzles.recurry_nft_puzzle(
                uncurried_nft, coin_spend.solution.to_program(), uncurried_nft.p2_puzzle
            )
        else:
            inner_puzzle = uncurried_nft.p2_puzzle

        full_puzzle = nft_puzzles.create_full_puzzle(
            uncurried_nft.singleton_launcher_id,
            metadata,
            uncurried_nft.metadata_updater_hash,
            inner_puzzle,
        )

        # Get launcher coin
        launcher_coin: List[CoinState] = await self.service.wallet_state_manager.wallet_node.get_coin_state(
            [uncurried_nft.singleton_launcher_id], peer=peer
        )
        if launcher_coin is None or len(launcher_coin) < 1 or launcher_coin[0].spent_height is None:
            return {
                "success": False,
                "error": f"Launcher coin record 0x{uncurried_nft.singleton_launcher_id.hex()} not found",
            }
        minter_did = await self.service.wallet_state_manager.get_minter_did(launcher_coin[0].coin, peer)

        nft_info: NFTInfo = await nft_puzzles.get_nft_info_from_puzzle(
            NFTCoinInfo(
                uncurried_nft.singleton_launcher_id,
                coin_state.coin,
                None,
                full_puzzle,
                uint32(launcher_coin[0].spent_height),
                minter_did,
                uint32(coin_state.created_height) if coin_state.created_height else uint32(0),
            ),
            self.service.wallet_state_manager.config,
            request.get("ignore_size_limit", False),
        )
        # This is a bit hacky, it should just come out like this, but this works for this RPC
        nft_info = dataclasses.replace(nft_info, p2_address=p2_puzzle_hash)
        return {"success": True, "nft_info": nft_info}

    @tx_endpoint
    async def nft_add_uri(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        # Note metadata updater can only add one uri for one field per spend.
        # If you want to add multiple uris for one field, you need to spend multiple times.
        nft_wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=NFTWallet)
        uri = request["uri"]
        key = request["key"]
        nft_coin_id = request["nft_coin_id"]
        if nft_coin_id.startswith(AddressType.NFT.hrp(self.service.config)):
            nft_coin_id = decode_puzzle_hash(nft_coin_id)
        else:
            nft_coin_id = bytes32.from_hexstr(nft_coin_id)
        nft_coin_info = await nft_wallet.get_nft_coin_by_id(nft_coin_id)

        fee = uint64(request.get("fee", 0))
        spend_bundle = await nft_wallet.update_metadata(
            nft_coin_info, key, uri, tx_config, fee=fee, extra_conditions=extra_conditions
        )
        return {"wallet_id": wallet_id, "success": True, "spend_bundle": spend_bundle}

    async def nft_calculate_royalties(self, request: Dict[str, Any]) -> EndpointResult:
        return NFTWallet.royalty_calculation(
            {
                asset["asset"]: (asset["royalty_address"], uint16(asset["royalty_percentage"]))
                for asset in request.get("royalty_assets", [])
            },
            {asset["asset"]: uint64(asset["amount"]) for asset in request.get("fungible_assets", [])},
        )

    @tx_endpoint
    async def nft_mint_bulk(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        if await self.service.wallet_state_manager.synced() is False:
            raise ValueError("Wallet needs to be fully synced.")
        wallet_id = uint32(request["wallet_id"])
        nft_wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=NFTWallet)
        royalty_address = request.get("royalty_address", None)
        if isinstance(royalty_address, str) and royalty_address != "":
            royalty_puzhash = decode_puzzle_hash(royalty_address)
        elif royalty_address in [None, ""]:
            royalty_puzhash = await nft_wallet.standard_wallet.get_new_puzzlehash()
        else:
            royalty_puzhash = bytes32.from_hexstr(royalty_address)
        royalty_percentage = request.get("royalty_percentage", None)
        if royalty_percentage is None:
            royalty_percentage = uint16(0)
        else:
            royalty_percentage = uint16(int(royalty_percentage))
        metadata_list = []
        for meta in request["metadata_list"]:
            if "uris" not in meta.keys():
                return {"success": False, "error": "Data URIs is required"}
            if not isinstance(meta["uris"], list):
                return {"success": False, "error": "Data URIs must be a list"}
            if not isinstance(meta.get("meta_uris", []), list):
                return {"success": False, "error": "Metadata URIs must be a list"}
            if not isinstance(meta.get("license_uris", []), list):
                return {"success": False, "error": "License URIs must be a list"}
            nft_metadata = [
                ("u", meta["uris"]),
                ("h", hexstr_to_bytes(meta["hash"])),
                ("mu", meta.get("meta_uris", [])),
                ("lu", meta.get("license_uris", [])),
                ("sn", uint64(meta.get("edition_number", 1))),
                ("st", uint64(meta.get("edition_total", 1))),
            ]
            if "meta_hash" in meta and len(meta["meta_hash"]) > 0:
                nft_metadata.append(("mh", hexstr_to_bytes(meta["meta_hash"])))
            if "license_hash" in meta and len(meta["license_hash"]) > 0:
                nft_metadata.append(("lh", hexstr_to_bytes(meta["license_hash"])))
            metadata_program = Program.to(nft_metadata)
            metadata_dict = {
                "program": metadata_program,
                "royalty_pc": royalty_percentage,
                "royalty_ph": royalty_puzhash,
            }
            metadata_list.append(metadata_dict)
        target_address_list = request.get("target_list", None)
        target_list = []
        if target_address_list:
            for target in target_address_list:
                target_list.append(decode_puzzle_hash(target))
        mint_number_start = request.get("mint_number_start", 1)
        mint_total = request.get("mint_total", None)
        xch_coin_list = request.get("xch_coins", None)
        xch_coins = None
        if xch_coin_list:
            xch_coins = {Coin.from_json_dict(xch_coin) for xch_coin in xch_coin_list}
        xch_change_target = request.get("xch_change_target", None)
        if xch_change_target is not None:
            if xch_change_target[:2] == "xch":
                xch_change_ph = decode_puzzle_hash(xch_change_target)
            else:
                xch_change_ph = bytes32(hexstr_to_bytes(xch_change_target))
        else:
            xch_change_ph = None
        new_innerpuzhash = request.get("new_innerpuzhash", None)
        new_p2_puzhash = request.get("new_p2_puzhash", None)
        did_coin_dict = request.get("did_coin", None)
        if did_coin_dict:
            did_coin = Coin.from_json_dict(did_coin_dict)
        else:
            did_coin = None
        did_lineage_parent_hex = request.get("did_lineage_parent", None)
        if did_lineage_parent_hex:
            did_lineage_parent = bytes32(hexstr_to_bytes(did_lineage_parent_hex))
        else:
            did_lineage_parent = None
        mint_from_did = request.get("mint_from_did", False)
        fee = uint64(request.get("fee", 0))

        if mint_from_did:
            txs = await nft_wallet.mint_from_did(
                metadata_list,
                mint_number_start=mint_number_start,
                mint_total=mint_total,
                target_list=target_list,
                xch_coins=xch_coins,
                xch_change_ph=xch_change_ph,
                new_innerpuzhash=new_innerpuzhash,
                new_p2_puzhash=new_p2_puzhash,
                did_coin=did_coin,
                did_lineage_parent=did_lineage_parent,
                fee=fee,
                tx_config=tx_config,
                extra_conditions=extra_conditions,
            )
        else:
            txs = await nft_wallet.mint_from_xch(
                metadata_list,
                mint_number_start=mint_number_start,
                mint_total=mint_total,
                target_list=target_list,
                xch_coins=xch_coins,
                xch_change_ph=xch_change_ph,
                fee=fee,
                tx_config=tx_config,
                extra_conditions=extra_conditions,
            )
        sb = txs[0].spend_bundle
        assert sb is not None
        nft_id_list = []
        for cs in sb.coin_spends:
            if cs.coin.puzzle_hash == nft_puzzles.LAUNCHER_PUZZLE_HASH:
                nft_id_list.append(encode_puzzle_hash(cs.coin.name(), AddressType.NFT.hrp(self.service.config)))
        return {
            "success": True,
            "spend_bundle": sb,
            "nft_id_list": nft_id_list,
        }

    async def get_coin_records(self, request: Dict[str, Any]) -> EndpointResult:
        parsed_request = GetCoinRecords.from_json_dict(request)

        if parsed_request.limit != uint32.MAXIMUM and parsed_request.limit > self.max_get_coin_records_limit:
            raise ValueError(f"limit of {self.max_get_coin_records_limit} exceeded: {parsed_request.limit}")

        for filter_name, filter in {
            "coin_id_filter": parsed_request.coin_id_filter,
            "puzzle_hash_filter": parsed_request.puzzle_hash_filter,
            "parent_coin_id_filter": parsed_request.parent_coin_id_filter,
            "amount_filter": parsed_request.amount_filter,
        }.items():
            if filter is None:
                continue
            if len(filter.values) > self.max_get_coin_records_filter_items:
                raise ValueError(
                    f"{filter_name} max items {self.max_get_coin_records_filter_items} exceeded: {len(filter.values)}"
                )

        result = await self.service.wallet_state_manager.coin_store.get_coin_records(
            offset=parsed_request.offset,
            limit=parsed_request.limit,
            wallet_id=parsed_request.wallet_id,
            wallet_type=None if parsed_request.wallet_type is None else WalletType(parsed_request.wallet_type),
            coin_type=None if parsed_request.coin_type is None else CoinType(parsed_request.coin_type),
            coin_id_filter=parsed_request.coin_id_filter,
            puzzle_hash_filter=parsed_request.puzzle_hash_filter,
            parent_coin_id_filter=parsed_request.parent_coin_id_filter,
            amount_filter=parsed_request.amount_filter,
            amount_range=parsed_request.amount_range,
            confirmed_range=parsed_request.confirmed_range,
            spent_range=parsed_request.spent_range,
            order=CoinRecordOrder(parsed_request.order),
            reverse=parsed_request.reverse,
            include_total_count=parsed_request.include_total_count,
        )

        return {
            "coin_records": [coin_record.to_json_dict_parsed_metadata() for coin_record in result.records],
            "total_count": result.total_count,
        }

    async def get_farmed_amount(self, request: Dict[str, Any]) -> EndpointResult:
        tx_records: List[TransactionRecord] = await self.service.wallet_state_manager.tx_store.get_farming_rewards()
        amount = 0
        pool_reward_amount = 0
        farmer_reward_amount = 0
        fee_amount = 0
        blocks_won = 0
        last_height_farmed = uint32(0)
        for record in tx_records:
            if record.wallet_id not in self.service.wallet_state_manager.wallets:
                continue
            if record.type == TransactionType.COINBASE_REWARD.value:
                if self.service.wallet_state_manager.wallets[record.wallet_id].type() == WalletType.POOLING_WALLET:
                    # Don't add pool rewards for pool wallets.
                    continue
                pool_reward_amount += record.amount
            height = record.height_farmed(self.service.constants.GENESIS_CHALLENGE)
            # .get_farming_rewards() above queries for only confirmed records.  This
            # could be hinted by making TransactionRecord generic but streamable can't
            # handle that presently.  Existing code would have raised an exception
            # anyway if this were to fail and we already have an assert below.
            assert height is not None
            if record.type == TransactionType.FEE_REWARD.value:
                base_farmer_reward = calculate_base_farmer_reward(height)
                fee_amount += record.amount - base_farmer_reward
                farmer_reward_amount += base_farmer_reward
                blocks_won += 1
            if height > last_height_farmed:
                last_height_farmed = height
            amount += record.amount

        last_time_farmed = uint64(
            await self.service.get_timestamp_for_height(last_height_farmed) if last_height_farmed > 0 else 0
        )
        assert amount == pool_reward_amount + farmer_reward_amount + fee_amount
        return {
            "farmed_amount": amount,
            "pool_reward_amount": pool_reward_amount,
            "farmer_reward_amount": farmer_reward_amount,
            "fee_amount": fee_amount,
            "last_height_farmed": last_height_farmed,
            "last_time_farmed": last_time_farmed,
            "blocks_won": blocks_won,
        }

    @tx_endpoint
    async def create_signed_transaction(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
        hold_lock: bool = True,
    ) -> EndpointResult:
        if "wallet_id" in request:
            wallet_id = uint32(request["wallet_id"])
            wallet = self.service.wallet_state_manager.wallets[wallet_id]
        else:
            wallet = self.service.wallet_state_manager.main_wallet

        assert isinstance(
            wallet, (Wallet, CATWallet, CRCATWallet)
        ), "create_signed_transaction only works for standard and CAT wallets"

        if "additions" not in request or len(request["additions"]) < 1:
            raise ValueError("Specify additions list")

        additions: List[Dict[str, Any]] = request["additions"]
        amount_0: uint64 = uint64(additions[0]["amount"])
        assert amount_0 <= self.service.constants.MAX_COIN_AMOUNT
        puzzle_hash_0 = bytes32.from_hexstr(additions[0]["puzzle_hash"])
        if len(puzzle_hash_0) != 32:
            raise ValueError(f"Address must be 32 bytes. {puzzle_hash_0.hex()}")

        memos_0 = [] if "memos" not in additions[0] else [mem.encode("utf-8") for mem in additions[0]["memos"]]

        additional_outputs: List[Payment] = []
        for addition in additions[1:]:
            receiver_ph = bytes32.from_hexstr(addition["puzzle_hash"])
            if len(receiver_ph) != 32:
                raise ValueError(f"Address must be 32 bytes. {receiver_ph.hex()}")
            amount = uint64(addition["amount"])
            if amount > self.service.constants.MAX_COIN_AMOUNT:
                raise ValueError(f"Coin amount cannot exceed {self.service.constants.MAX_COIN_AMOUNT}")
            memos = [] if "memos" not in addition else [mem.encode("utf-8") for mem in addition["memos"]]
            additional_outputs.append(Payment(receiver_ph, amount, memos))

        fee: uint64 = uint64(request.get("fee", 0))

        coins = None
        if "coins" in request and len(request["coins"]) > 0:
            coins = {Coin.from_json_dict(coin_json) for coin_json in request["coins"]}

        coin_announcements: Optional[Set[Announcement]] = None
        if (
            "coin_announcements" in request
            and request["coin_announcements"] is not None
            and len(request["coin_announcements"]) > 0
        ):
            coin_announcements = {
                Announcement(
                    bytes32.from_hexstr(announcement["coin_id"]),
                    hexstr_to_bytes(announcement["message"]),
                    hexstr_to_bytes(announcement["morph_bytes"])
                    if "morph_bytes" in announcement and len(announcement["morph_bytes"]) > 0
                    else None,
                )
                for announcement in request["coin_announcements"]
            }

        puzzle_announcements: Optional[Set[Announcement]] = None
        if (
            "puzzle_announcements" in request
            and request["puzzle_announcements"] is not None
            and len(request["puzzle_announcements"]) > 0
        ):
            puzzle_announcements = {
                Announcement(
                    bytes32.from_hexstr(announcement["puzzle_hash"]),
                    hexstr_to_bytes(announcement["message"]),
                    hexstr_to_bytes(announcement["morph_bytes"])
                    if "morph_bytes" in announcement and len(announcement["morph_bytes"]) > 0
                    else None,
                )
                for announcement in request["puzzle_announcements"]
            }

        async def _generate_signed_transaction() -> EndpointResult:
            if isinstance(wallet, Wallet):
                [tx] = await wallet.generate_signed_transaction(
                    amount_0,
                    bytes32(puzzle_hash_0),
                    tx_config,
                    fee,
                    coins=coins,
                    ignore_max_send_amount=True,
                    primaries=additional_outputs,
                    memos=memos_0,
                    coin_announcements_to_consume=coin_announcements,
                    puzzle_announcements_to_consume=puzzle_announcements,
                    extra_conditions=extra_conditions,
                )
                signed_tx = tx.to_json_dict_convenience(self.service.config)

                return {"signed_txs": [signed_tx], "signed_tx": signed_tx}

            else:
                assert isinstance(wallet, CATWallet)

                txs = await wallet.generate_signed_transaction(
                    [amount_0] + [output.amount for output in additional_outputs],
                    [bytes32(puzzle_hash_0)] + [output.puzzle_hash for output in additional_outputs],
                    tx_config,
                    fee,
                    coins=coins,
                    ignore_max_send_amount=True,
                    memos=[memos_0] + [output.memos for output in additional_outputs],
                    coin_announcements_to_consume=coin_announcements,
                    puzzle_announcements_to_consume=puzzle_announcements,
                    extra_conditions=extra_conditions,
                )
                signed_txs = [tx.to_json_dict_convenience(self.service.config) for tx in txs]

                return {"signed_txs": signed_txs, "signed_tx": signed_txs[0]}

        if hold_lock:
            async with self.service.wallet_state_manager.lock:
                return await _generate_signed_transaction()
        else:
            return await _generate_signed_transaction()

    ##########################################################################################
    # Pool Wallet
    ##########################################################################################
    @tx_endpoint
    async def pw_join_pool(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        fee = uint64(request.get("fee", 0))
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=PoolWallet)

        pool_wallet_info: PoolWalletInfo = await wallet.get_current_state()
        owner_pubkey = pool_wallet_info.current.owner_pubkey
        target_puzzlehash = None

        if await self.service.wallet_state_manager.synced() is False:
            raise ValueError("Wallet needs to be fully synced.")

        if "target_puzzlehash" in request:
            target_puzzlehash = bytes32(hexstr_to_bytes(request["target_puzzlehash"]))
        assert target_puzzlehash is not None
        new_target_state: PoolState = create_pool_state(
            FARMING_TO_POOL,
            target_puzzlehash,
            owner_pubkey,
            request["pool_url"],
            uint32(request["relative_lock_height"]),
        )

        async with self.service.wallet_state_manager.lock:
            total_fee, tx, fee_tx = await wallet.join_pool(new_target_state, fee, tx_config)
            return {"total_fee": total_fee, "transaction": tx, "fee_transaction": fee_tx}

    @tx_endpoint
    async def pw_self_pool(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        # Leaving a pool requires two state transitions.
        # First we transition to PoolSingletonState.LEAVING_POOL
        # Then we transition to FARMING_TO_POOL or SELF_POOLING
        fee = uint64(request.get("fee", 0))
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=PoolWallet)

        if await self.service.wallet_state_manager.synced() is False:
            raise ValueError("Wallet needs to be fully synced.")

        async with self.service.wallet_state_manager.lock:
            total_fee, tx, fee_tx = await wallet.self_pool(fee, tx_config)
            return {"total_fee": total_fee, "transaction": tx, "fee_transaction": fee_tx}

    @tx_endpoint
    async def pw_absorb_rewards(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        """Perform a sweep of the p2_singleton rewards controlled by the pool wallet singleton"""
        if await self.service.wallet_state_manager.synced() is False:
            raise ValueError("Wallet needs to be fully synced before collecting rewards")
        fee = uint64(request.get("fee", 0))
        max_spends_in_tx = request.get("max_spends_in_tx", None)
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=PoolWallet)

        assert isinstance(wallet, PoolWallet)
        async with self.service.wallet_state_manager.lock:
            transaction, fee_tx = await wallet.claim_pool_rewards(fee, max_spends_in_tx, tx_config)
            state: PoolWalletInfo = await wallet.get_current_state()
        return {"state": state.to_json_dict(), "transaction": transaction, "fee_transaction": fee_tx}

    async def pw_status(self, request: Dict[str, Any]) -> EndpointResult:
        """Return the complete state of the Pool wallet with id `request["wallet_id"]`"""
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.get_wallet(id=wallet_id, required_type=PoolWallet)

        assert isinstance(wallet, PoolWallet)
        state: PoolWalletInfo = await wallet.get_current_state()
        unconfirmed_transactions: List[TransactionRecord] = await wallet.get_unconfirmed_transactions()
        return {
            "state": state.to_json_dict(),
            "unconfirmed_transactions": unconfirmed_transactions,
        }

    ##########################################################################################
    # DataLayer Wallet
    ##########################################################################################
    @tx_endpoint
    async def create_new_dl(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        """Initialize the DataLayer Wallet (only one can exist)"""
        if self.service.wallet_state_manager is None:
            raise ValueError("The wallet service is not currently initialized")

        try:
            dl_wallet = self.service.wallet_state_manager.get_dl_wallet()
        except ValueError:
            async with self.service.wallet_state_manager.lock:
                dl_wallet = await DataLayerWallet.create_new_dl_wallet(self.service.wallet_state_manager)

        try:
            async with self.service.wallet_state_manager.lock:
                dl_tx, std_tx, launcher_id = await dl_wallet.generate_new_reporter(
                    bytes32.from_hexstr(request["root"]),
                    tx_config,
                    fee=request.get("fee", uint64(0)),
                    extra_conditions=extra_conditions,
                )
                await self.service.wallet_state_manager.add_pending_transaction(dl_tx)
                await self.service.wallet_state_manager.add_pending_transaction(std_tx)
        except ValueError as e:
            log.error(f"Error while generating new reporter {e}")
            return {"success": False, "error": str(e)}

        return {
            "success": True,
            "transactions": [tx.to_json_dict_convenience(self.service.config) for tx in (dl_tx, std_tx)],
            "launcher_id": launcher_id,
        }

    async def dl_track_new(self, request: Dict[str, Any]) -> EndpointResult:
        """Initialize the DataLayer Wallet (only one can exist)"""
        if self.service.wallet_state_manager is None:
            raise ValueError("The wallet service is not currently initialized")
        try:
            dl_wallet = self.service.wallet_state_manager.get_dl_wallet()
        except ValueError:
            async with self.service.wallet_state_manager.lock:
                dl_wallet = await DataLayerWallet.create_new_dl_wallet(
                    self.service.wallet_state_manager,
                )
        peer_list = self.service.get_full_node_peers_in_order()
        peer_length = len(peer_list)
        for i, peer in enumerate(peer_list):
            try:
                await dl_wallet.track_new_launcher_id(
                    bytes32.from_hexstr(request["launcher_id"]),
                    peer,
                )
            except LauncherCoinNotFoundError as e:
                if i == peer_length - 1:
                    raise e  # raise the error if we've tried all peers
                continue  # try some other peers, maybe someone has it
        return {}

    async def dl_stop_tracking(self, request: Dict[str, Any]) -> EndpointResult:
        """Initialize the DataLayer Wallet (only one can exist)"""
        if self.service.wallet_state_manager is None:
            raise ValueError("The wallet service is not currently initialized")

        dl_wallet = self.service.wallet_state_manager.get_dl_wallet()
        await dl_wallet.stop_tracking_singleton(bytes32.from_hexstr(request["launcher_id"]))
        return {}

    async def dl_latest_singleton(self, request: Dict[str, Any]) -> EndpointResult:
        """Get the singleton record for the latest singleton of a launcher ID"""
        if self.service.wallet_state_manager is None:
            raise ValueError("The wallet service is not currently initialized")

        only_confirmed = request.get("only_confirmed")
        if only_confirmed is None:
            only_confirmed = False
        wallet = self.service.wallet_state_manager.get_dl_wallet()
        record = await wallet.get_latest_singleton(bytes32.from_hexstr(request["launcher_id"]), only_confirmed)
        return {"singleton": None if record is None else record.to_json_dict()}

    async def dl_singletons_by_root(self, request: Dict[str, Any]) -> EndpointResult:
        """Get the singleton records that contain the specified root"""
        if self.service.wallet_state_manager is None:
            raise ValueError("The wallet service is not currently initialized")

        wallet = self.service.wallet_state_manager.get_dl_wallet()
        records = await wallet.get_singletons_by_root(
            bytes32.from_hexstr(request["launcher_id"]), bytes32.from_hexstr(request["root"])
        )
        records_json = [rec.to_json_dict() for rec in records]
        return {"singletons": records_json}

    @tx_endpoint
    async def dl_update_root(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        """Get the singleton record for the latest singleton of a launcher ID"""
        if self.service.wallet_state_manager is None:
            raise ValueError("The wallet service is not currently initialized")

        wallet = self.service.wallet_state_manager.get_dl_wallet()
        async with self.service.wallet_state_manager.lock:
            records = await wallet.create_update_state_spend(
                bytes32.from_hexstr(request["launcher_id"]),
                bytes32.from_hexstr(request["new_root"]),
                tx_config,
                fee=uint64(request.get("fee", 0)),
                extra_conditions=extra_conditions,
            )
            for record in records:
                await self.service.wallet_state_manager.add_pending_transaction(record)
            return {"tx_record": records[0].to_json_dict_convenience(self.service.config)}

    @tx_endpoint
    async def dl_update_multiple(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        """Update multiple singletons with new merkle roots"""
        if self.service.wallet_state_manager is None:
            return {"success": False, "error": "not_initialized"}

        wallet = self.service.wallet_state_manager.get_dl_wallet()
        async with self.service.wallet_state_manager.lock:
            # TODO: This method should optionally link the singletons with announcements.
            #       Otherwise spends are vulnerable to signature subtraction.
            tx_records: List[TransactionRecord] = []
            for launcher, root in request["updates"].items():
                records = await wallet.create_update_state_spend(
                    bytes32.from_hexstr(launcher),
                    bytes32.from_hexstr(root),
                    tx_config,
                    extra_conditions=extra_conditions,
                )
                tx_records.extend(records)
            # Now that we have all the txs, we need to aggregate them all into just one spend
            modified_txs: List[TransactionRecord] = []
            aggregate_spend = SpendBundle([], G2Element())
            for tx in tx_records:
                if tx.spend_bundle is not None:
                    aggregate_spend = SpendBundle.aggregate([aggregate_spend, tx.spend_bundle])
                    modified_txs.append(dataclasses.replace(tx, spend_bundle=None))
            modified_txs[0] = dataclasses.replace(modified_txs[0], spend_bundle=aggregate_spend)
            for tx in modified_txs:
                await self.service.wallet_state_manager.add_pending_transaction(tx)
            return {"tx_records": [rec.to_json_dict_convenience(self.service.config) for rec in modified_txs]}

    async def dl_history(self, request: Dict[str, Any]) -> EndpointResult:
        """Get the singleton record for the latest singleton of a launcher ID"""
        if self.service.wallet_state_manager is None:
            raise ValueError("The wallet service is not currently initialized")

        wallet = self.service.wallet_state_manager.get_dl_wallet()
        additional_kwargs = {}

        if "min_generation" in request:
            additional_kwargs["min_generation"] = uint32(request["min_generation"])
        if "max_generation" in request:
            additional_kwargs["max_generation"] = uint32(request["max_generation"])
        if "num_results" in request:
            additional_kwargs["num_results"] = uint32(request["num_results"])

        history = await wallet.get_history(bytes32.from_hexstr(request["launcher_id"]), **additional_kwargs)
        history_json = [rec.to_json_dict() for rec in history]
        return {"history": history_json, "count": len(history_json)}

    async def dl_owned_singletons(self, request: Dict[str, Any]) -> EndpointResult:
        """Get all owned singleton records"""
        if self.service.wallet_state_manager is None:
            raise ValueError("The wallet service is not currently initialized")

        wallet = self.service.wallet_state_manager.get_dl_wallet()
        singletons = await wallet.get_owned_singletons()
        singletons_json = [singleton.to_json_dict() for singleton in singletons]

        return {"singletons": singletons_json, "count": len(singletons_json)}

    async def dl_get_mirrors(self, request: Dict[str, Any]) -> EndpointResult:
        """Get all of the mirrors for a specific singleton"""
        if self.service.wallet_state_manager is None:
            raise ValueError("The wallet service is not currently initialized")

        wallet = self.service.wallet_state_manager.get_dl_wallet()
        mirrors_json = []
        for mirror in await wallet.get_mirrors_for_launcher(bytes32.from_hexstr(request["launcher_id"])):
            mirrors_json.append(mirror.to_json_dict())

        return {"mirrors": mirrors_json}

    @tx_endpoint
    async def dl_new_mirror(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        """Add a new on chain message for a specific singleton"""
        if self.service.wallet_state_manager is None:
            raise ValueError("The wallet service is not currently initialized")

        dl_wallet = self.service.wallet_state_manager.get_dl_wallet()
        async with self.service.wallet_state_manager.lock:
            txs = await dl_wallet.create_new_mirror(
                bytes32.from_hexstr(request["launcher_id"]),
                request["amount"],
                [bytes(url, "utf8") for url in request["urls"]],
                tx_config,
                fee=request.get("fee", uint64(0)),
                extra_conditions=extra_conditions,
            )
            for tx in txs:
                await self.service.wallet_state_manager.add_pending_transaction(tx)

        return {
            "transactions": [tx.to_json_dict_convenience(self.service.config) for tx in txs],
        }

    @tx_endpoint
    async def dl_delete_mirror(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        """Remove an existing mirror for a specific singleton"""
        if self.service.wallet_state_manager is None:
            raise ValueError("The wallet service is not currently initialized")

        dl_wallet = self.service.wallet_state_manager.get_dl_wallet()

        async with self.service.wallet_state_manager.lock:
            txs = await dl_wallet.delete_mirror(
                bytes32.from_hexstr(request["coin_id"]),
                self.service.get_full_node_peer(),
                tx_config,
                fee=request.get("fee", uint64(0)),
                extra_conditions=extra_conditions,
            )
            for tx in txs:
                await self.service.wallet_state_manager.add_pending_transaction(tx)

        return {
            "transactions": [tx.to_json_dict_convenience(self.service.config) for tx in txs],
        }

    async def dl_verify_proof(
        self,
        request: Dict[str, Any],
    ) -> EndpointResult:
        """Verify a proof of inclusion for a DL singleton"""
        if self.service.wallet_state_manager is None:
            raise ValueError("The wallet service is not currently initialized")

        res = await dl_verify_proof(
            request,
            peer=self.service.get_full_node_peer(),
            wallet_node=self.service.wallet_state_manager.wallet_node,
            max_cost=self.service.wallet_state_manager.constants.MAX_BLOCK_COST_CLVM,
        )

        return res

    ##########################################################################################
    # Verified Credential
    ##########################################################################################
    @tx_endpoint
    async def vc_mint(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        """
        Mint a verified credential using the assigned DID
        :param request: We require 'did_id' that will be minting the VC and options for a new 'target_address' as well
        as a 'fee' for the mint tx
        :return: a 'vc_record' containing all the information of the soon-to-be-confirmed vc as well as any relevant
        'transactions'
        """

        @streamable
        @dataclasses.dataclass(frozen=True)
        class VCMint(Streamable):
            did_id: str
            target_address: Optional[str] = None
            fee: uint64 = uint64(0)

        parsed_request = VCMint.from_json_dict(request)

        did_id = decode_puzzle_hash(parsed_request.did_id)
        puzhash: Optional[bytes32] = None
        if parsed_request.target_address is not None:
            puzhash = decode_puzzle_hash(parsed_request.target_address)

        vc_wallet: VCWallet = await self.service.wallet_state_manager.get_or_create_vc_wallet()
        vc_record, tx_list = await vc_wallet.launch_new_vc(
            did_id, tx_config, puzhash, parsed_request.fee, extra_conditions=extra_conditions
        )
        for tx in tx_list:
            await self.service.wallet_state_manager.add_pending_transaction(tx)
        return {
            "vc_record": vc_record.to_json_dict(),
            "transactions": [tx.to_json_dict_convenience(self.service.config) for tx in tx_list],
        }

    async def vc_get(self, request: Dict[str, Any]) -> EndpointResult:
        """
        Given a launcher ID get the verified credential
        :param request: the 'vc_id' launcher id of a verifiable credential
        :return: the 'vc_record' representing the specified verifiable credential
        """

        @streamable
        @dataclasses.dataclass(frozen=True)
        class VCGet(Streamable):
            vc_id: bytes32

        parsed_request = VCGet.from_json_dict(request)

        vc_record = await self.service.wallet_state_manager.vc_store.get_vc_record(parsed_request.vc_id)
        return {"vc_record": vc_record}

    async def vc_get_list(self, request: Dict[str, Any]) -> EndpointResult:
        """
        Get a list of verified credentials
        :param request: optional parameters for pagination 'start' and 'count'
        :return: all 'vc_records' in the specified range and any 'proofs' associated with the roots contained within
        """

        @streamable
        @dataclasses.dataclass(frozen=True)
        class VCGetList(Streamable):
            start: uint32 = uint32(0)
            end: uint32 = uint32(50)

        parsed_request = VCGetList.from_json_dict(request)

        vc_list = await self.service.wallet_state_manager.vc_store.get_vc_record_list(
            parsed_request.start, parsed_request.end
        )
        return {
            "vc_records": [{"coin_id": "0x" + vc.vc.coin.name().hex(), **vc.to_json_dict()} for vc in vc_list],
            "proofs": {
                rec.vc.proof_hash.hex(): None if fetched_proof is None else fetched_proof.key_value_pairs
                for rec in vc_list
                if rec.vc.proof_hash is not None
                for fetched_proof in (
                    await self.service.wallet_state_manager.vc_store.get_proofs_for_root(rec.vc.proof_hash),
                )
            },
        }

    @tx_endpoint
    async def vc_spend(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        """
        Spend a verified credential
        :param request: Required 'vc_id' launcher id of the vc we wish to spend. Optional paramaters for a 'new_puzhash'
        for the vc to end up at and 'new_proof_hash' & 'provider_inner_puzhash' which can be used to update the vc's
        proofs. Also standard 'fee' & 'reuse_puzhash' parameters for the transaction.
        :return: a list of all relevant 'transactions' (TransactionRecord) that this spend generates (VC TX + fee TX)
        """

        @streamable
        @dataclasses.dataclass(frozen=True)
        class VCSpend(Streamable):
            vc_id: bytes32
            new_puzhash: Optional[bytes32] = None
            new_proof_hash: Optional[bytes32] = None
            provider_inner_puzhash: Optional[bytes32] = None
            fee: uint64 = uint64(0)

        parsed_request = VCSpend.from_json_dict(request)

        vc_wallet: VCWallet = await self.service.wallet_state_manager.get_or_create_vc_wallet()

        txs = await vc_wallet.generate_signed_transaction(
            parsed_request.vc_id,
            tx_config,
            parsed_request.fee,
            parsed_request.new_puzhash,
            new_proof_hash=parsed_request.new_proof_hash,
            provider_inner_puzhash=parsed_request.provider_inner_puzhash,
            extra_conditions=extra_conditions,
        )
        for tx in txs:
            await self.service.wallet_state_manager.add_pending_transaction(tx)

        return {
            "transactions": [tx.to_json_dict_convenience(self.service.config) for tx in txs],
        }

    async def vc_add_proofs(self, request: Dict[str, Any]) -> EndpointResult:
        """
        Add a set of proofs to the DB that can be used when spending a VC. VCs are near useless until their proofs have
        been added.
        :param request: 'proofs' is a dictionary of key/value pairs
        :return:
        """
        vc_wallet: VCWallet = await self.service.wallet_state_manager.get_or_create_vc_wallet()

        await vc_wallet.store.add_vc_proofs(VCProofs(request["proofs"]))

        return {}

    async def vc_get_proofs_for_root(self, request: Dict[str, Any]) -> EndpointResult:
        """
        Given a specified vc root, get any proofs associated with that root.
        :param request: must specify 'root' representing the tree hash of some set of proofs
        :return: a dictionary of root hashes mapped to dictionaries of key value pairs of 'proofs'
        """

        @streamable
        @dataclasses.dataclass(frozen=True)
        class VCGetProofsForRoot(Streamable):
            root: bytes32

        parsed_request = VCGetProofsForRoot.from_json_dict(request)
        vc_wallet: VCWallet = await self.service.wallet_state_manager.get_or_create_vc_wallet()

        vc_proofs: Optional[VCProofs] = await vc_wallet.store.get_proofs_for_root(parsed_request.root)
        if vc_proofs is None:
            raise ValueError("no proofs found for specified root")  # pragma: no cover
        return {"proofs": vc_proofs.key_value_pairs}

    @tx_endpoint
    async def vc_revoke(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        """
        Revoke an on chain VC provided the correct DID is available
        :param request: required 'vc_parent_id' for the VC coin. Standard transaction params 'fee' & 'reuse_puzhash'.
        :return: a list of all relevant 'transactions' (TransactionRecord) that this spend generates (VC TX + fee TX)
        """

        @streamable
        @dataclasses.dataclass(frozen=True)
        class VCRevoke(Streamable):
            vc_parent_id: bytes32
            fee: uint64 = uint64(0)

        parsed_request = VCRevoke.from_json_dict(request)
        vc_wallet: VCWallet = await self.service.wallet_state_manager.get_or_create_vc_wallet()

        txs = await vc_wallet.revoke_vc(
            parsed_request.vc_parent_id,
            self.service.get_full_node_peer(),
            tx_config,
            parsed_request.fee,
            extra_conditions=extra_conditions,
        )
        for tx in txs:
            await self.service.wallet_state_manager.add_pending_transaction(tx)

        return {
            "transactions": [tx.to_json_dict_convenience(self.service.config) for tx in txs],
        }

    @tx_endpoint
    async def crcat_approve_pending(
        self,
        request: Dict[str, Any],
        tx_config: TXConfig = DEFAULT_TX_CONFIG,
        extra_conditions: Tuple[Condition, ...] = tuple(),
    ) -> EndpointResult:
        """
        Moving any "pending approval" CR-CATs into the spendable balance of the wallet
        :param request: Required 'wallet_id'. Optional 'min_amount_to_claim' (deafult: full balance).
        Standard transaction params 'fee' & 'reuse_puzhash'.
        :return: a list of all relevant 'transactions' (TransactionRecord) that this spend generates:
        (CRCAT TX + fee TX)
        """

        @streamable
        @dataclasses.dataclass(frozen=True)
        class CRCATApprovePending(Streamable):
            wallet_id: uint32
            min_amount_to_claim: uint64
            fee: uint64 = uint64(0)

        parsed_request = CRCATApprovePending.from_json_dict(request)
        cr_cat_wallet = self.service.wallet_state_manager.wallets[parsed_request.wallet_id]
        assert isinstance(cr_cat_wallet, CRCATWallet)

        txs = await cr_cat_wallet.claim_pending_approval_balance(
            parsed_request.min_amount_to_claim,
            tx_config,
            fee=parsed_request.fee,
            extra_conditions=extra_conditions,
        )
        for tx in txs:
            await self.service.wallet_state_manager.add_pending_transaction(tx)

        return {
            "transactions": [tx.to_json_dict_convenience(self.service.config) for tx in txs],
        }
