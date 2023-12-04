from __future__ import annotations

from dataclasses import asdict, field
from typing import List

from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.ints import uint64
from chia.wallet.util.signer_protocol import (
    ClvmStreamable,
    KeyHints,
    PathHint,
    SigningInstructions,
    SigningResponse,
    SigningTarget,
    SumHint,
    TransportLayer,
    TransportLayerMapping,
)


class BSTLSigningTarget(ClvmStreamable):
    pubkey: bytes = field(metadata=dict(key="p"))
    message: bytes = field(metadata=dict(key="m"))
    hook: bytes32 = field(metadata=dict(key="h"))

    @staticmethod
    def from_wallet_api(_from: SigningTarget) -> BSTLSigningTarget:
        return BSTLSigningTarget(**asdict(_from))

    @staticmethod
    def to_wallet_api(_from: BSTLSigningTarget) -> SigningTarget:
        return SigningTarget(**asdict(_from))


class BSTLSumHint(ClvmStreamable):
    fingerprints: List[bytes] = field(metadata=dict(key="f"))
    synthetic_offset: bytes = field(metadata=dict(key="o"))

    @staticmethod
    def from_wallet_api(_from: SumHint) -> BSTLSumHint:
        return BSTLSumHint(**asdict(_from))

    @staticmethod
    def to_wallet_api(_from: BSTLSumHint) -> SumHint:
        return SumHint(**asdict(_from))


class BSTLPathHint(ClvmStreamable):
    root_fingerprint: bytes = field(metadata=dict(key="f"))
    path: List[uint64] = field(metadata=dict(key="p"))

    @staticmethod
    def from_wallet_api(_from: PathHint) -> BSTLPathHint:
        return BSTLPathHint(**asdict(_from))

    @staticmethod
    def to_wallet_api(_from: BSTLPathHint) -> PathHint:
        return PathHint(**asdict(_from))


class BSTLSigningInstructions(ClvmStreamable):
    sum_hints: List[BSTLSumHint] = field(metadata=dict(key="s"))
    path_hints: List[BSTLPathHint] = field(metadata=dict(key="p"))
    targets: List[BSTLSigningTarget] = field(metadata=dict(key="t"))

    @staticmethod
    def from_wallet_api(_from: SigningInstructions) -> BSTLSigningInstructions:
        return BSTLSigningInstructions(
            [BSTLSumHint(**asdict(sum_hint)) for sum_hint in _from.key_hints.sum_hints],
            [BSTLPathHint(**asdict(path_hint)) for path_hint in _from.key_hints.path_hints],
            [BSTLSigningTarget(**asdict(signing_target)) for signing_target in _from.targets],
        )

    @staticmethod
    def to_wallet_api(_from: BSTLSigningInstructions) -> SigningInstructions:
        return SigningInstructions(
            KeyHints(
                [SumHint(**asdict(sum_hint)) for sum_hint in _from.sum_hints],
                [PathHint(**asdict(path_hint)) for path_hint in _from.path_hints],
            ),
            [SigningTarget(**asdict(signing_target)) for signing_target in _from.targets],
        )


class BSTLSigningResponse(ClvmStreamable):
    signature: bytes = field(metadata=dict(key="s"))
    hook: bytes32 = field(metadata=dict(key="h"))

    @staticmethod
    def from_wallet_api(_from: SigningResponse) -> BSTLSigningResponse:
        return BSTLSigningResponse(**asdict(_from))

    @staticmethod
    def to_wallet_api(_from: BSTLSigningResponse) -> SigningResponse:
        return SigningResponse(**asdict(_from))


BLIND_SIGNER_TRANSPORT = TransportLayer(
    [
        TransportLayerMapping(
            SigningTarget, BSTLSigningTarget, BSTLSigningTarget.from_wallet_api, BSTLSigningTarget.to_wallet_api
        ),
        TransportLayerMapping(SumHint, BSTLSumHint, BSTLSumHint.from_wallet_api, BSTLSumHint.to_wallet_api),
        TransportLayerMapping(PathHint, BSTLPathHint, BSTLPathHint.from_wallet_api, BSTLPathHint.to_wallet_api),
        TransportLayerMapping(
            SigningInstructions,
            BSTLSigningInstructions,
            BSTLSigningInstructions.from_wallet_api,
            BSTLSigningInstructions.to_wallet_api,
        ),
        TransportLayerMapping(
            SigningResponse, BSTLSigningResponse, BSTLSigningResponse.from_wallet_api, BSTLSigningResponse.to_wallet_api
        ),
    ]
)
