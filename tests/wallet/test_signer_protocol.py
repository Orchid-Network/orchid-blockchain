from __future__ import annotations

import dataclasses
from typing import List, Optional

import pytest
from chia_rs import AugSchemeMPL, G1Element, G2Element, PrivateKey

from chia.rpc.wallet_rpc_client import WalletRpcClient
from chia.types.blockchain_format.coin import Coin as ConsensusCoin
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_spend import CoinSpend
from chia.types.spend_bundle import SpendBundle
from chia.util.ints import uint64
from chia.util.streamable import ConversionError, Streamable, streamable
from chia.wallet.conditions import AggSigMe
from chia.wallet.derivation_record import DerivationRecord
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import (
    DEFAULT_HIDDEN_PUZZLE_HASH,
    calculate_synthetic_offset,
)
from chia.wallet.util.blind_signer_tl import (
    BLIND_SIGNER_TRANSPORT,
    BSTLPathHint,
    BSTLSigningInstructions,
    BSTLSigningResponse,
    BSTLSigningTarget,
    BSTLSumHint,
)
from chia.wallet.util.signer_protocol import (
    ClvmStreamable,
    Coin,
    KeyHints,
    PathHint,
    SignedTransaction,
    SigningInstructions,
    SigningResponse,
    SigningTarget,
    Spend,
    SumHint,
    TransactionInfo,
    TransportLayer,
    TransportLayerMapping,
    UnsignedTransaction,
    clvm_serialization_mode,
)
from chia.wallet.util.tx_config import DEFAULT_COIN_SELECTION_CONFIG
from chia.wallet.wallet import Wallet
from chia.wallet.wallet_state_manager import WalletStateManager
from tests.wallet.conftest import WalletStateTransition, WalletTestFramework


def test_signing_serialization() -> None:
    pubkey: G1Element = G1Element()
    message: bytes = b"message"

    coin: ConsensusCoin = ConsensusCoin(bytes32([0] * 32), bytes32([0] * 32), uint64(0))
    puzzle: Program = Program.to(1)
    solution: Program = Program.to([AggSigMe(pubkey, message).to_program()])

    coin_spend: CoinSpend = CoinSpend(coin, puzzle, solution)
    assert Spend.from_coin_spend(coin_spend).as_coin_spend() == coin_spend

    tx: UnsignedTransaction = UnsignedTransaction(
        TransactionInfo([Spend.from_coin_spend(coin_spend)]),
        SigningInstructions(
            KeyHints([], []),
            [SigningTarget(bytes(pubkey), message, bytes32([1] * 32))],
        ),
    )

    assert tx == UnsignedTransaction.from_program(Program.from_bytes(bytes(tx.as_program())))

    as_json_dict = {
        "coin": {
            "parent_coin_id": "0x" + tx.transaction_info.spends[0].coin.parent_coin_id.hex(),
            "puzzle_hash": "0x" + tx.transaction_info.spends[0].coin.puzzle_hash.hex(),
            "amount": tx.transaction_info.spends[0].coin.amount,
        },
        "puzzle": "0x" + bytes(tx.transaction_info.spends[0].puzzle).hex(),
        "solution": "0x" + bytes(tx.transaction_info.spends[0].solution).hex(),
    }
    assert tx.transaction_info.spends[0].to_json_dict() == as_json_dict

    # Test from_json_dict with the special case where it encounters the as_program serialization in the middle of JSON
    assert tx.transaction_info.spends[0] == Spend.from_json_dict(
        {
            "coin": bytes(tx.transaction_info.spends[0].coin.as_program()).hex(),
            "puzzle": bytes(tx.transaction_info.spends[0].puzzle).hex(),
            "solution": bytes(tx.transaction_info.spends[0].solution).hex(),
        }
    )

    # Test the optional serialization as blobs
    with clvm_serialization_mode(True):
        assert (
            tx.transaction_info.spends[0].to_json_dict()
            == bytes(tx.transaction_info.spends[0].as_program()).hex()  # type: ignore[comparison-overlap]
        )

    # Make sure it's still a dict if using a Streamable object
    @streamable
    @dataclasses.dataclass(frozen=True)
    class TempStreamable(Streamable):
        streamable_key: Spend

    with clvm_serialization_mode(True):
        assert TempStreamable(tx.transaction_info.spends[0]).to_json_dict() == {
            "streamable_key": bytes(tx.transaction_info.spends[0].as_program()).hex()
        }

    with clvm_serialization_mode(False):
        assert TempStreamable(tx.transaction_info.spends[0]).to_json_dict() == {"streamable_key": as_json_dict}

    with clvm_serialization_mode(False):
        assert TempStreamable(tx.transaction_info.spends[0]).to_json_dict() == {"streamable_key": as_json_dict}
        with clvm_serialization_mode(True):
            assert TempStreamable(tx.transaction_info.spends[0]).to_json_dict() == {
                "streamable_key": bytes(tx.transaction_info.spends[0].as_program()).hex()
            }
            with clvm_serialization_mode(False):
                assert TempStreamable(tx.transaction_info.spends[0]).to_json_dict() == {"streamable_key": as_json_dict}

    streamable_blob = bytes(tx.transaction_info.spends[0])
    with clvm_serialization_mode(True):
        clvm_streamable_blob = bytes(tx.transaction_info.spends[0])

    assert streamable_blob != clvm_streamable_blob
    Spend.from_bytes(streamable_blob)
    Spend.from_bytes(clvm_streamable_blob)
    assert Spend.from_bytes(streamable_blob) == Spend.from_bytes(clvm_streamable_blob) == tx.transaction_info.spends[0]

    with clvm_serialization_mode(False):
        assert bytes(tx.transaction_info.spends[0]) == streamable_blob

    inside_streamable_blob = bytes(TempStreamable(tx.transaction_info.spends[0]))
    with clvm_serialization_mode(True):
        inside_clvm_streamable_blob = bytes(TempStreamable(tx.transaction_info.spends[0]))

    assert inside_streamable_blob != inside_clvm_streamable_blob
    assert (
        TempStreamable.from_bytes(inside_streamable_blob)
        == TempStreamable.from_bytes(inside_clvm_streamable_blob)
        == TempStreamable(tx.transaction_info.spends[0])
    )

    # Test some json loading errors

    with pytest.raises(ConversionError):
        Spend.from_json_dict("blah")
    with pytest.raises(ConversionError):
        UnsignedTransaction.from_json_dict(streamable_blob.hex())


class FooSpend(ClvmStreamable):
    coin: Coin
    blah: Program
    blah_also: Program = dataclasses.field(metadata=dict(key="solution"))

    @staticmethod
    def from_wallet_api(_from: Spend) -> FooSpend:
        return FooSpend(
            _from.coin,
            _from.puzzle,
            _from.solution,
        )

    @staticmethod
    def to_wallet_api(_from: FooSpend) -> Spend:
        return Spend(
            _from.coin,
            _from.blah,
            _from.blah_also,
        )


def test_transport_layer() -> None:
    FOO_TRANSPORT = TransportLayer(
        [
            TransportLayerMapping(
                Spend,
                FooSpend,
                FooSpend.from_wallet_api,
                FooSpend.to_wallet_api,
            )
        ]
    )

    spend = Spend(
        Coin(bytes32([0] * 32), bytes32([0] * 32), uint64(0)),
        Program.to(1),
        Program.to([]),
    )

    with clvm_serialization_mode(True):
        spend_bytes = bytes(spend)

    spend_program = Program.from_bytes(spend_bytes)
    assert spend_program.at("ff") == Program.to("coin")
    assert spend_program.at("rff") == Program.to("puzzle")
    assert spend_program.at("rrff") == Program.to("solution")

    with clvm_serialization_mode(True, FOO_TRANSPORT):
        foo_spend_bytes = bytes(spend)
        assert foo_spend_bytes.hex() == spend.to_json_dict()  # type: ignore[comparison-overlap]
        assert spend == Spend.from_bytes(foo_spend_bytes)
        assert spend == Spend.from_json_dict(foo_spend_bytes.hex())

    # Deserialization should only work now if using the transport layer
    with pytest.raises(Exception):
        Spend.from_bytes(foo_spend_bytes)
    with pytest.raises(Exception):
        Spend.from_json_dict(foo_spend_bytes.hex())

    assert foo_spend_bytes != spend_bytes
    foo_spend_program = Program.from_bytes(foo_spend_bytes)
    assert foo_spend_program.at("ff") == Program.to("coin")
    assert foo_spend_program.at("rff") == Program.to("blah")
    assert foo_spend_program.at("rrff") == Program.to("solution")


def test_blind_signer_transport_layer() -> None:
    sum_hints: List[SumHint] = [SumHint([b"a", b"b", b"c"], b"offset"), SumHint([b"c", b"b", b"a"], b"offset2")]
    path_hints: List[PathHint] = [
        PathHint(b"root1", [uint64(1), uint64(2), uint64(3)]),
        PathHint(b"root2", [uint64(4), uint64(5), uint64(6)]),
    ]
    signing_targets: List[SigningTarget] = [
        SigningTarget(b"pubkey", b"message", bytes32([0] * 32)),
        SigningTarget(b"pubkey2", b"message2", bytes32([1] * 32)),
    ]

    instructions: SigningInstructions = SigningInstructions(
        KeyHints(sum_hints, path_hints),
        signing_targets,
    )
    signing_response: SigningResponse = SigningResponse(
        b"signature",
        bytes32([1] * 32),
    )

    bstl_sum_hints: List[BSTLSumHint] = [
        BSTLSumHint([b"a", b"b", b"c"], b"offset"),
        BSTLSumHint([b"c", b"b", b"a"], b"offset2"),
    ]
    bstl_path_hints: List[BSTLPathHint] = [
        BSTLPathHint(b"root1", [uint64(1), uint64(2), uint64(3)]),
        BSTLPathHint(b"root2", [uint64(4), uint64(5), uint64(6)]),
    ]
    bstl_signing_targets: List[BSTLSigningTarget] = [
        BSTLSigningTarget(b"pubkey", b"message", bytes32([0] * 32)),
        BSTLSigningTarget(b"pubkey2", b"message2", bytes32([1] * 32)),
    ]

    bstl_instructions: BSTLSigningInstructions = BSTLSigningInstructions(
        bstl_sum_hints,
        bstl_path_hints,
        bstl_signing_targets,
    )
    bstl_signing_response: BSTLSigningResponse = BSTLSigningResponse(
        b"signature",
        bytes32([1] * 32),
    )
    with clvm_serialization_mode(True, None):
        bstl_instructions_bytes = bytes(bstl_instructions)
        bstl_signing_response_bytes = bytes(bstl_signing_response)

    with clvm_serialization_mode(True, BLIND_SIGNER_TRANSPORT):
        instructions_bytes = bytes(instructions)
        signing_response_bytes = bytes(signing_response)
        assert instructions_bytes == bstl_instructions_bytes == bytes(bstl_instructions)
        assert signing_response_bytes == bstl_signing_response_bytes == bytes(bstl_signing_response)

    # Deserialization should only work now if using the transport layer
    with pytest.raises(Exception):
        SigningInstructions.from_bytes(instructions_bytes)
    with pytest.raises(Exception):
        SigningResponse.from_bytes(signing_response_bytes)

    assert BSTLSigningInstructions.from_bytes(instructions_bytes) == bstl_instructions
    assert BSTLSigningResponse.from_bytes(signing_response_bytes) == bstl_signing_response
    with clvm_serialization_mode(True, BLIND_SIGNER_TRANSPORT):
        assert SigningInstructions.from_bytes(instructions_bytes) == instructions
        assert SigningResponse.from_bytes(signing_response_bytes) == signing_response

    assert Program.from_bytes(instructions_bytes).at("ff") == Program.to("s")
    assert Program.from_bytes(signing_response_bytes).at("ff") == Program.to("s")


@pytest.mark.parametrize(
    "wallet_environments",
    [
        {
            "num_environments": 1,
            "blocks_needed": [1],
            "trusted": True,
            "reuse_puzhash": True,
        }
    ],
    indirect=True,
)
@pytest.mark.anyio
async def test_p2dohp_wallet_signer_protocol(wallet_environments: WalletTestFramework) -> None:
    wallet: Wallet = wallet_environments.environments[0].xch_wallet
    wallet_state_manager: WalletStateManager = wallet_environments.environments[0].wallet_state_manager
    wallet_rpc: WalletRpcClient = wallet_environments.environments[0].rpc_client

    # Test first that we can properly examine and sign a regular transaction
    [coin] = await wallet.select_coins(uint64(0), DEFAULT_COIN_SELECTION_CONFIG)
    puzzle: Program = await wallet.puzzle_for_puzzle_hash(coin.puzzle_hash)
    delegated_puzzle: Program = Program.to(None)
    delegated_puzzle_hash: bytes32 = delegated_puzzle.get_tree_hash()
    solution: Program = Program.to([None, None, None])

    coin_spend: CoinSpend = CoinSpend(
        coin,
        puzzle,
        solution,
    )

    derivation_record: Optional[
        DerivationRecord
    ] = await wallet_state_manager.puzzle_store.get_derivation_record_for_puzzle_hash(coin.puzzle_hash)
    assert derivation_record is not None
    pubkey: G1Element = derivation_record.pubkey
    synthetic_pubkey: G1Element = G1Element.from_bytes(puzzle.uncurry()[1].at("f").atom)
    message: bytes = delegated_puzzle_hash + coin.name() + wallet_state_manager.constants.AGG_SIG_ME_ADDITIONAL_DATA

    utx: UnsignedTransaction = UnsignedTransaction(
        TransactionInfo([Spend.from_coin_spend(coin_spend)]),
        (await wallet_rpc.gather_signing_info(spends=[Spend.from_coin_spend(coin_spend)])).signing_instructions,
    )
    assert utx.signing_instructions.key_hints.sum_hints == [
        SumHint(
            [pubkey.get_fingerprint().to_bytes(4, "big")],
            calculate_synthetic_offset(pubkey, DEFAULT_HIDDEN_PUZZLE_HASH).to_bytes(32, "big"),
        )
    ]
    assert utx.signing_instructions.key_hints.path_hints == [
        PathHint(
            wallet_state_manager.root_pubkey.get_fingerprint().to_bytes(4, "big"),
            [uint64(12381), uint64(8444), uint64(2), uint64(derivation_record.index)],
        )
    ]
    assert len(utx.signing_instructions.targets) == 1
    assert utx.signing_instructions.targets[0].pubkey == bytes(synthetic_pubkey)
    assert utx.signing_instructions.targets[0].message == message

    signing_responses: List[SigningResponse] = await wallet_state_manager.execute_signing_instructions(
        utx.signing_instructions
    )
    assert len(signing_responses) == 1
    assert signing_responses[0].hook == utx.signing_instructions.targets[0].hook
    assert AugSchemeMPL.verify(synthetic_pubkey, message, G2Element.from_bytes(signing_responses[0].signature))

    # Now test that we can partially sign a transaction
    ACS: Program = Program.to(1)
    ACS_PH: Program = Program.to(1).get_tree_hash()
    not_our_private_key: PrivateKey = PrivateKey.from_bytes(bytes(32))
    not_our_pubkey: G1Element = not_our_private_key.get_g1()
    not_our_message: bytes = b"not our message"
    not_our_coin: ConsensusCoin = ConsensusCoin(
        bytes32([0] * 32),
        ACS_PH,
        uint64(0),
    )
    not_our_coin_spend: CoinSpend = CoinSpend(not_our_coin, ACS, Program.to([[49, not_our_pubkey, not_our_message]]))

    not_our_utx: UnsignedTransaction = UnsignedTransaction(
        TransactionInfo([Spend.from_coin_spend(coin_spend), Spend.from_coin_spend(not_our_coin_spend)]),
        (
            await wallet_rpc.gather_signing_info(
                spends=[Spend.from_coin_spend(coin_spend), Spend.from_coin_spend(not_our_coin_spend)]
            )
        ).signing_instructions,
    )
    assert not_our_utx.signing_instructions.key_hints == utx.signing_instructions.key_hints
    assert len(not_our_utx.signing_instructions.targets) == 2
    assert not_our_utx.signing_instructions.targets[0].pubkey == bytes(synthetic_pubkey)
    assert not_our_utx.signing_instructions.targets[0].message == bytes(message)
    assert not_our_utx.signing_instructions.targets[1].pubkey == bytes(not_our_pubkey)
    assert not_our_utx.signing_instructions.targets[1].message == bytes(not_our_message)
    not_our_signing_instructions: SigningInstructions = not_our_utx.signing_instructions
    with pytest.raises(ValueError, match=r"not found \(or path/sum hinted to\)"):
        await wallet_state_manager.execute_signing_instructions(not_our_signing_instructions)
    with pytest.raises(ValueError, match=r"No pubkey found \(or path hinted to\) for fingerprint"):
        not_our_signing_instructions = dataclasses.replace(
            not_our_signing_instructions,
            key_hints=dataclasses.replace(
                not_our_signing_instructions.key_hints,
                sum_hints=[
                    *not_our_signing_instructions.key_hints.sum_hints,
                    SumHint([bytes(not_our_pubkey)], b""),
                ],
            ),
        )
        await wallet_state_manager.execute_signing_instructions(not_our_signing_instructions)
    with pytest.raises(ValueError, match="No root pubkey for fingerprint"):
        not_our_signing_instructions = dataclasses.replace(
            not_our_signing_instructions,
            key_hints=dataclasses.replace(
                not_our_signing_instructions.key_hints,
                path_hints=[
                    *not_our_signing_instructions.key_hints.path_hints,
                    PathHint(bytes(not_our_pubkey), [uint64(0)]),
                ],
            ),
        )
        await wallet_state_manager.execute_signing_instructions(not_our_signing_instructions)
    signing_responses_2 = await wallet_state_manager.execute_signing_instructions(
        not_our_signing_instructions, partial_allowed=True
    )
    assert len(signing_responses_2) == 1
    assert signing_responses_2 == signing_responses

    signed_txs: List[SignedTransaction] = (
        await wallet_rpc.apply_signatures(
            spends=[Spend.from_coin_spend(coin_spend)], signing_responses=signing_responses
        )
    ).signed_transactions
    await wallet_rpc.submit_transactions(signed_transactions=signed_txs)
    await wallet_environments.full_node.wait_bundle_ids_in_mempool(
        [
            SpendBundle(
                [spend.as_coin_spend() for tx in signed_txs for spend in tx.transaction_info.spends],
                G2Element.from_bytes(signing_responses[0].signature),
            ).name()
        ]
    )

    await wallet_environments.process_pending_states(
        [
            WalletStateTransition(
                # We haven't submitted a TransactionRecord so the wallet won't know about this until confirmed
                pre_block_balance_updates={},
                post_block_balance_updates={
                    1: {
                        "confirmed_wallet_balance": -1 * coin.amount,
                        "unconfirmed_wallet_balance": -1 * coin.amount,
                        "spendable_balance": -1 * coin.amount,
                        "max_send_amount": -1 * coin.amount,
                        "unspent_coin_count": -1,
                    },
                },
            ),
        ]
    )
