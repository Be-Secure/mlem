import os
import tempfile
from enum import Enum
from importlib import import_module
from typing import Any, ClassVar, Dict, Optional

from transformers import (
    AutoModel,
    AutoTokenizer,
    BatchEncoding,
    PreTrainedTokenizer,
    TensorType,
)
from transformers.modeling_utils import PreTrainedModel

from mlem.core.artifacts import Artifacts
from mlem.core.data_type import (
    DataAnalyzer,
    DataHook,
    DataType,
    DictSerializer,
    DictType,
)
from mlem.core.hooks import IsInstanceHookMixin
from mlem.core.model import BufferModelIO, ModelHook, ModelType, Signature
from mlem.core.requirements import InstallableRequirement, Requirements


class ObjectType(str, Enum):
    MODEL = "model"
    TOKENIZER = "tokenizer"


_loaders = {ObjectType.MODEL: AutoModel, ObjectType.TOKENIZER: AutoTokenizer}

_bases = {
    PreTrainedModel: ObjectType.MODEL,
    PreTrainedTokenizer: ObjectType.TOKENIZER,
}


def get_object_type(obj) -> ObjectType:
    for base, obj_type in _bases.items():
        if isinstance(obj, base):
            return obj_type
    raise ValueError(f"Cannot determine object type for {obj}")


class TransformersIO(BufferModelIO):
    type: ClassVar = "transformers"

    class Config:
        use_enum_values = True

    obj_type: ObjectType

    def save_model(self, model: PreTrainedModel, path: str):
        model.save_pretrained(path)

    @property
    def load_class(self):
        return _loaders[self.obj_type]

    def load(self, artifacts: Artifacts):
        with tempfile.TemporaryDirectory() as tmpdir:
            for name, art in artifacts.items():
                art.materialize(os.path.join(tmpdir, name))
            return self.load_class.from_pretrained(tmpdir)


class TokenizerModelType(ModelType, ModelHook, IsInstanceHookMixin):
    type: ClassVar = "transformers"
    valid_types: ClassVar = (PreTrainedModel, PreTrainedTokenizer)

    class Config:
        use_enum_values = True

    return_tensors: Optional[TensorType] = None
    io: TransformersIO

    @classmethod
    def process(
        cls,
        obj: Any,
        sample_data: Optional[Any] = None,
        methods_sample_data: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> ModelType:
        call_kwargs = {}
        return_tensors = kwargs.get("return_tensors")
        if return_tensors:
            call_kwargs["return_tensors"] = return_tensors
        sample_data = (methods_sample_data or {}).get("__call__", sample_data)
        signature = Signature.from_method(
            obj.__call__,
            sample_data,
            auto_infer=sample_data is not None,
            **call_kwargs,
        )
        [a for a in signature.args if a.name == "return_tensors"][
            0
        ].default = return_tensors
        return TokenizerModelType(
            methods={"__call__": signature},
            io=TransformersIO(obj_type=get_object_type(obj)),
        )

    def get_requirements(self) -> Requirements:
        reqs = super().get_requirements()
        if self.io.obj_type == ObjectType.TOKENIZER:
            try:
                reqs += InstallableRequirement.from_module(
                    import_module("sentencepiece")
                )
                reqs += InstallableRequirement.from_module(
                    import_module("google.protobuf"), package_name="protobuf"
                )
            except ImportError:
                pass
        return reqs


_ADDITIONAL_DEPS = {
    TensorType.NUMPY: "numpy",
    TensorType.PYTORCH: "torch",
    TensorType.TENSORFLOW: "tensorflow",
}


class BatchEncodingType(DictType, DataHook, IsInstanceHookMixin):
    class Config:
        use_enum_values = True

    valid_types: ClassVar = BatchEncoding
    return_tensors: Optional[TensorType] = None

    @staticmethod
    def get_tensors_type(obj: BatchEncoding) -> Optional[TensorType]:
        types = {type(v) for v in obj.values()}
        if len(types) > 1:
            raise ValueError(f"Mixed tensor types in {obj}")
        type_ = next(iter(types))
        if type_.__module__ == "torch":
            return TensorType.PYTORCH
        if type_.__module__.startswith("tensorflow"):
            return TensorType.TENSORFLOW
        if type_.__module__.startswith("numpy"):
            return TensorType.NUMPY
        if type_ is list:
            return None
        raise ValueError(f"Unknown tensor type {type_}")

    @classmethod
    def process(cls, obj: BatchEncoding, **kwargs) -> DataType:
        return BatchEncodingType(
            return_tensors=cls.get_tensors_type(obj),
            item_types={
                k: DataAnalyzer.analyze(v, is_dynamic=True, **kwargs)
                for (k, v) in obj.items()
            },
        )

    def get_requirements(self) -> Requirements:
        new = Requirements.new("transformers")
        if self.return_tensors in _ADDITIONAL_DEPS:
            new += Requirements.new(_ADDITIONAL_DEPS[self.return_tensors])
        return new


class BatchEncodingSerializer(DictSerializer):
    data_class: ClassVar = BatchEncodingType
    is_default: ClassVar = True

    @staticmethod
    def _check_type_and_keys(data_type, obj, exc_type):
        data_type.check_type(obj, BatchEncoding, exc_type)
        if set(obj.keys()) != set(data_type.item_types.keys()):
            raise exc_type(
                f"given dict has keys: {set(obj.keys())}, expected: {set(data_type.item_types.keys())}"
            )
