# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from argparse import ArgumentParser, Namespace
from typing import Optional, Sequence

import torch
from torch import nn, distributions as d

from torchrl.data import DEVICE_TYPING, CompositeSpec
from torchrl.envs.common import _EnvClass
from torchrl.envs.utils import set_exploration_mode
from torchrl.modules import (
    ActorValueOperator,
    NoisyLinear,
    TDModule,
    NormalParamWrapper,
    TDSequence,
)
from torchrl.modules.distributions import (
    Delta,
    OneHotCategorical,
    TanhDelta,
    TanhNormal,
    TruncatedNormal,
)
from torchrl.modules.distributions.continuous import (
    SafeTanhTransform,
)
from torchrl.modules.models.exploration import LazygSDEModule
from torchrl.modules.models.models import (
    ConvNet,
    DdpgCnnActor,
    DdpgCnnQNet,
    DdpgMlpActor,
    DdpgMlpQNet,
    DuelingCnnDQNet,
    LSTMNet,
    MLP,
    DuelingMlpDQNet,
)
from torchrl.modules.td_module import (
    Actor,
    DistributionalQValueActor,
    QValueActor,
)
from torchrl.modules.td_module.actors import (
    ActorCriticWrapper,
    ValueOperator,
    ProbabilisticActor,
)

DISTRIBUTIONS = {
    "delta": Delta,
    "tanh-normal": TanhNormal,
    "categorical": OneHotCategorical,
    "tanh-delta": TanhDelta,
}

ACTIVATIONS = {
    "elu": nn.ELU,
    "tanh": nn.Tanh,
    "relu": nn.ReLU,
}

__all__ = [
    "make_dqn_actor",
    "make_ddpg_actor",
    "make_ppo_model",
    "make_sac_model",
    "make_redq_model",
    "parser_model_args_continuous",
    "parser_model_args_discrete",
]


def make_dqn_actor(
    proof_environment: _EnvClass, args: Namespace, device: torch.device
) -> Actor:
    """
    DQN constructor helper function.

    Args:
        proof_environment (_EnvClass): a dummy environment to retrieve the observation and action spec.
        args (argparse.Namespace): arguments of the DQN script
        device (torch.device): device on which the model must be cast

    Returns:
         A DQN policy operator.

    Examples:
        >>> from torchrl.trainers.helpers.models import make_dqn_actor, parser_model_args_discrete
        >>> from torchrl.trainers.helpers.envs import parser_env_args
        >>> from torchrl.envs import GymEnv
        >>> from torchrl.envs.transforms import ToTensorImage, TransformedEnv
        >>> import argparse
        >>> proof_environment = TransformedEnv(GymEnv("ALE/Pong-v5",
        ...    pixels_only=True), ToTensorImage())
        >>> device = torch.device("cpu")
        >>> parser = parser_env_args(argparse.ArgumentParser())
        >>> parser = parser_model_args_discrete(parser)
        >>> args = parser.parse_args(['--env_name', 'ALE/Pong-v5', '--from_pixels'])
        >>> actor = make_dqn_actor(proof_environment, args, device)
        >>> _ = proof_environment.reset()
        >>> td = proof_environment.current_tensordict
        >>> print(actor(td))
        TensorDict(
            fields={
                done: Tensor(torch.Size([1]), dtype=torch.bool),
                pixels: Tensor(torch.Size([3, 210, 160]), dtype=torch.float32),
                action: Tensor(torch.Size([6]), dtype=torch.int64),
                action_value: Tensor(torch.Size([6]), dtype=torch.float32),
                chosen_action_value: Tensor(torch.Size([1]), dtype=torch.float32)},
            batch_size=torch.Size([]),
            device=cpu,
            is_shared=False)


    """
    env_specs = proof_environment.specs

    atoms = args.atoms if args.distributional else None
    linear_layer_class = torch.nn.Linear if not args.noisy else NoisyLinear

    action_spec = env_specs["action_spec"]
    if action_spec.domain != "discrete":
        raise ValueError(
            f"env {proof_environment} has an action domain "
            f"{action_spec.domain} which is incompatible with "
            f"DQN. Make sure your environment has a discrete "
            f"domain."
        )

    if args.from_pixels:
        net_class = DuelingCnnDQNet
        default_net_kwargs = {
            "cnn_kwargs": {
                "bias_last_layer": True,
                "depth": None,
                "num_cells": [32, 64, 64],
                "kernel_sizes": [8, 4, 3],
                "strides": [4, 2, 1],
            },
            "mlp_kwargs": {"num_cells": 512, "layer_class": linear_layer_class},
        }
        in_key = "pixels"

    else:
        net_class = DuelingMlpDQNet
        default_net_kwargs = {
            "mlp_kwargs_feature": {},  # see class for details
            "mlp_kwargs_output": {"num_cells": 512, "layer_class": linear_layer_class},
        }
        # automatically infer in key
        in_key = list(env_specs["observation_spec"].keys())[0].split("next_")[-1]

    out_features = env_specs["action_spec"].shape[0]
    actor_class = QValueActor
    actor_kwargs = {}
    if args.distributional:
        if not atoms:
            raise RuntimeError(
                "Expected atoms to be a positive integer, " f"got {atoms}"
            )
        vmin = -3
        vmax = 3

        out_features = (atoms, out_features)
        support = torch.linspace(vmin, vmax, atoms)
        actor_class = DistributionalQValueActor
        actor_kwargs.update({"support": support})
        default_net_kwargs.update({"out_features_value": (atoms, 1)})

    net = net_class(
        out_features=out_features,
        **default_net_kwargs,
    )

    model = actor_class(
        module=net,
        spec=CompositeSpec(action=action_spec),
        in_keys=[in_key],
        safe=True,
        **actor_kwargs,
    ).to(device)

    # init
    with torch.no_grad():
        td = proof_environment.rollout(max_steps=1000)
        model(td.to(device))
    return model


def make_ddpg_actor(
    proof_environment: _EnvClass,
    args: Namespace,
    actor_net_kwargs: Optional[dict] = None,
    value_net_kwargs: Optional[dict] = None,
    device: DEVICE_TYPING = "cpu",
) -> torch.nn.ModuleList:
    """
    DDPG constructor helper function.

    Args:
        proof_environment (_EnvClass): a dummy environment to retrieve the observation and action spec
        args (argparse.Namespace): arguments of the DDPG script
        actor_net_kwargs (dict, optional): kwargs to be used for the policy network (either DdpgCnnActor or
            DdpgMlpActor).
        value_net_kwargs (dict, optional): kwargs to be used for the policy network (either DdpgCnnQNet or
            DdpgMlpQNet).
        device (torch.device, optional): device on which the model must be cast. Default is "cpu".

    Returns:
         An actor and a value operators for DDPG.

    For more details on DDPG, refer to "CONTINUOUS CONTROL WITH DEEP REINFORCEMENT LEARNING",
    https://arxiv.org/pdf/1509.02971.pdf.

    Examples:
        >>> from torchrl.trainers.helpers.envs import parser_env_args
        >>> from torchrl.trainers.helpers.models import make_ddpg_actor, parser_model_args_continuous
        >>> from torchrl.envs import GymEnv
        >>> from torchrl.envs.transforms import CatTensors, TransformedEnv, DoubleToFloat, Compose
        >>> import argparse
        >>> proof_environment = TransformedEnv(GymEnv("HalfCheetah-v2"), Compose(DoubleToFloat(["next_observation"]),
        ...    CatTensors(["next_observation"], "next_observation_vector")))
        >>> device = torch.device("cpu")
        >>> parser = argparse.ArgumentParser()
        >>> parser = parser_env_args(parser)
        >>> args = parser_model_args_continuous(parser, algorithm="DDPG").parse_args([])
        >>> actor, value = make_ddpg_actor(
        ...     proof_environment,
        ...     device=device,
        ...     args=args)
        >>> _ = proof_environment.reset()
        >>> td = proof_environment.current_tensordict
        >>> print(actor(td))
        TensorDict(
            fields={
                done: Tensor(torch.Size([1]), dtype=torch.bool),
                observation_vector: Tensor(torch.Size([17]), dtype=torch.float32),
                param: Tensor(torch.Size([6]), dtype=torch.float32),
                action: Tensor(torch.Size([6]), dtype=torch.float32)},
            batch_size=torch.Size([]),
            device=cpu,
            is_shared=False)
        >>> print(value(td))
        TensorDict(
            fields={
                done: Tensor(torch.Size([1]), dtype=torch.bool),
                observation_vector: Tensor(torch.Size([17]), dtype=torch.float32),
                param: Tensor(torch.Size([6]), dtype=torch.float32),
                action: Tensor(torch.Size([6]), dtype=torch.float32),
                state_action_value: Tensor(torch.Size([1]), dtype=torch.float32)},
            batch_size=torch.Size([]),
            device=cpu,
            is_shared=False)
    """

    # TODO: https://arxiv.org/pdf/1804.08617.pdf

    from_pixels = args.from_pixels
    noisy = args.noisy

    actor_net_kwargs = actor_net_kwargs if actor_net_kwargs is not None else dict()
    value_net_kwargs = value_net_kwargs if value_net_kwargs is not None else dict()

    linear_layer_class = torch.nn.Linear if not noisy else NoisyLinear

    env_specs = proof_environment.specs
    out_features = env_specs["action_spec"].shape[0]

    actor_net_default_kwargs = {
        "action_dim": out_features,
        "mlp_net_kwargs": {
            "layer_class": linear_layer_class,
            "activation_class": ACTIVATIONS[args.activation],
        },
    }
    actor_net_default_kwargs.update(actor_net_kwargs)
    if from_pixels:
        in_keys = ["pixels"]
        actor_net_default_kwargs["conv_net_kwargs"] = {
            "activation_class": ACTIVATIONS[args.activation]
        }
        actor_net = DdpgCnnActor(**actor_net_default_kwargs)
        gSDE_state_key = "hidden"
        out_keys = ["param", "hidden"]
    else:
        in_keys = ["observation_vector"]
        actor_net = DdpgMlpActor(**actor_net_default_kwargs)
        gSDE_state_key = "observation_vector"
        out_keys = ["param"]
    actor_module = TDModule(actor_net, in_keys=in_keys, out_keys=out_keys)

    if args.gSDE:
        min = env_specs["action_spec"].space.minimum
        max = env_specs["action_spec"].space.maximum
        transform = SafeTanhTransform()
        if (min != -1).any() or (max != 1).any():
            transform = d.ComposeTransform(
                transform, d.AffineTransform(loc=(max + min) / 2, scale=(max - min) / 2)
            )
        actor_module = TDSequence(
            actor_module,
            TDModule(
                LazygSDEModule(transform=transform, learn_sigma=False),
                in_keys=["param", gSDE_state_key, "_eps_gSDE"],
                out_keys=["loc", "scale", "action", "_eps_gSDE"],
            ),
        )

    # We use a ProbabilisticActor to make sure that we map the network output to the right space using a TanhDelta
    # distribution.
    actor = ProbabilisticActor(
        module=actor_module,
        dist_param_keys=["param"],
        spec=CompositeSpec(action=env_specs["action_spec"]),
        safe=True,
        distribution_class=TanhDelta,
        distribution_kwargs={
            "min": env_specs["action_spec"].space.minimum,
            "max": env_specs["action_spec"].space.maximum,
        },
    )

    state_class = ValueOperator
    if from_pixels:
        value_net_default_kwargs = {
            "mlp_net_kwargs": {
                "layer_class": linear_layer_class,
                "activation_class": ACTIVATIONS[args.activation],
            }
        }
        value_net_default_kwargs.update(value_net_kwargs)

        in_keys = ["pixels", "action"]
        out_keys = ["state_action_value"]
        q_net = DdpgCnnQNet(**value_net_default_kwargs)
    else:
        value_net_default_kwargs1 = {"activation_class": ACTIVATIONS[args.activation]}
        value_net_default_kwargs1.update(
            value_net_kwargs.get(
                "mlp_net_kwargs_net1",
                {
                    "layer_class": linear_layer_class,
                    "activation_class": ACTIVATIONS[args.activation],
                    "bias_last_layer": True,
                },
            )
        )
        value_net_default_kwargs2 = {
            "num_cells": [400, 300],
            "activation_class": ACTIVATIONS[args.activation],
            "bias_last_layer": True,
        }
        value_net_default_kwargs2.update(
            value_net_kwargs.get(
                "mlp_net_kwargs_net2",
                {
                    "layer_class": linear_layer_class,
                },
            )
        )
        in_keys = ["observation_vector", "action"]
        out_keys = ["state_action_value"]
        q_net = DdpgMlpQNet(
            mlp_net_kwargs_net1=value_net_default_kwargs1,
            mlp_net_kwargs_net2=value_net_default_kwargs2,
        )

    value = state_class(
        in_keys=in_keys,
        out_keys=out_keys,
        module=q_net,
    )

    module = torch.nn.ModuleList([actor, value]).to(device)

    # init
    with torch.no_grad(), set_exploration_mode("random"):
        td = proof_environment.rollout(max_steps=1000)
        td = td.to(device)
        module[0](td)
        module[1](td)

    return module


def make_ppo_model(
    proof_environment: _EnvClass,
    args: Namespace,
    device: DEVICE_TYPING,
    in_keys_actor: Optional[Sequence[str]] = None,
    observation_key=None,
    **kwargs,
) -> ActorValueOperator:
    """
    Actor-value model constructor helper function.
    Currently constructs MLP networks with immutable default arguments as described in "Proximal Policy Optimization
    Algorithms", https://arxiv.org/abs/1707.06347
    Other configurations can easily be implemented by modifying this function at will.

    Args:
        proof_environment (_EnvClass): a dummy environment to retrieve the observation and action spec
        args (argparse.Namespace): arguments of the PPO script
        device (torch.device): device on which the model must be cast.
        in_keys_actor (iterable of strings, optional): observation key to be read by the actor, usually one of
            `'observation_vector'` or `'pixels'`. If none is provided, one of these two keys is chosen based on
            the `args.from_pixels` argument.

    Returns:
         A joined ActorCriticOperator.

    Examples:
        >>> from torchrl.trainers.helpers.envs import parser_env_args
        >>> from torchrl.trainers.helpers.models import make_ppo_model, parser_model_args_continuous
        >>> from torchrl.envs import GymEnv
        >>> from torchrl.envs.transforms import CatTensors, TransformedEnv, DoubleToFloat, Compose
        >>> import argparse
        >>> proof_environment = TransformedEnv(GymEnv("HalfCheetah-v2"), Compose(DoubleToFloat(["next_observation"]),
        ...    CatTensors(["next_observation"], "next_observation_vector")))
        >>> device = torch.device("cpu")
        >>> parser = argparse.ArgumentParser()
        >>> parser = parser_env_args(parser)
        >>> args = parser_model_args_continuous(parser, algorithm="PPO").parse_args(["--shared_mapping"])
        >>> actor_value = make_ppo_model(
        ...     proof_environment,
        ...     device=device,
        ...     args=args,
        ...     )
        >>> actor = actor_value.get_policy_operator()
        >>> value = actor_value.get_value_operator()
        >>> _ = proof_environment.reset()
        >>> td = proof_environment.current_tensordict
        >>> print(actor(td.clone()))
        TensorDict(
            fields={
                done: Tensor(torch.Size([1]), dtype=torch.bool),
                observation_vector: Tensor(torch.Size([17]), dtype=torch.float32),
                hidden: Tensor(torch.Size([300]), dtype=torch.float32),
                loc: Tensor(torch.Size([6]), dtype=torch.float32),
                scale: Tensor(torch.Size([6]), dtype=torch.float32),
                action: Tensor(torch.Size([6]), dtype=torch.float32),
                sample_log_prob: Tensor(torch.Size([1]), dtype=torch.float32)},
            batch_size=torch.Size([]),
            device=cpu,
            is_shared=False)
        >>> print(value(td.clone()))
        TensorDict(
            fields={
                done: Tensor(torch.Size([1]), dtype=torch.bool),
                observation_vector: Tensor(torch.Size([17]), dtype=torch.float32),
                hidden: Tensor(torch.Size([300]), dtype=torch.float32),
                state_value: Tensor(torch.Size([1]), dtype=torch.float32)},
            batch_size=torch.Size([]),
            device=cpu,
            is_shared=False)

    """
    # proof_environment.set_seed(args.seed)
    specs = proof_environment.specs  # TODO: use env.sepcs
    action_spec = specs["action_spec"]
    obs_spec = specs["observation_spec"]

    if observation_key is not None:
        obs_spec = obs_spec[observation_key]
    else:
        obs_spec_values = list(obs_spec.values())
        if len(obs_spec_values) > 1:
            raise RuntimeError(
                "There is more than one observation in the spec, PPO helper "
                "cannot infer automatically which to pick. "
                "Please indicate which key to read via the `observation_key` "
                "keyword in this helper."
            )
        else:
            obs_spec = obs_spec_values[0]

    if in_keys_actor is None and proof_environment.from_pixels:
        in_keys_actor = ["pixels"]
        in_keys_critic = ["pixels"]
    elif in_keys_actor is None:
        in_keys_actor = ["observation_vector"]
        in_keys_critic = ["observation_vector"]
    out_keys = ["action"]

    if action_spec.domain == "continuous":
        out_features = (2 - args.gSDE) * action_spec.shape[-1]
        if args.distribution == "tanh_normal":
            policy_distribution_kwargs = {
                "min": action_spec.space.minimum,
                "max": action_spec.space.maximum,
                "tanh_loc": args.tanh_loc,
            }
            policy_distribution_class = TanhNormal
        elif args.distribution == "truncated_normal":
            policy_distribution_kwargs = {
                "min": action_spec.space.minimum,
                "max": action_spec.space.maximum,
                "tanh_loc": args.tanh_loc,
            }
            policy_distribution_class = TruncatedNormal
    elif action_spec.domain == "discrete":
        out_features = action_spec.shape[-1]
        policy_distribution_kwargs = {}
        policy_distribution_class = OneHotCategorical
    else:
        raise NotImplementedError(
            f"actions with domain {action_spec.domain} are not supported"
        )

    if args.shared_mapping:
        hidden_features = 300
        if proof_environment.from_pixels:
            if in_keys_actor is None:
                in_keys_actor = ["pixels"]
            common_module = ConvNet(
                bias_last_layer=True,
                depth=None,
                num_cells=[32, 64, 64],
                kernel_sizes=[8, 4, 3],
                strides=[4, 2, 1],
            )
        else:
            if args.lstm:
                raise NotImplementedError(
                    "lstm not yet compatible with shared mapping for PPO"
                )
            common_module = MLP(
                num_cells=[
                    400,
                ],
                out_features=hidden_features,
                activate_last_layer=True,
            )
        common_operator = TDModule(
            spec=None,
            module=common_module,
            in_keys=in_keys_actor,
            out_keys=["hidden"],
        )

        policy_net = MLP(
            num_cells=[200],
            out_features=out_features,
        )
        if not args.gSDE:
            actor_net = NormalParamWrapper(
                policy_net, scale_mapping=f"biased_softplus_{args.default_policy_scale}"
            )
            in_keys = ["hidden"]
            actor_module = TDModule(
                actor_net, in_keys=in_keys, out_keys=["loc", "scale"]
            )
        else:
            in_keys = ["hidden"]
            gSDE_state_key = "hidden"
            actor_module = TDModule(
                policy_net,
                in_keys=in_keys,
                out_keys=["action"],  # will be overwritten
            )

            if action_spec.domain == "continuous":
                min = action_spec.space.minimum
                max = action_spec.space.maximum
                transform = SafeTanhTransform()
                if (min != -1).any() or (max != 1).any():
                    transform = d.ComposeTransform(
                        transform,
                        d.AffineTransform(loc=(max + min) / 2, scale=(max - min) / 2),
                    )
            else:
                raise RuntimeError("cannot use gSDE with discrete actions")

            actor_module = TDSequence(
                actor_module,
                TDModule(
                    LazygSDEModule(transform=transform),
                    in_keys=["action", gSDE_state_key, "_eps_gSDE"],
                    out_keys=["loc", "scale", "action", "_eps_gSDE"],
                ),
            )

        policy_operator = ProbabilisticActor(
            spec=CompositeSpec(action=action_spec),
            module=actor_module,
            dist_param_keys=["loc", "scale"],
            default_interaction_mode="random",
            distribution_class=policy_distribution_class,
            distribution_kwargs=policy_distribution_kwargs,
            return_log_prob=True,
        )
        value_net = MLP(
            num_cells=[200],
            out_features=1,
        )
        value_operator = ValueOperator(value_net, in_keys=["hidden"])
        actor_value = ActorValueOperator(
            common_operator=common_operator,
            policy_operator=policy_operator,
            value_operator=value_operator,
        ).to(device)
    else:
        if args.from_pixels:
            raise RuntimeError(
                "PPO learnt from pixels require the shared_mapping to be set to True."
            )
        if args.lstm:
            policy_net = LSTMNet(
                out_features=out_features,
                lstm_kwargs={"input_size": 256, "hidden_size": 256},
                mlp_kwargs={"num_cells": [256, 256], "out_features": 256},
            )
            in_keys_actor += ["hidden0", "hidden1"]
            out_keys += ["hidden0", "hidden1", "next_hidden0", "next_hidden1"]
        else:
            policy_net = MLP(
                num_cells=[400, 300],
                out_features=out_features,
            )

        if not args.gSDE:
            actor_net = NormalParamWrapper(
                policy_net, scale_mapping=f"biased_softplus_{args.default_policy_scale}"
            )
            actor_module = TDModule(
                actor_net, in_keys=in_keys_actor, out_keys=["loc", "scale"]
            )
        else:
            in_keys = in_keys_actor
            gSDE_state_key = in_keys_actor[0]
            actor_module = TDModule(
                policy_net,
                in_keys=in_keys,
                out_keys=["action"],  # will be overwritten
            )

            if action_spec.domain == "continuous":
                min = action_spec.space.minimum
                max = action_spec.space.maximum
                transform = SafeTanhTransform()
                if (min != -1).any() or (max != 1).any():
                    transform = d.ComposeTransform(
                        transform,
                        d.AffineTransform(loc=(max + min) / 2, scale=(max - min) / 2),
                    )
            else:
                raise RuntimeError("cannot use gSDE with discrete actions")

            actor_module = TDSequence(
                actor_module,
                TDModule(
                    LazygSDEModule(transform=transform),
                    in_keys=["action", gSDE_state_key, "_eps_gSDE"],
                    out_keys=["loc", "scale", "action", "_eps_gSDE"],
                ),
            )

        policy_po = ProbabilisticActor(
            actor_module,
            spec=action_spec,
            dist_param_keys=["loc", "scale"],
            distribution_class=policy_distribution_class,
            distribution_kwargs=policy_distribution_kwargs,
            return_log_prob=True,
            default_interaction_mode="random",
        )

        value_net = MLP(
            num_cells=[400, 300],
            out_features=1,
        )
        value_po = ValueOperator(
            value_net,
            in_keys=in_keys_critic,
        )
        actor_value = ActorCriticWrapper(policy_po, value_po).to(device)

    with torch.no_grad(), set_exploration_mode("random"):
        td = proof_environment.rollout(max_steps=1000)
        td_device = td.to(device)
        td_device = actor_value(td_device)  # for init
    return actor_value


def make_sac_model(
    proof_environment: _EnvClass,
    args: Namespace,
    device: DEVICE_TYPING = "cpu",
    in_keys: Optional[Sequence[str]] = None,
    actor_net_kwargs=None,
    qvalue_net_kwargs=None,
    value_net_kwargs=None,
    observation_key=None,
    **kwargs,
) -> nn.ModuleList:
    """
    Actor, Q-value and value model constructor helper function for SAC.

    Follows default parameters proposed in SAC original paper: https://arxiv.org/pdf/1801.01290.pdf.
    Other configurations can easily be implemented by modifying this function at will.

    Args:
        proof_environment (_EnvClass): a dummy environment to retrieve the observation and action spec
        args (argparse.Namespace): arguments of the SAC script
        device (torch.device, optional): device on which the model must be cast. Default is "cpu".
        in_keys (iterable of strings, optional): observation key to be read by the actor, usually one of
            `'observation_vector'` or `'pixels'`. If none is provided, one of these two keys is chosen
             based on the `args.from_pixels` argument.
        actor_net_kwargs (dict, optional): kwargs of the actor MLP.
        qvalue_net_kwargs (dict, optional): kwargs of the qvalue MLP.
        value_net_kwargs (dict, optional): kwargs of the value MLP.

    Returns:
         A nn.ModuleList containing the actor, qvalue operator(s) and the value operator.

    Examples:
        >>> from torchrl.trainers.helpers.envs import parser_env_args
        >>> from torchrl.trainers.helpers.models import make_sac_model, parser_model_args_continuous
        >>> from torchrl.envs import GymEnv
        >>> from torchrl.envs.transforms import CatTensors, TransformedEnv, DoubleToFloat, Compose
        >>> import argparse
        >>> proof_environment = TransformedEnv(GymEnv("HalfCheetah-v2"), Compose(DoubleToFloat(["next_observation"]),
        ...    CatTensors(["next_observation"], "next_observation_vector")))
        >>> device = torch.device("cpu")
        >>> parser = argparse.ArgumentParser()
        >>> parser = parser_env_args(parser)
        >>> args = parser_model_args_continuous(parser,
        ...    algorithm="SAC").parse_args([])
        >>> model = make_sac_model(
        ...     proof_environment,
        ...     device=device,
        ...     args=args,
        ...     )
        >>> actor, qvalue, value = model
        >>> _ = proof_environment.reset()
        >>> td = proof_environment.current_tensordict
        >>> print(actor(td))
        TensorDict(
            fields={
                done: Tensor(torch.Size([1]), dtype=torch.bool),
                observation_vector: Tensor(torch.Size([17]), dtype=torch.float32),
                loc: Tensor(torch.Size([6]), dtype=torch.float32),
                scale: Tensor(torch.Size([6]), dtype=torch.float32),
                action: Tensor(torch.Size([6]), dtype=torch.float32)},
            batch_size=torch.Size([]),
            device=cpu,
            is_shared=False)
        >>> print(qvalue(td.clone()))
        TensorDict(
            fields={
                done: Tensor(torch.Size([1]), dtype=torch.bool),
                observation_vector: Tensor(torch.Size([17]), dtype=torch.float32),
                loc: Tensor(torch.Size([6]), dtype=torch.float32),
                scale: Tensor(torch.Size([6]), dtype=torch.float32),
                action: Tensor(torch.Size([6]), dtype=torch.float32),
                state_action_value: Tensor(torch.Size([1]), dtype=torch.float32)},
            batch_size=torch.Size([]),
            device=cpu,
            is_shared=False)
        >>> print(value(td.clone()))
        TensorDict(
            fields={
                done: Tensor(torch.Size([1]), dtype=torch.bool),
                observation_vector: Tensor(torch.Size([17]), dtype=torch.float32),
                loc: Tensor(torch.Size([6]), dtype=torch.float32),
                scale: Tensor(torch.Size([6]), dtype=torch.float32),
                action: Tensor(torch.Size([6]), dtype=torch.float32),
                state_value: Tensor(torch.Size([1]), dtype=torch.float32)},
            batch_size=torch.Size([]),
            device=cpu,
            is_shared=False)

    """
    tanh_loc = args.tanh_loc
    default_policy_scale = args.default_policy_scale
    gSDE = args.gSDE

    proof_environment.reset()
    td = proof_environment.current_tensordict
    action_spec = proof_environment.action_spec
    obs_spec = proof_environment.observation_spec

    if observation_key is not None:
        obs_spec = obs_spec[observation_key]
    else:
        obs_spec_values = list(obs_spec.values())
        if len(obs_spec_values) > 1:
            raise RuntimeError(
                "There is more than one observation in the spec, SAC helper "
                "cannot infer automatically which to pick. "
                "Please indicate which key to read via the `observation_key` "
                "keyword in this helper."
            )
        else:
            obs_spec = obs_spec_values[0]

    if actor_net_kwargs is None:
        actor_net_kwargs = {}
    if value_net_kwargs is None:
        value_net_kwargs = {}
    if qvalue_net_kwargs is None:
        qvalue_net_kwargs = {}

    if in_keys is None:
        in_keys = ["observation_vector"]

    actor_net_kwargs_default = {
        "num_cells": [args.actor_cells, args.actor_cells],
        "out_features": (2 - gSDE) * action_spec.shape[-1],
        "activation_class": ACTIVATIONS[args.activation],
    }
    actor_net_kwargs_default.update(actor_net_kwargs)
    actor_net = MLP(**actor_net_kwargs_default)

    qvalue_net_kwargs_default = {
        "num_cells": [args.qvalue_cells, args.qvalue_cells],
        "out_features": 1,
        "activation_class": ACTIVATIONS[args.activation],
    }
    qvalue_net_kwargs_default.update(qvalue_net_kwargs)
    qvalue_net = MLP(
        **qvalue_net_kwargs_default,
    )

    value_net_kwargs_default = {
        "num_cells": [args.value_cells, args.value_cells],
        "out_features": 1,
        "activation_class": ACTIVATIONS[args.activation],
    }
    value_net_kwargs_default.update(value_net_kwargs)
    value_net = MLP(
        **value_net_kwargs_default,
    )

    dist_class = TanhNormal
    dist_kwargs = {
        "min": action_spec.space.minimum,
        "max": action_spec.space.maximum,
        "tanh_loc": tanh_loc,
    }

    if not gSDE:
        actor_net = NormalParamWrapper(
            actor_net,
            scale_mapping=f"biased_softplus_{default_policy_scale}",
            scale_lb=args.scale_lb,
        )
        in_keys_actor = in_keys
        actor_module = TDModule(
            actor_net,
            in_keys=in_keys_actor,
            out_keys=[
                "loc",
                "scale",
            ],
        )

    else:
        gSDE_state_key = in_keys[0]
        actor_module = TDModule(
            actor_net,
            in_keys=in_keys,
            out_keys=["action"],  # will be overwritten
        )

        if action_spec.domain == "continuous":
            min = action_spec.space.minimum
            max = action_spec.space.maximum
            transform = SafeTanhTransform()
            if (min != -1).any() or (max != 1).any():
                transform = d.ComposeTransform(
                    transform,
                    d.AffineTransform(loc=(max + min) / 2, scale=(max - min) / 2),
                )
        else:
            raise RuntimeError("cannot use gSDE with discrete actions")

        actor_module = TDSequence(
            actor_module,
            TDModule(
                LazygSDEModule(transform=transform),
                in_keys=["action", gSDE_state_key, "_eps_gSDE"],
                out_keys=["loc", "scale", "action", "_eps_gSDE"],
            ),
        )

    actor = ProbabilisticActor(
        spec=action_spec,
        dist_param_keys=["loc", "scale"],
        module=actor_module,
        distribution_class=dist_class,
        distribution_kwargs=dist_kwargs,
        default_interaction_mode="random",
        return_log_prob=False,
    )

    qvalue = ValueOperator(
        in_keys=["action"] + in_keys,
        module=qvalue_net,
    )
    value = ValueOperator(
        in_keys=in_keys,
        module=value_net,
    )
    model = nn.ModuleList([actor, qvalue, value]).to(device)

    # init nets
    with torch.no_grad(), set_exploration_mode("random"):
        td = td.to(device)
        for net in model:
            net(td)
    del td

    return model


def make_redq_model(
    proof_environment: _EnvClass,
    args: Namespace,
    device: DEVICE_TYPING = "cpu",
    in_keys: Optional[Sequence[str]] = None,
    actor_net_kwargs=None,
    qvalue_net_kwargs=None,
    observation_key=None,
    **kwargs,
) -> nn.ModuleList:
    """
    Actor and Q-value model constructor helper function for REDQ.
    Follows default parameters proposed in REDQ original paper: https://openreview.net/pdf?id=AY8zfZm0tDd.
    Other configurations can easily be implemented by modifying this function at will.
    A single instance of the Q-value model is returned. It will be multiplicated by the loss function.

    Args:
        proof_environment (_EnvClass): a dummy environment to retrieve the observation and action spec
        args (argparse.Namespace): arguments of the REDQ script
        device (torch.device, optional): device on which the model must be cast. Default is "cpu".
        in_keys (iterable of strings, optional): observation key to be read by the actor, usually one of
            `'observation_vector'` or `'pixels'`. If none is provided, one of these two keys is chosen
             based on the `args.from_pixels` argument.
        actor_net_kwargs (dict, optional): kwargs of the actor MLP.
        qvalue_net_kwargs (dict, optional): kwargs of the qvalue MLP.

    Returns:
         A nn.ModuleList containing the actor, qvalue operator(s) and the value operator.

    Examples:
        >>> from torchrl.trainers.helpers.envs import parser_env_args
        >>> from torchrl.trainers.helpers.models import make_redq_model, parser_model_args_continuous
        >>> from torchrl.envs import GymEnv
        >>> from torchrl.envs.transforms import CatTensors, TransformedEnv, DoubleToFloat, Compose
        >>> import argparse
        >>> proof_environment = TransformedEnv(GymEnv("HalfCheetah-v2"), Compose(DoubleToFloat(["next_observation"]),
        ...    CatTensors(["next_observation"], "next_observation_vector")))
        >>> device = torch.device("cpu")
        >>> parser = argparse.ArgumentParser()
        >>> parser = parser_env_args(parser)
        >>> args = parser_model_args_continuous(parser,
        ...    algorithm="REDQ").parse_args([])
        >>> model = make_redq_model(
        ...     proof_environment,
        ...     device=device,
        ...     args=args,
        ...     )
        >>> actor, qvalue = model
        >>> _ = proof_environment.reset()
        >>> td = proof_environment.current_tensordict
        >>> print(actor(td))
        TensorDict(
            fields={
                done: Tensor(torch.Size([1]), dtype=torch.bool),
                observation_vector: Tensor(torch.Size([17]), dtype=torch.float32),
                loc: Tensor(torch.Size([6]), dtype=torch.float32),
                scale: Tensor(torch.Size([6]), dtype=torch.float32),
                action: Tensor(torch.Size([6]), dtype=torch.float32),
                sample_log_prob: Tensor(torch.Size([1]), dtype=torch.float32)},
            batch_size=torch.Size([]),
            device=cpu,
            is_shared=False)
        >>> print(qvalue(td.clone()))
        TensorDict(
            fields={
                done: Tensor(torch.Size([1]), dtype=torch.bool),
                observation_vector: Tensor(torch.Size([17]), dtype=torch.float32),
                loc: Tensor(torch.Size([6]), dtype=torch.float32),
                scale: Tensor(torch.Size([6]), dtype=torch.float32),
                action: Tensor(torch.Size([6]), dtype=torch.float32),
                sample_log_prob: Tensor(torch.Size([1]), dtype=torch.float32),
                state_action_value: Tensor(torch.Size([1]), dtype=torch.float32)},
            batch_size=torch.Size([]),
            device=cpu,
            is_shared=False)

    """

    tanh_loc = args.tanh_loc
    default_policy_scale = args.default_policy_scale
    gSDE = args.gSDE

    proof_environment.reset()
    td = proof_environment.current_tensordict
    action_spec = proof_environment.action_spec
    obs_spec = proof_environment.observation_spec

    if observation_key is not None:
        obs_spec = obs_spec[observation_key]
    else:
        obs_spec_values = list(obs_spec.values())
        if len(obs_spec_values) > 1:
            raise RuntimeError(
                "There is more than one observation in the spec, REDQ helper "
                "cannot infer automatically which to pick. "
                "Please indicate which key to read via the `observation_key` "
                "keyword in this helper."
            )
        else:
            obs_spec = obs_spec_values[0]

    if actor_net_kwargs is None:
        actor_net_kwargs = {}
    if qvalue_net_kwargs is None:
        qvalue_net_kwargs = {}

    linear_layer_class = torch.nn.Linear if not args.noisy else NoisyLinear

    out_features_actor = (2 - gSDE) * action_spec.shape[-1]
    if args.from_pixels:
        if in_keys is None:
            in_keys_actor = ["pixels"]
        else:
            in_keys_actor = in_keys
        actor_net_kwargs_default = {
            "mlp_net_kwargs": {
                "layer_class": linear_layer_class,
                "activation_class": ACTIVATIONS[args.activation],
            },
            "conv_net_kwargs": {"activation_class": ACTIVATIONS[args.activation]},
        }
        actor_net_kwargs_default.update(actor_net_kwargs)
        actor_net = DdpgCnnActor(out_features_actor, **actor_net_kwargs_default)
        gSDE_state_key = "hidden"
        out_keys_actor = ["param", "hidden"]

        value_net_default_kwargs = {
            "mlp_net_kwargs": {
                "layer_class": linear_layer_class,
                "activation_class": ACTIVATIONS[args.activation],
            },
            "conv_net_kwargs": {"activation_class": ACTIVATIONS[args.activation]},
        }
        value_net_default_kwargs.update(qvalue_net_kwargs)

        in_keys_qvalue = ["pixels", "action"]
        qvalue_net = DdpgCnnQNet(**value_net_default_kwargs)
    else:
        if in_keys is None:
            in_keys_actor = ["observation_vector"]
        else:
            in_keys_actor = in_keys

        actor_net_kwargs_default = {
            "num_cells": [args.actor_cells, args.actor_cells],
            "out_features": out_features_actor,
            "activation_class": ACTIVATIONS[args.activation],
        }
        actor_net_kwargs_default.update(actor_net_kwargs)
        actor_net = MLP(**actor_net_kwargs_default)
        out_keys_actor = ["param"]
        gSDE_state_key = in_keys_actor[0]

        qvalue_net_kwargs_default = {
            "num_cells": [args.qvalue_cells, args.qvalue_cells],
            "out_features": 1,
            "activation_class": ACTIVATIONS[args.activation],
        }
        qvalue_net_kwargs_default.update(qvalue_net_kwargs)
        qvalue_net = MLP(
            **qvalue_net_kwargs_default,
        )
        in_keys_qvalue = in_keys_actor + ["action"]

    dist_class = TanhNormal
    dist_kwargs = {
        "min": action_spec.space.minimum,
        "max": action_spec.space.maximum,
        "tanh_loc": tanh_loc,
    }

    if not gSDE:
        actor_net = NormalParamWrapper(
            actor_net,
            scale_mapping=f"biased_softplus_{default_policy_scale}",
            scale_lb=args.scale_lb,
        )
        actor_module = TDModule(
            actor_net,
            in_keys=in_keys_actor,
            out_keys=["loc", "scale"] + out_keys_actor[1:],
        )

    else:
        actor_module = TDModule(
            actor_net,
            in_keys=in_keys_actor,
            out_keys=["action"] + out_keys_actor[1:],  # will be overwritten
        )

        if action_spec.domain == "continuous":
            min = action_spec.space.minimum
            max = action_spec.space.maximum
            transform = SafeTanhTransform()
            if (min != -1).any() or (max != 1).any():
                transform = d.ComposeTransform(
                    transform,
                    d.AffineTransform(loc=(max + min) / 2, scale=(max - min) / 2),
                )
        else:
            raise RuntimeError("cannot use gSDE with discrete actions")

        actor_module = TDSequence(
            actor_module,
            TDModule(
                LazygSDEModule(transform=transform),
                in_keys=["action", gSDE_state_key, "_eps_gSDE"],
                out_keys=["loc", "scale", "action", "_eps_gSDE"],
            ),
        )

    actor = ProbabilisticActor(
        spec=action_spec,
        dist_param_keys=["loc", "scale"],
        module=actor_module,
        distribution_class=dist_class,
        distribution_kwargs=dist_kwargs,
        default_interaction_mode="random",
        return_log_prob=True,
    )
    qvalue = ValueOperator(
        in_keys=in_keys_qvalue,
        module=qvalue_net,
    )
    model = nn.ModuleList([actor, qvalue]).to(device)

    # init nets
    with torch.no_grad(), set_exploration_mode("random"):
        td = td.to(device)
        for net in model:
            net(td)
    del td
    return model


def parser_model_args_continuous(
    parser: ArgumentParser, algorithm: str
) -> ArgumentParser:
    """
    Populates the argument parser to build a model for continuous actions.

    Args:
        parser (ArgumentParser): parser to be populated.
        algorithm (str): one of `"DDPG"`, `"SAC"`, `"REDQ"`, `"PPO"`

    """

    if algorithm not in ("SAC", "DDPG", "PPO", "REDQ"):
        raise NotImplementedError(f"Unknown algorithm {algorithm}")

    if algorithm in ("SAC", "DDPG", "REDQ"):
        parser.add_argument(
            "--annealing_frames",
            "--annealing-frames",
            type=int,
            default=1000000,
            help="float of frames used for annealing of the OrnsteinUhlenbeckProcess. Default=1e6.",
        )
        parser.add_argument(
            "--noisy",
            action="store_true",
            help="whether to use NoisyLinearLayers in the value network.",
        )
        parser.add_argument(
            "--ou_exploration",
            "--ou-exploration",
            action="store_true",
            help="wraps the policy in an OU exploration wrapper, similar to DDPG. SAC being designed for "
            "efficient entropy-based exploration, this should be left for experimentation only.",
        )
        parser.add_argument(
            "--no_ou_exploration",
            "--no-ou-exploration",
            action="store_false",
            dest="ou_exploration",
            help="Aimed at superseeding --ou_exploration.",
        )
        parser.add_argument(
            "--distributional",
            action="store_true",
            help="whether a distributional loss should be used (TODO: not implemented yet).",
        )
        parser.add_argument(
            "--atoms",
            type=int,
            default=51,
            help="number of atoms used for the distributional loss (TODO)",
        )

    parser.add_argument(
        "--gSDE",
        action="store_true",
        help="if True, exploration is achieved using the gSDE technique.",
    )
    if algorithm in ("SAC", "PPO", "REDQ"):
        parser.add_argument(
            "--tanh_loc",
            "--tanh-loc",
            action="store_true",
            help="if True, uses a Tanh-Normal transform for the policy "
            "location of the form "
            "`upscale * tanh(loc/upscale)` (only available with "
            "TanhTransform and TruncatedGaussian distributions)",
        )
        parser.add_argument(
            "--default_policy_scale",
            "--default-policy-scale",
            default=1.0,
            help="Default policy scale parameter",
        )
        parser.add_argument(
            "--distribution",
            type=str,
            default="tanh_normal",
            help="if True, uses a Tanh-Normal-Tanh distribution for the policy",
        )
    if algorithm == "PPO":
        parser.add_argument(
            "--lstm",
            action="store_true",
            help="if True, uses an LSTM for the policy.",
        )
        parser.add_argument(
            "--shared_mapping",
            "--shared-mapping",
            action="store_true",
            help="if True, the first layers of the actor-critic are shared.",
        )

    if algorithm in ("SAC", "REDQ"):
        parser.add_argument(
            "--actor_cells",
            "--actor-cells",
            type=int,
            default=256,
            help="cells of the actor",
        )
        parser.add_argument(
            "--qvalue_cells",
            "--qvalue-cells",
            type=int,
            default=256,
            help="cells of the qvalue net",
        )
        parser.add_argument(
            "--scale_lb",
            "--scale-lb",
            type=float,
            default=0.1,
            help="min value of scale",
        )

    if algorithm in ("SAC", "REDQ"):
        parser.add_argument(
            "--value_cells",
            type=int,
            default=256,
            help="cells of the value net",
        )

    if algorithm in ("SAC", "DDPG", "REDQ"):
        parser.add_argument(
            "--activation",
            type=str,
            choices=["relu", "elu", "tanh"],
            default="tanh",
            help="activation function",
        )

    return parser


def parser_model_args_discrete(parser: ArgumentParser) -> ArgumentParser:
    """
    Populates the argument parser to build a model for discrete actions.

    Args:
        parser (ArgumentParser): parser to be populated.

    """
    parser.add_argument(
        "--annealing_frames",
        "--annealing-frames",
        type=int,
        default=1000000,
        help="Number of frames used for annealing of the EGreedy exploration. Default=1e6.",
    )

    parser.add_argument(
        "--noisy",
        action="store_true",
        help="whether to use NoisyLinearLayers in the value network.",
    )
    parser.add_argument(
        "--distributional",
        action="store_true",
        help="whether a distributional loss should be used.",
    )
    parser.add_argument(
        "--atoms",
        type=int,
        default=51,
        help="number of atoms used for the distributional loss",
    )

    return parser