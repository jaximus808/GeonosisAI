"""TDM-36 / S-05a: openpi training config for a single-arm LeRobot dataset.

This module is imported *inside* the Modal container (where openpi is installed).
It defines the data mapping the fine-tune function will generate for our YAM data, and
proves it on a public single-arm dataset (SO-100, joint-space absolute actions,
top + wrist camera). The YAM version only changes the dataset keys, `joint_dims` and
`action_dim`; see `yam_dataset_spec()` below.

Schema openpi expects after the repack transform (what our robot client must also send
to the policy server at inference time):

    observation/image        uint8 HxWx3 (or float CxHxW in [0,1]) - base/top camera
    observation/wrist_image  same - wrist camera
    observation/state        float32 [state_dim]  (joints..., gripper)
    actions                  float32 [action_horizon, action_dim]  (training only)
    prompt                   str  (from the LeRobot task string)
"""

from __future__ import annotations

import dataclasses
import pathlib

import einops
import flax.nnx as nnx
import numpy as np

import openpi.models.model as _model
import openpi.models.pi0_config as pi0_config
import openpi.training.config as _config
import openpi.training.optimizer as _optimizer
import openpi.training.weight_loaders as weight_loaders
import openpi.transforms as _transforms

PI05_BASE_PARAMS = "gs://openpi-assets/checkpoints/pi05_base/params"


@dataclasses.dataclass(frozen=True)
class DatasetSpec:
    """Everything that differs between datasets. This is what the Data Droid must emit."""

    repo_id: str
    base_image_key: str
    wrist_image_key: str
    state_key: str = "observation.state"
    action_key: str = "action"
    # Number of leading action dims that are absolute joint targets (converted to deltas).
    # The remaining dims (gripper) stay absolute.
    joint_dims: int = 6
    # Real action dim of the robot (model is padded to 32).
    action_dim: int = 7
    fps: int = 30


SO100_PICKPLACE = DatasetSpec(
    repo_id="lerobot/svla_so100_pickplace",
    base_image_key="observation.images.top",
    wrist_image_key="observation.images.wrist",
    joint_dims=5,
    action_dim=6,
    fps=30,
)


def yam_dataset_spec(repo_id: str) -> DatasetSpec:
    """What we expect our own YAM LeRobot export to look like (6 joints + gripper)."""
    return DatasetSpec(
        repo_id=repo_id,
        base_image_key="observation.images.top",
        wrist_image_key="observation.images.wrist",
        joint_dims=6,
        action_dim=7,
        fps=30,
    )


def _parse_image(image) -> np.ndarray:
    image = np.asarray(image)
    if np.issubdtype(image.dtype, np.floating):
        image = (255 * image).astype(np.uint8)
    if image.shape[0] == 3:
        image = einops.rearrange(image, "c h w -> h w c")
    return image


@dataclasses.dataclass(frozen=True)
class SingleArmInputs(_transforms.DataTransformFn):
    """Map one base + one wrist camera and joint state into pi0.5's three image slots."""

    model_type: _model.ModelType

    def __call__(self, data: dict) -> dict:
        base_image = _parse_image(data["observation/image"])
        wrist_image = _parse_image(data["observation/wrist_image"])
        inputs = {
            "state": np.asarray(data["observation/state"], dtype=np.float32),
            "image": {
                "base_0_rgb": base_image,
                "left_wrist_0_rgb": wrist_image,
                "right_wrist_0_rgb": np.zeros_like(base_image),
            },
            "image_mask": {
                "base_0_rgb": np.True_,
                "left_wrist_0_rgb": np.True_,
                "right_wrist_0_rgb": np.False_,
            },
        }
        if "actions" in data:
            inputs["actions"] = np.asarray(data["actions"], dtype=np.float32)
        if "prompt" in data:
            inputs["prompt"] = data["prompt"]
        return inputs


@dataclasses.dataclass(frozen=True)
class SingleArmOutputs(_transforms.DataTransformFn):
    action_dim: int

    def __call__(self, data: dict) -> dict:
        return {"actions": np.asarray(data["actions"][:, : self.action_dim])}


@dataclasses.dataclass(frozen=True)
class LeRobotSingleArmDataConfig(_config.DataConfigFactory):
    spec: DatasetSpec = SO100_PICKPLACE

    def create(self, assets_dirs: pathlib.Path, model_config: _model.BaseModelConfig) -> _config.DataConfig:
        s = self.spec
        repack = _transforms.Group(
            inputs=[
                _transforms.RepackTransform(
                    {
                        "observation/image": s.base_image_key,
                        "observation/wrist_image": s.wrist_image_key,
                        "observation/state": s.state_key,
                        "actions": s.action_key,
                        "prompt": "prompt",
                    }
                )
            ]
        )
        data_transforms = _transforms.Group(
            inputs=[SingleArmInputs(model_type=model_config.model_type)],
            outputs=[SingleArmOutputs(action_dim=s.action_dim)],
        )
        # Dataset actions are absolute joint targets; pi0.5 is trained on joint deltas
        # relative to the current state (gripper stays absolute).
        mask = _transforms.make_bool_mask(s.joint_dims, -(s.action_dim - s.joint_dims))
        data_transforms = data_transforms.push(
            inputs=[_transforms.DeltaActions(mask)],
            outputs=[_transforms.AbsoluteActions(mask)],
        )
        model_transforms = _config.ModelTransformFactory()(model_config)
        return dataclasses.replace(
            self.create_base_config(assets_dirs, model_config),
            repack_transforms=repack,
            data_transforms=data_transforms,
            model_transforms=model_transforms,
            action_sequence_keys=(s.action_key,),
        )


def make_train_config(
    *,
    mode: str,
    spec: DatasetSpec = SO100_PICKPLACE,
    num_train_steps: int = 200,
    batch_size: int = 32,
    action_horizon: int = 50,
    assets_dir: str = "/vol/assets",
    checkpoint_base_dir: str = "/vol/checkpoints",
    exp_name: str = "smoke",
    num_workers: int = 8,
    log_interval: int = 10,
    save_interval: int | None = None,
    peak_lr: float | None = None,
) -> _config.TrainConfig:
    """mode: 'lora' (gemma_2b_lora + gemma_300m_lora, frozen base) or 'full'."""
    if mode == "lora":
        model = pi0_config.Pi0Config(
            pi05=True,
            action_horizon=action_horizon,
            paligemma_variant="gemma_2b_lora",
            action_expert_variant="gemma_300m_lora",
        )
        freeze_filter = model.get_freeze_filter()
        ema_decay = None
        lr = peak_lr or 2.5e-5
    elif mode == "full":
        model = pi0_config.Pi0Config(pi05=True, action_horizon=action_horizon)
        freeze_filter = nnx.Nothing
        ema_decay = 0.99
        lr = peak_lr or 2.5e-5
    else:
        raise ValueError(f"unknown mode {mode!r}")

    return _config.TrainConfig(
        name=f"rdf_s05a_pi05_{mode}",
        exp_name=exp_name,
        model=model,
        data=LeRobotSingleArmDataConfig(
            repo_id=spec.repo_id,
            spec=spec,
            assets=_config.AssetsConfig(assets_dir=assets_dir, asset_id=spec.repo_id),
            base_config=_config.DataConfig(prompt_from_task=True),
        ),
        weight_loader=weight_loaders.CheckpointWeightLoader(PI05_BASE_PARAMS),
        freeze_filter=freeze_filter,
        ema_decay=ema_decay,
        lr_schedule=_optimizer.CosineDecaySchedule(
            warmup_steps=min(1000, max(10, num_train_steps // 10)),
            peak_lr=lr,
            decay_steps=max(num_train_steps, 1000),
            decay_lr=lr / 10,
        ),
        optimizer=_optimizer.AdamW(clip_gradient_norm=1.0),
        batch_size=batch_size,
        num_workers=num_workers,
        num_train_steps=num_train_steps,
        log_interval=log_interval,
        save_interval=save_interval or num_train_steps,
        keep_period=None,
        overwrite=True,
        wandb_enabled=False,
        checkpoint_base_dir=checkpoint_base_dir,
        assets_base_dir=assets_dir,
    )
