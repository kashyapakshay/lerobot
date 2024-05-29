#!/usr/bin/env python

# Copyright 2024 Robotic AI, Learning Lab Berkeley,
# and The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Octo Policy as per "Octo: An Open-Source Generalist Robot Policy"

TODO:
  - Remove reliance on diffusers for DDPMScheduler and LR scheduler.
  - Make compatible with multiple image keys.
  - Add support multiple proprioceptive observations.
  - Add support for language and goal conditioning.
"""

from collections import deque
from typing import Callable

import torch
import torch.nn.functional as F  # noqa: N812
import torchvision
from diffusers.schedulers.scheduling_ddim import DDIMScheduler
from diffusers.schedulers.scheduling_ddpm import DDPMScheduler
from einops import rearrange, repeat
from huggingface_hub import PyTorchModelHubMixin
from torch import Tensor, nn
from torch.nn import TransformerEncoder, TransformerEncoderLayer

from lerobot.common.policies.normalize import Normalize, Unnormalize
from lerobot.common.policies.octo.configuration_octo import OctoConfig
from lerobot.common.policies.utils import (
    get_device_from_parameters,
    get_dtype_from_parameters,
    populate_queues,
)


class OctoPolicy(nn.Module, PyTorchModelHubMixin):
    """
    Octo Policy as per "Octo: An Open-Source Generalist Robot Policy"
    (paper: https://arxiv.org/pdf/2405.12213, code: https://github.com/octo-models/octo/).
    """

    name = "octo"

    def __init__(
        self,
        config: OctoConfig | None = None,
        dataset_stats: dict[str, dict[str, Tensor]] | None = None,
    ):
        """
        Args:
            config: Policy configuration class instance or None, in which case the default instantiation of
                the configuration class is used.
            dataset_stats: Dataset statistics to be used for normalization. If not passed here, it is expected
                that they will be passed with a call to `load_state_dict` before the policy is used.
        """
        super().__init__()
        if config is None:
            config = OctoConfig()
        self.config = config
        self.normalize_inputs = Normalize(
            config.input_shapes, config.input_normalization_modes, dataset_stats
        )
        self.normalize_targets = Normalize(
            config.output_shapes, config.output_normalization_modes, dataset_stats
        )
        self.unnormalize_outputs = Unnormalize(
            config.output_shapes, config.output_normalization_modes, dataset_stats
        )

        # queues are populated during rollout of the policy, they contain the n latest observations and actions
        self._queues = None

        self.model = OctoModel(config)

        image_keys = [k for k in config.input_shapes if k.startswith("observation.image")]
        # Note: This check is covered in the post-init of the config but have a sanity check just in case.
        if len(image_keys) != 1:
            raise NotImplementedError(
                f"{self.__class__.__name__} only handles one image for now. Got image keys {image_keys}."
            )
        self.input_image_key = image_keys[0]

        self.reset()

    def reset(self):
        """Clear observation and action queues. Should be called on `env.reset()`"""
        self._queues = {
            "observation.image": deque(maxlen=self.config.n_obs_steps),
            "observation.state": deque(maxlen=self.config.n_obs_steps),
            "action": deque(maxlen=self.config.n_action_steps),
        }

    @torch.no_grad
    def select_action(self, batch: dict[str, Tensor]) -> Tensor:
        """Select a single action given environment observations.

        This method handles caching a history of observations and an action trajectory generated by the
        underlying diffusion model. Here's how it works:
          - `n_obs_steps` steps worth of observations are cached (for the first steps, the observation is
            copied `n_obs_steps` times to fill the cache).
          - The diffusion model generates `horizon` steps worth of actions.
          - `n_action_steps` worth of actions are actually kept for execution, starting from the current step.
        Schematically this looks like:
            ----------------------------------------------------------------------------------------------
            (legend: o = n_obs_steps, h = horizon, a = n_action_steps)
            |timestep            | n-o+1 | n-o+2 | ..... | n     | ..... | n+a-1 | n+a   | ..... |n-o+1+h|
            |observation is used | YES   | YES   | YES   | NO    | NO    | NO    | NO    | NO    | NO    |
            |action is generated | YES   | YES   | YES   | YES   | YES   | YES   | YES   | YES   | YES   |
            |action is used      | NO    | NO    | NO    | YES   | YES   | YES   | NO    | NO    | NO    |
            ----------------------------------------------------------------------------------------------
        Note that this means we require: `n_action_steps < horizon - n_obs_steps + 1`. Also, note that
        "horizon" may not the best name to describe what the variable actually means, because this period is
        actually measured from the first observation which (if `n_obs_steps` > 1) happened in the past.
        """
        batch = self.normalize_inputs(batch)
        batch["observation.image"] = batch[self.input_image_key]

        self._queues = populate_queues(self._queues, batch)

        if len(self._queues["action"]) == 0:
            # stack n latest observations from the queue
            batch = {k: torch.stack(list(self._queues[k]), dim=1) for k in batch if k in self._queues}
            actions = self.model.generate_actions(batch)

            # TODO(rcadene): make above methods return output dictionary?
            actions = self.unnormalize_outputs({"action": actions})["action"]

            self._queues["action"].extend(actions.transpose(0, 1))

        action = self._queues["action"].popleft()
        return action

    def forward(self, batch: dict[str, Tensor]) -> dict[str, Tensor]:
        """Run the batch through the model and compute the loss for training or validation."""
        batch = self.normalize_inputs(batch)
        batch["observation.image"] = batch[self.input_image_key]
        batch = self.normalize_targets(batch)
        loss = self.model.compute_loss(batch)
        return {"loss": loss}


def _make_noise_scheduler(name: str, **kwargs: dict) -> DDPMScheduler | DDIMScheduler:
    """
    Factory for noise scheduler instances of the requested type. All kwargs are passed
    to the scheduler.
    """
    if name == "DDPM":
        return DDPMScheduler(**kwargs)
    elif name == "DDIM":
        return DDIMScheduler(**kwargs)
    else:
        raise ValueError(f"Unsupported noise scheduler type {name}")


class OctoModel(nn.Module):
    def __init__(self, config: OctoConfig):
        """An overview of this minimal Octo model implementation:

        There are two main components:
        1) OctoTransformer, which processes an input sequence of state and image tokens (collectively called
            "observation tokens") and readout tokens as follows:
            - Normalized state inputs of shape (`n_obs_steps`, `state_dim`) are projected to (`n_obs_steps`, `embed_dim`).
            - Feature maps of the images generated by a vision encoder are flattened along the spatial dimension and
                projected to (`n_obs_steps`, `n_img_features`, `embed_dim`).
            - The above are concatenated to form a sequence of observation tokens of shape (`n_obs_steps`, `n_obs_tokens_per_step`, `embed_dim`).
            - Additionally, learned "readout" tokens of shape (`n_obs_steps`, `n_readouts_per_step`, `embed_dim`) appended
                after tokens of each observation step to form the final input sequence for the transformer encoder.
            A causal mask (see make_causal_mask for an example viz) is used to prevent:
                a) observation and readout tokens from attending to any future tokens,
                b) observation tokens from attending to any readout tokens, and
                c) readout tokens from attending to prior readout tokens.
        2) OctoDiffusionActionHead, which predicts the noise to remove from a noisy trajectory, conditioned on the mean
            of all readout embeddings and a projection of the denoising iteration K.

        An example, with `n_obs_steps`=2, `n_readouts_per_step`=1, a 7-DoF state (joint angles, gripper, etc.)
            and a 96x96 wrist image (which would result in 256x6x6 feature maps using resnet18):

        `n_obs_tokens_per_step` = 6*6 + 1 + 1 = 38
        ---------------------------------------------------------------------------
        Token Index | 0 | 1 | 2 | 3 | 4 | ... | 36 | 37 | 38 | 39 | ... | 74 | 75 |
        ---------------------------------------------------------------------------
        Obs Timestep| 0 | 0 | 0 | 0 | 0 | ... |  1 |  1 |  1 |  1 | ... |  1 |  1 |
        ---------------------------------------------------------------------------
        Token Type  |obs|obs|obs|obs|obs| ... |obs |rout|obs |obs | ... |obs |rout|
        ------------|---|---|---|---|---|-----|----|----|----|----|-----|----|----|
                                                      |                        |
                                                      V                        V
                                                  <r_embed_1>              <r_embed_2>
                                                      |                        |
                                                      --------> (Mean) <--------
                                                                  |
                                                                  V
                            <noisy_sample>, <K_proj> --> (Action Diffusion Head) --> <noise_pred>

        Note that this implementation does not (yet) include certain features from the original Octo implementation:
        1) Language and Goal Conditioning: The original Octo supports conditioning on language and goal images, which
            would be tokenized and prepended to the input sequence.
        1) Multiple trajectory generation: The original Octo generates a trajectory for each readout token (i.e,
            trajectory starting at each observation step). This implementation only generates a single trajectory using
            the mean of all readout tokens.
        2) MAP over multiple readout tokens: The original Octo has an option to use Multihead Attention Pooling over
            multiple readout tokens for each observation step. This supports multiple readout tokens but utilizes a
            simple mean pooling over them.
        """
        super().__init__()
        self.config = config

        self.rgb_encoder = OctoRgbEncoder(config)
        feat_map_shape = self.rgb_encoder.feature_map_shape
        # we are assuming there is a single proprioceptive observation per step for now.
        # TODO: generalize to multiple proprioceptive observations in the future.
        n_state_obs = 1
        n_img_features = feat_map_shape[1] * feat_map_shape[2]
        n_obs_tokens_per_step = n_img_features + n_state_obs
        self.transformer = OctoTransformer(
            config,
            img_dim=feat_map_shape[0],
            n_obs_tokens_per_step=n_obs_tokens_per_step,
        )
        self.action_head = OctoDiffusionActionHead(config)

        self.noise_scheduler = _make_noise_scheduler(
            config.noise_scheduler_type,
            num_train_timesteps=config.num_train_timesteps,
            beta_start=config.beta_start,
            beta_end=config.beta_end,
            beta_schedule=config.beta_schedule,
            clip_sample=config.clip_sample,
            clip_sample_range=config.clip_sample_range,
            prediction_type=config.prediction_type,
        )

        if config.num_inference_steps is None:
            self.num_inference_steps = self.noise_scheduler.config.num_train_timesteps
        else:
            self.num_inference_steps = config.num_inference_steps

    def conditional_sample(
        self, batch_size: int, readout_embeds: Tensor, generator: torch.Generator | None = None
    ) -> Tensor:
        device = get_device_from_parameters(self)
        dtype = get_dtype_from_parameters(self)

        # Sample prior.
        sample = torch.randn(
            size=(batch_size, self.config.horizon, self.config.output_shapes["action"][0]),
            dtype=dtype,
            device=device,
            generator=generator,
        )
        sample = rearrange(sample, "b t d -> b (t d)")

        self.noise_scheduler.set_timesteps(self.num_inference_steps)

        for t in self.noise_scheduler.timesteps:
            # Predict model output.
            t_ = t.repeat((batch_size, 1)).to(device)
            model_output = self.action_head(readout_embeds, t_, sample)

            # Compute previous image: x_t -> x_t-1
            sample = self.noise_scheduler.step(model_output, t, sample, generator=generator).prev_sample

        sample = rearrange(sample, "b (t d) -> b t d", t=self.config.horizon)
        return sample

    def generate_actions(self, batch: dict[str, Tensor]) -> Tensor:
        """
        This function expects `batch` to have:
        {
            "observation.state": (B, n_obs_steps, state_dim)
            "observation.image": (B, n_obs_steps, C, H, W)
        }
        """
        batch_size, n_obs_steps = batch["observation.state"].shape[:2]
        assert n_obs_steps == self.config.n_obs_steps

        # Extract image feature (first combine batch and sequence dims).
        img_features = self.rgb_encoder(rearrange(batch["observation.image"], "b n ... -> (b n) ..."))
        # Separate batch and obs_step dims, flatten into a sequence of patch tokens for each step.
        img_features = rearrange(img_features, "(b n) c h w -> b n (h w) c", b=batch_size)

        readout_embeds = self.transformer(batch["observation.state"], img_features)

        # run sampling
        sample = self.conditional_sample(batch_size, readout_embeds)

        # `horizon` steps worth of actions (from the first observation).
        actions = sample[..., : self.config.output_shapes["action"][0]]
        # Extract `n_action_steps` steps worth of actions (from the current observation).
        start = n_obs_steps - 1
        end = start + self.config.n_action_steps
        actions = actions[:, start:end]

        return actions

    def compute_loss(self, batch: dict[str, Tensor]) -> Tensor:
        """
        This function expects `batch` to have (at least):
        {
            "observation.state": (B, n_obs_steps, state_dim)
            "observation.image": (B, n_obs_steps, C, H, W)
            "action": (B, horizon, action_dim)
            "action_is_pad": (B, horizon)
        }
        """
        # Input validation.
        assert set(batch).issuperset({"observation.state", "observation.image", "action", "action_is_pad"})
        batch_size, n_obs_steps = batch["observation.state"].shape[:2]
        horizon = batch["action"].shape[1]
        assert horizon == self.config.horizon
        assert n_obs_steps == self.config.n_obs_steps

        # Extract image feature (first combine batch and obs_step dims).
        img_features = self.rgb_encoder(rearrange(batch["observation.image"], "b n ... -> (b n) ..."))
        # Separate batch and obs_step dims, flatten into a sequence of patch tokens for each step.
        img_features = rearrange(img_features, "(b n) c h w -> b n (h w) c", b=batch_size)

        readout_embeds = self.transformer(batch["observation.state"], img_features)

        trajectory = batch["action"]

        # Forward diffusion.
        # Sample noise to add to the trajectory.
        eps = torch.randn(trajectory.shape, device=trajectory.device)
        # Sample a random noising timestep for each item in the batch.
        timesteps = torch.randint(
            low=0,
            high=self.noise_scheduler.config.num_train_timesteps,
            size=(trajectory.shape[0], 1),
            device=trajectory.device,
        ).long()
        # Add noise to the clean trajectories according to the noise magnitude at each timestep.
        noisy_trajectory = self.noise_scheduler.add_noise(trajectory, eps, timesteps)

        # Run the denoising network (that might denoise the trajectory, or attempt to predict the noise).
        pred = self.action_head(readout_embeds, timesteps, rearrange(noisy_trajectory, "b t d -> b (t d)"))
        pred = rearrange(pred, "b (t d) -> b t d", t=horizon)

        # Compute the loss.
        # The target is either the original trajectory, or the noise.
        if self.config.prediction_type == "epsilon":
            target = eps
        elif self.config.prediction_type == "sample":
            target = batch["action"]
        else:
            raise ValueError(f"Unsupported prediction type {self.config.prediction_type}")

        loss = F.mse_loss(pred, target, reduction="none")

        # Mask loss wherever the action is padded with copies (edges of the dataset trajectory).
        if self.config.do_mask_loss_for_padding and "action_is_pad" in batch:
            in_episode_bound = ~batch["action_is_pad"]
            loss = loss * in_episode_bound.unsqueeze(-1)

        return loss.mean()


def _replace_submodules(
    root_module: nn.Module, predicate: Callable[[nn.Module], bool], func: Callable[[nn.Module], nn.Module]
) -> nn.Module:
    """
    Args:
        root_module: The module for which the submodules need to be replaced
        predicate: Takes a module as an argument and must return True if the that module is to be replaced.
        func: Takes a module as an argument and returns a new module to replace it with.
    Returns:
        The root module with its submodules replaced.
    """
    if predicate(root_module):
        return func(root_module)

    replace_list = [k.split(".") for k, m in root_module.named_modules(remove_duplicate=True) if predicate(m)]
    for *parents, k in replace_list:
        parent_module = root_module
        if len(parents) > 0:
            parent_module = root_module.get_submodule(".".join(parents))
        if isinstance(parent_module, nn.Sequential):
            src_module = parent_module[int(k)]
        else:
            src_module = getattr(parent_module, k)
        tgt_module = func(src_module)
        if isinstance(parent_module, nn.Sequential):
            parent_module[int(k)] = tgt_module
        else:
            setattr(parent_module, k, tgt_module)
    # verify that all BN are replaced
    assert not any(predicate(m) for _, m in root_module.named_modules(remove_duplicate=True))
    return root_module


class OctoRgbEncoder(nn.Module):
    """Encoder an RGB image into a 1D feature vector.
    (Copied from Diffusion Policy code.)

    Includes the ability to normalize and crop the image first.
    """

    def __init__(self, config: OctoConfig):
        super().__init__()
        # Set up optional preprocessing.
        if config.crop_shape is not None:
            self.do_crop = True
            # Always use center crop for eval
            self.center_crop = torchvision.transforms.CenterCrop(config.crop_shape)
            if config.crop_is_random:
                self.maybe_random_crop = torchvision.transforms.RandomCrop(config.crop_shape)
            else:
                self.maybe_random_crop = self.center_crop
        else:
            self.do_crop = False

        # Set up backbone.
        backbone_model = getattr(torchvision.models, config.vision_backbone)(
            weights=config.pretrained_backbone_weights
        )
        # Note: This assumes that the layer4 feature map is children()[-3]
        # TODO(alexander-soare): Use a safer alternative.
        self.backbone = nn.Sequential(*(list(backbone_model.children())[:-2]))
        if config.use_group_norm:
            if config.pretrained_backbone_weights:
                raise ValueError(
                    "You can't replace BatchNorm in a pretrained model without ruining the weights!"
                )
            self.backbone = _replace_submodules(
                root_module=self.backbone,
                predicate=lambda x: isinstance(x, nn.BatchNorm2d),
                func=lambda x: nn.GroupNorm(num_groups=x.num_features // 16, num_channels=x.num_features),
            )

        # Use a dry run to get the feature map shape.
        # The dummy input should take the number of image channels from `config.input_shapes` and it should
        # use the height and width from `config.crop_shape`.
        image_keys = [k for k in config.input_shapes if k.startswith("observation.image")]
        assert len(image_keys) == 1
        image_key = image_keys[0]
        dummy_input = torch.zeros(size=(1, config.input_shapes[image_key][0], *config.crop_shape))
        with torch.inference_mode():
            dummy_feature_map = self.backbone(dummy_input)
        self.feature_map_shape = tuple(dummy_feature_map.shape[1:])

    def forward(self, x: Tensor) -> Tensor:
        """
        Args:
            x: (B, C, H, W) image tensor with pixel values in [0, 1].
        Returns:
            (B, D) image feature.
        """
        # Preprocess: maybe crop (if it was set up in the __init__).
        if self.do_crop:
            if self.training:  # noqa: SIM108
                x = self.maybe_random_crop(x)
            else:
                # Always use center crop for eval.
                x = self.center_crop(x)
        # Extract backbone feature.
        return self.backbone(x)


def make_causal_mask(n_obs_tokens_per_step, n_obs_steps, n_readouts_per_step):
    """Make the block-wise causal mask for the OctoTransformer.

    Example:
    --------------------------------------
    Obs Timestep | 0 | 0 | 0 | 1 | 1 | 1 |
    --------------------------------------
    Token index  | 0 | 1 | 2 | 3 | 4 | 5 |
    -------------|---|---|---|---|---|---|
    0 attends to | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ |
    1 attends to | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ |
    2 attends to | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ | < readout
    3 attends to | ✓ | ✓ | ✗ | ✓ | ✗ | ✗ |
    4 attends to | ✓ | ✓ | ✗ | ✓ | ✓ | ✗ |
    5 attends to | ✓ | ✓ | ✗ | ✓ | ✓ | ✓ | < readout
    --------------------------------------
                           ^           ^
                           readout     readout

    Args:
        n_obs_tokens_per_step: int, number of observation tokens in each observation step.
        n_obs_steps: int, number of observation steps.
        n_readouts_per_step: int, number of readout tokens used for each observation step.
    Returns:
        mask: torch.Tensor, block-wise causal mask.
    """

    input_seq_len = (n_obs_tokens_per_step + n_readouts_per_step) * n_obs_steps
    mask = torch.full((input_seq_len, input_seq_len), -float("inf"))
    mask = torch.triu(mask, diagonal=1)
    for i in range(n_obs_tokens_per_step, input_seq_len, n_obs_tokens_per_step + n_readouts_per_step):
        for j in range(n_readouts_per_step):
            mask[:, i + j] = -float("inf")
            mask[i + j, i + j] = 0
    return mask


class OctoTransformer(nn.Module):
    """Transformer Encoder for Octo, as described above.

    Args:
        config: OctoConfig, configuration class instance.
        img_dim: int, dimension of the image feature patches.
        n_obs_tokens_per_step: int, number of observation tokens in the input sequence per observation step.
    """

    def __init__(self, config: OctoConfig, img_dim: int, n_obs_tokens_per_step: int):
        super().__init__()

        self.config = config

        self.state_proj = nn.Linear(config.input_shapes["observation.state"][0], config.embed_dim)
        self.img_proj = nn.Linear(img_dim, config.embed_dim)
        self.readout_tokens = nn.Parameter(
            torch.randn((1, config.n_obs_steps, config.n_readouts_per_step, config.embed_dim))
        )

        # init as per original Octo implementation
        self.obs_pos_emb = nn.Parameter(
            torch.normal(
                mean=0, std=torch.full((1, config.n_obs_steps, n_obs_tokens_per_step, config.embed_dim), 0.02)
            )
        )
        self.readout_pos_emb = nn.Parameter(
            torch.normal(
                mean=0,
                std=torch.full((1, config.n_obs_steps, config.n_readouts_per_step, config.embed_dim), 0.02),
            )
        )

        blockwise_causal_mask = make_causal_mask(
            n_obs_tokens_per_step, config.n_obs_steps, config.n_readouts_per_step
        )
        self.register_buffer("blockwise_causal_mask", blockwise_causal_mask)

        encoder_layers = TransformerEncoderLayer(
            config.embed_dim,
            config.n_heads,
            dim_feedforward=config.d_ffn,
            dropout=config.p_dropout,
            batch_first=True,
            norm_first=True,
            activation="gelu",
        )
        self.transformer_encoder = TransformerEncoder(
            encoder_layers, config.n_layers, norm=nn.LayerNorm(config.embed_dim)
        )

    def forward(self, state, img_feats):
        """
        Args:
            state: torch.Tensor, shape [batch_size, n_obs_steps, state_dim]
            img_feats: torch.Tensor, shape [batch_size, n_obs_steps, n_patches, embed_dim]
        Returns:
            readout_embeds: torch.Tensor, shape [batch_size, n_obs_steps, n_readouts_per_step, embed_dim]
        """
        b, t, *_ = img_feats.size()
        img_proj = self.img_proj(img_feats)
        state_proj = self.state_proj(state)

        obs_tokens = (
            torch.cat(
                [
                    state_proj.view((b, t, 1, -1)),
                    img_proj,
                ],
                dim=2,
            )
            + self.obs_pos_emb
        )

        readout_tokens = repeat(self.readout_tokens, "1 t n f -> b t n f", b=b) + self.readout_pos_emb

        x = torch.cat([obs_tokens, readout_tokens], dim=2)
        x = rearrange(x, "b t l f -> b (t l) f")
        x = self.transformer_encoder(x, mask=self.blockwise_causal_mask)
        x = rearrange(x, "b (t l) f -> b t l f", t=t)
        readout_embeds = x[:, :, -self.config.n_readouts_per_step :, :]
        return readout_embeds


class OctoFourierFeatures(nn.Module):
    """Learnable fourier feature transform as in
    "Fourier Features Let Networks Learn High Frequency Functions in Low Dimensional Domains"
    https://arxiv.org/abs/2006.10739
    """

    def __init__(self, output_size, learnable=True):
        super().__init__()

        self.output_size = output_size
        self.learnable = learnable

        if learnable:
            self.kernel = nn.Parameter(torch.randn(output_size // 2, 1))
        else:
            half_dim = output_size // 2
            f = torch.log(10000) / (half_dim - 1)
            f = torch.exp(torch.arange(half_dim) * -f)
            self.register_buffer("f", f)

    def forward(self, x):
        """
        Args:
            x: torch.Tensor, shape [B, 1]
        Returns:
            torch.Tensor, shape [B, output_size]
        """
        f = 2 * torch.pi * x @ self.kernel.t() if self.learnable else self.f * x
        return torch.cat([torch.cos(f), torch.sin(f)], dim=-1)


class OctoMLP(nn.Module):
    """An MLP with SiLU activation function. Original Octo implementation optionally
    uses Dropout and LayerNorm that seem to be disabled by default.

    Args:
        input_dim: int, dimension of the input
        hidden_dims: Iterable[int], dimensions of the hidden layers
    """

    def __init__(self, input_dim, hidden_dims):
        super().__init__()
        layers = []
        for i, dim in enumerate(hidden_dims):
            layers.append(nn.Linear(input_dim, dim))
            if i + 1 < len(hidden_dims):
                layers.append(nn.SiLU())
            input_dim = dim
        self.net = nn.Sequential(*layers)

    def forward(self, x):
        """
        Args:
            x: torch.Tensor, shape [B, input_dim]
        Returns:
            torch.Tensor, shape [B, output_dim]
        """
        return self.net(x)


class OctoMLPResNetBlock(nn.Module):
    """An MLP ResNet block with optional dropout and layer norm.

    Args:
        in_dim: int, input dimension.
        dropout: float, Optional, dropout rate.
        use_layer_norm: bool, Optional, whether to use layer norm.
    """

    def __init__(self, in_dim, dropout=0, use_layer_norm=True):
        super().__init__()

        layers = []
        if dropout > 0:
            layers.append(nn.Dropout(dropout))
        if use_layer_norm:
            layers.append(nn.LayerNorm(in_dim))
        layers += [nn.Linear(in_dim, in_dim * 4), nn.SiLU(), nn.Linear(in_dim * 4, in_dim)]

        self.net = nn.Sequential(*layers)

    def forward(self, x):
        """
        Args:
            x: torch.Tensor, shape [B, in_dim]
        Returns:
            torch.Tensor, shape [B, in_dim]
        """
        return x + self.net(x)


class OctoMLPResNet(nn.Module):
    """An MLP ResNet with optional dropout and layer norm.

    Args:
        in_dim: int, input dimension.
        out_dim: int, output dimension.
        hidden_dim: int, dimension of the hidden layers.
        num_layers: int, number of hidden layers.
    """

    def __init__(self, in_dim, out_dim, hidden_dim, num_layers):
        super().__init__()

        layers = [nn.Linear(in_dim, hidden_dim)]
        for _ in range(num_layers):
            layers.append(OctoMLPResNetBlock(hidden_dim))
        layers += [nn.SiLU(), nn.Linear(hidden_dim, out_dim)]

        self.net = nn.Sequential(*layers)

    def forward(self, x):
        """
        Args:
            x: torch.Tensor, shape [B, in_dim]
        Returns:
            torch.Tensor, shape [B, out_dim]
        """
        return self.net(x)


class OctoDiffusionActionHead(nn.Module):
    """Diffusion Action Head for Octo, as described above.

    Args:
        config: OctoConfig, configuration class instance.
    """

    def __init__(self, config: OctoConfig):
        super().__init__()

        self.fourier_feature_embedder = OctoFourierFeatures(config.time_dim)
        self.time_feature_encoder = OctoMLP(config.time_dim, (2 * config.time_dim, config.time_dim))
        self.net = OctoMLPResNet(
            config.time_dim + config.embed_dim + config.output_shapes["action"][0],
            config.output_shapes["action"][0] * config.horizon,
            hidden_dim=config.diffusion_head_dim,
            num_layers=config.n_diffusion_head_layers,
        )

    def forward(self, readout_embeds, time, actions):
        """
        Args:
            readout_embeds: torch.Tensor, shape [batch_size, n_obs_steps, n_readouts_per_step, embed_dim]
            time: torch.Tensor, shape [batch_size, 1]
            actions: torch.Tensor, shape [batch_size, pred_horizon * action_dim]
        Returns:
            eps_pred: torch.Tensor, shape [batch_size, pred_horizon * action_dim]
        """
        # we use the mean of all readout tokens for now but there is room for experimentation.
        mean_readouts_embed = readout_embeds.mean(dim=(1, 2))
        time_emb = self.fourier_feature_embedder(time)
        time_cond = self.time_feature_encoder(time_emb)
        x = torch.cat([time_cond, mean_readouts_embed, actions], dim=-1)
        eps_pred = self.net(x)
        return eps_pred
