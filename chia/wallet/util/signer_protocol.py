from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, fields
from io import BytesIO
from typing import Any, BinaryIO, Callable, Dict, Generic, Iterator, List, Optional, Type, TypeVar, Union

from hsms.clvm_serde import from_program_for_type, to_program_for_type
from typing_extensions import dataclass_transform

from chia.types.blockchain_format.coin import Coin as _Coin
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.serialized_program import SerializedProgram
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_spend import CoinSpend
from chia.util.byte_types import hexstr_to_bytes
from chia.util.ints import uint64
from chia.util.streamable import ConversionError, Streamable, streamable

USE_CLVM_SERIALIZATION = False
TRANSPORT_LAYER = None


@contextmanager
def clvm_serialization_mode(use: bool, transport_layer: Optional[TransportLayer] = None) -> Iterator[None]:
    global USE_CLVM_SERIALIZATION
    global TRANSPORT_LAYER
    old_mode = USE_CLVM_SERIALIZATION
    old_tl = TRANSPORT_LAYER
    USE_CLVM_SERIALIZATION = use
    TRANSPORT_LAYER = transport_layer
    yield
    USE_CLVM_SERIALIZATION = old_mode
    TRANSPORT_LAYER = old_tl


@dataclass_transform()
class ClvmStreamableMeta(type):
    def __init__(cls: ClvmStreamableMeta, *args: Any) -> None:
        if cls.__name__ == "ClvmStreamable":
            return
        # Not sure how to fix the hints here, but it works
        dcls: Type[ClvmStreamable] = streamable(dataclass(frozen=True)(cls))  # type: ignore[arg-type]
        # Iterate over the fields of the class
        for field_obj in fields(dcls):
            field_name = field_obj.name
            field_metadata = {"key": field_name}
            field_metadata.update(field_obj.metadata)
            setattr(field_obj, "metadata", field_metadata)
        setattr(dcls, "as_program", to_program_for_type(dcls))
        setattr(dcls, "from_program", lambda prog: from_program_for_type(dcls)(prog))
        super().__init__(*args)


_T_ClvmStreamable = TypeVar("_T_ClvmStreamable", bound="ClvmStreamable")
_T_TLClvmStreamable = TypeVar("_T_TLClvmStreamable", bound="ClvmStreamable")


class ClvmStreamable(Streamable, metaclass=ClvmStreamableMeta):
    def as_program(self) -> Program:
        raise NotImplementedError()  # pragma: no cover

    @classmethod
    def from_program(cls: Type[_T_ClvmStreamable], prog: Program) -> _T_ClvmStreamable:
        raise NotImplementedError()  # pragma: no cover

    def stream(self, f: BinaryIO) -> None:
        global USE_CLVM_SERIALIZATION
        global TRANSPORT_LAYER
        if TRANSPORT_LAYER is not None:
            new_self = TRANSPORT_LAYER.serialize_for_transport(self)
        else:
            new_self = self

        if USE_CLVM_SERIALIZATION:
            f.write(bytes(new_self.as_program()))
        else:
            super().stream(f)

    @classmethod
    def parse(cls: Type[_T_ClvmStreamable], f: BinaryIO) -> _T_ClvmStreamable:
        assert isinstance(f, BytesIO)
        global TRANSPORT_LAYER
        if TRANSPORT_LAYER is not None:
            cls_mapping: Optional[
                TransportLayerMapping[_T_ClvmStreamable, ClvmStreamable]
            ] = TRANSPORT_LAYER.get_mapping(cls)
            if cls_mapping is not None:
                new_cls: Type[Union[_T_ClvmStreamable, ClvmStreamable]] = cls_mapping.to_type
            else:
                new_cls = cls
        else:
            new_cls = cls

        try:
            result = new_cls.from_program(Program.from_bytes(bytes(f.getbuffer())))
            f.read()
            if TRANSPORT_LAYER is not None and cls_mapping is not None:
                deserialized_result: _T_ClvmStreamable = cls_mapping.deserialize_function(result)
                return deserialized_result
            else:
                assert isinstance(result, cls)
                return result
        except Exception:
            return super().parse(f)

    def override_json_serialization(self, default_recurse_jsonify: Callable[[Any], Dict[str, Any]]) -> Any:
        global USE_CLVM_SERIALIZATION
        global TRANSPORT_LAYER
        if TRANSPORT_LAYER is not None:
            new_self = TRANSPORT_LAYER.serialize_for_transport(self)
        else:
            new_self = self

        if USE_CLVM_SERIALIZATION:
            return bytes(new_self).hex()
        else:
            new_dict = {}
            for field in fields(new_self):
                new_dict[field.name] = default_recurse_jsonify(getattr(new_self, field.name))
            return new_dict

    @classmethod
    def from_json_dict(cls: Type[_T_ClvmStreamable], json_dict: Any) -> _T_ClvmStreamable:
        global TRANSPORT_LAYER
        if TRANSPORT_LAYER is not None:
            cls_mapping: Optional[
                TransportLayerMapping[_T_ClvmStreamable, ClvmStreamable]
            ] = TRANSPORT_LAYER.get_mapping(cls)
            if cls_mapping is not None:
                new_cls: Type[Union[_T_ClvmStreamable, ClvmStreamable]] = cls_mapping.to_type
            else:
                new_cls = cls
        else:
            new_cls = cls

        if isinstance(json_dict, str):
            try:
                byts = hexstr_to_bytes(json_dict)
            except ValueError as e:
                raise ConversionError(json_dict, new_cls, e)

            try:
                result = new_cls.from_program(Program.from_bytes(byts))
                if TRANSPORT_LAYER is not None and cls_mapping is not None:
                    deserialized_result: _T_ClvmStreamable = cls_mapping.deserialize_function(result)
                    return deserialized_result
                else:
                    assert isinstance(result, cls)
                    return result
            except Exception as e:
                raise ConversionError(json_dict, new_cls, e)
        else:
            return super().from_json_dict(json_dict)


@dataclass(frozen=True)
class TransportLayerMapping(Generic[_T_ClvmStreamable, _T_TLClvmStreamable]):
    from_type: Type[_T_ClvmStreamable]
    to_type: Type[_T_TLClvmStreamable]
    serialize_function: Callable[[_T_ClvmStreamable], _T_TLClvmStreamable]
    deserialize_function: Callable[[_T_TLClvmStreamable], _T_ClvmStreamable]


@dataclass(frozen=True)
class TransportLayer:
    type_mappings: List[TransportLayerMapping[Any, Any]]

    def get_mapping(
        self, _type: Type[_T_ClvmStreamable]
    ) -> Optional[TransportLayerMapping[_T_ClvmStreamable, ClvmStreamable]]:
        mappings: List[TransportLayerMapping[_T_ClvmStreamable, ClvmStreamable]] = [
            m for m in self.type_mappings if m.from_type == _type
        ]
        if len(mappings) == 1:
            return mappings[0]
        elif len(mappings) == 0:
            return None
        else:
            raise RuntimeError("Malformed TransportLayer")

    def serialize_for_transport(self, instance: _T_ClvmStreamable) -> ClvmStreamable:
        mappings: List[TransportLayerMapping[_T_ClvmStreamable, ClvmStreamable]] = [
            m for m in self.type_mappings if m.from_type == instance.__class__
        ]
        if len(mappings) == 1:
            return mappings[0].serialize_function(instance)
        elif len(mappings) == 0:
            return instance
        else:
            raise RuntimeError("Malformed TransportLayer")

    def deserialize_from_transport(self, instance: _T_ClvmStreamable) -> ClvmStreamable:
        mappings: List[TransportLayerMapping[ClvmStreamable, _T_ClvmStreamable]] = [
            m for m in self.type_mappings if m.to_type == instance.__class__
        ]
        if len(mappings) == 1:
            return mappings[0].deserialize_function(instance)
        elif len(mappings) == 0:
            return instance
        else:
            raise RuntimeError("Malformed TransportLayer")


class Coin(ClvmStreamable):
    parent_coin_id: bytes32
    puzzle_hash: bytes32
    amount: uint64


class Spend(ClvmStreamable):
    coin: Coin
    puzzle: Program
    solution: Program

    @classmethod
    def from_coin_spend(cls, coin_spend: CoinSpend) -> Spend:
        return cls(
            Coin(
                coin_spend.coin.parent_coin_info,
                coin_spend.coin.puzzle_hash,
                uint64(coin_spend.coin.amount),
            ),
            coin_spend.puzzle_reveal.to_program(),
            coin_spend.solution.to_program(),
        )

    def as_coin_spend(self) -> CoinSpend:
        return CoinSpend(
            _Coin(
                self.coin.parent_coin_id,
                self.coin.puzzle_hash,
                self.coin.amount,
            ),
            SerializedProgram.from_program(self.puzzle),
            SerializedProgram.from_program(self.solution),
        )


class TransactionInfo(ClvmStreamable):
    spends: List[Spend]


class SigningTarget(ClvmStreamable):
    pubkey: bytes
    message: bytes
    hook: bytes32


class SumHint(ClvmStreamable):
    fingerprints: List[bytes]
    synthetic_offset: bytes


class PathHint(ClvmStreamable):
    root_fingerprint: bytes
    path: List[uint64]


class KeyHints(ClvmStreamable):
    sum_hints: List[SumHint]
    path_hints: List[PathHint]


class SigningInstructions(ClvmStreamable):
    key_hints: KeyHints
    targets: List[SigningTarget]


class UnsignedTransaction(ClvmStreamable):
    transaction_info: TransactionInfo
    signing_instructions: SigningInstructions


class SigningResponse(ClvmStreamable):
    signature: bytes
    hook: bytes32


class Signature(ClvmStreamable):
    type: str
    signature: bytes


class SignedTransaction(ClvmStreamable):
    transaction_info: TransactionInfo
    signatures: List[Signature]
