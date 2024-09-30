import os
import torch
import torch.nn as nn

from transformers import Trainer
from typing import Optional


def unwrap_model(model: nn.Module) -> nn.Module:
    """
    Recursively unwraps a model from potential containers (as used in distributed training).

    Args:
        model (`torch.nn.Module`): The model to unwrap.
    """
    # since there could be multiple levels of wrapping, unwrap recursively
    if hasattr(model, "module"):
        return unwrap_model(model.module)
    else:
        return model


class SensorLLMTrainer(Trainer):

    def _save(self, output_dir: Optional[str] = None, state_dict=None):
        # Save the model
        _state_dict = state_dict
        if _state_dict is None:
            # Only save the model itself if we are using distributed training
            model_to_save = unwrap_model(self.model)
            _state_dict = model_to_save.state_dict()

        keys_to_match = ['pt_encoder_backbone']
        filtered_state_dict = {k: v for k, v in _state_dict.items() if
                               not any(key_match in k for key_match in keys_to_match)}

        super(SensorLLMTrainer, self)._save(output_dir, filtered_state_dict)

    def save_model(self, output_dir: Optional[str] = None, _internal_call: bool = False):
        if self.fsdp is not None:
            if output_dir is None:
                output_dir = self.args.output_dir
            from torch.distributed.fsdp import (
                FullyShardedDataParallel as FSDP,
                FullStateDictConfig,
                StateDictType,
            )
            save_policy = FullStateDictConfig(offload_to_cpu=True, rank0_only=True)
            with FSDP.state_dict_type(self.model, StateDictType.FULL_STATE_DICT, save_policy):
                cpu_state_dict = self.model.state_dict()
            if self.args.should_save:
                self._save(output_dir, state_dict=cpu_state_dict)  # noqa
            # Push to the Hub when `save_model` is called by the user.
            if self.args.push_to_hub and not _internal_call:
                self.push_to_hub(commit_message="Model save")
        else:
            super().save_model(output_dir, _internal_call)


class SensorLLMWeightedCELossTrainer(SensorLLMTrainer):
    def __init__(self, *args, class_weights=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Ensure label_weights is a tensor
        assert class_weights is not None, "class_weights for SensorLLMWeightedCELossTrainer is None"
        print(f"class_weights: {class_weights}")
        self.class_weights = class_weights

    def compute_loss(self, model, inputs, return_outputs=False):
        # Extract labels and convert them to long type for cross_entropy
        labels = inputs.pop("labels")

        # Forward pass
        outputs = model(**inputs)

        # Extract logits assuming they are directly outputted by the model
        logits = outputs.get('logits')

        # Compute custom loss with class weights for imbalanced data handling
        assert self.class_weights is not None, "self.class_weights is None"
        loss_fct = torch.nn.CrossEntropyLoss(
            weight=torch.tensor(self.class_weights, device=model.device, dtype=logits.dtype))

        loss = loss_fct(logits.view(-1, self.model.config.num_labels), labels.view(-1))

        return (loss, outputs) if return_outputs else loss



