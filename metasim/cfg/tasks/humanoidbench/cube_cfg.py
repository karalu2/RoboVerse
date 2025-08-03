"""Rotate Cube in hand task for humanoid robots."""

from __future__ import annotations

import torch

from metasim.cfg.checkers import _CubeChecker
from metasim.cfg.objects import RigidObjCfg
from metasim.constants import PhysicStateType
from metasim.types import EnvState
from metasim.utils import configclass, humanoid_reward_util, humanoid_robot_util
from metasim.utils.humanoid_robot_util import (
    robot_velocity_tensor,
    body_pos_tensor,
    object_rotation_tensor,
    object_position_tensor,
)

from .base_cfg import HumanoidBaseReward, HumanoidTaskCfg, StableReward


class StandingReward(HumanoidBaseReward):
    """Reward function for maintaining standing posture."""

    def __init__(self, robot_name="h1_hand_hb", ):
        """Initialize the standing reward."""
        super().__init__(robot_name)
        self._stand_height = 0.6

    def __call__(self, states: list[EnvState]) -> torch.FloatTensor:
        """Compute the standing reward."""
        com_vel = robot_velocity_tensor(states, self.robot_name)
        still_x = humanoid_reward_util.tolerance_tensor(com_vel[:, 0], bounds=(0.0, 0.0), margin=2)
        still_y = humanoid_reward_util.tolerance_tensor(com_vel[:, 1], bounds=(0.0, 0.0), margin=2)

        still_reward = (still_x + still_y) / 2
        stable_reward = StableReward(robot_name=self.robot_name)(states)

        return still_reward * stable_reward


class OrientationReward(HumanoidBaseReward):
    """Reward function for cube orientation alignment."""

    def __init__(self, robot_name="h1_hand_hb"):
        """Initialize the orientation reward."""
        super().__init__(robot_name)

    def __call__(self, states: list[EnvState]) -> torch.FloatTensor:
        """Compute the orientation reward."""
        left_cube_rot = object_rotation_tensor(states, "cube_1")
        right_cube_rot = object_rotation_tensor(states, "cube_2")
        target_cube_rot = object_rotation_tensor(states, "cube_destination")

        left_alignment = torch.norm(left_cube_rot - target_cube_rot)
        right_alignment = torch.norm(right_cube_rot - target_cube_rot)

        return left_alignment + right_alignment



class HandProximityReward(HumanoidBaseReward):
    """Reward function for hand-cube proximity."""

    def __init__(self, robot_name="h1_hand_hb"):
        """Initialize the hand proximity reward."""
        super().__init__(robot_name)

    def __call__(self, states: list[EnvState]) -> torch.FloatTensor:
        """Compute the hand proximity reward."""
        left_hand_pos = body_pos_tensor(states, self.robot_name, "left_elbow_link")
        right_hand_pos = body_pos_tensor(states, self.robot_name, "right_elbow_link")
        cube1_pos = object_position_tensor(states, "cube_1")
        cube2_pos = object_position_tensor(states, "cube_2")

        left_dist = torch.norm(left_hand_pos - cube1_pos)
        right_dist = torch.norm(right_hand_pos - cube2_pos)

        left_proximity = humanoid_reward_util.tolerance_tensor(left_dist, bounds=(0.0, 0.0), margin=0.5)
        right_proximity = humanoid_reward_util.tolerance_tensor(right_dist, bounds=(0.0, 0.0), margin=0.5)

        return (left_proximity + right_proximity) / 2


@configclass
class CubeCfg(HumanoidTaskCfg):
    """Cube task for humanoid robots."""

    episode_length = 1000
    objects = [
        RigidObjCfg(
            name="cube_1",
            mjcf_path="roboverse_data/assets/humanoidbench/cube/cube_1/mjcf/cube_1.xml",
            physics=PhysicStateType.GEOM,
        ),
        RigidObjCfg(
            name="cube_2",
            mjcf_path="roboverse_data/assets/humanoidbench/cube/cube_2/mjcf/cube_2.xml",
            physics=PhysicStateType.GEOM,
        ),
        RigidObjCfg(
            name="cube_destination",
            mjcf_path="roboverse_data/assets/humanoidbench/cube/cube_destination/mjcf/cube_destination.xml",
            physics=PhysicStateType.GEOM,
            fix_base_link=True,
        ),
    ]
    traj_filepath = "roboverse_data/trajs/humanoidbench/cube/v2/initial_state_v2.json"
    checker = _CubeChecker()
    reward_weights = [0.2, 0.5, 0.3]
    reward_functions = [StandingReward, OrientationReward, HandProximityReward]

    def extra_spec(self):
        """This task does not require any extra observations."""
        return {}
