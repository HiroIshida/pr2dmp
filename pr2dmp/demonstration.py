import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Literal, Optional, Tuple

import numpy as np
from movement_primitives.dmp import DMP, CartesianDMP
from plainmp.ik import IKConfig, solve_ik
from plainmp.robot_spec import PR2LarmSpec, PR2RarmSpec
from plainmp.utils import set_robot_state
from scipy.spatial.transform import Rotation as R
from scipy.spatial.transform import Slerp
from skrobot.coordinates.math import matrix2quaternion, wxyz2xyzw, xyzw2wxyz

from pr2dmp.utils import RichTrasnform


def root_path() -> Path:
    root = Path("~/.pr2dmp").expanduser()
    root.mkdir(exist_ok=True)
    return root


def project_root_path(project_name: str) -> Path:
    root = root_path() / project_name
    root.mkdir(exist_ok=True)
    return root


@dataclass
class DMPParameter:
    forcing_term_pos: Optional[np.ndarray] = None
    forcing_term_rot: Optional[np.ndarray] = None
    gripper_forcing_term: Optional[np.ndarray] = None
    goal_pos_diff: Optional[np.ndarray] = None
    # NOTE: goal rot diff is not used in the current implementation

    def to_vector(self) -> np.ndarray:
        if self.forcing_term_pos is None:
            self.forcing_term_pos = np.zeros((3, 10))
        if self.forcing_term_rot is None:
            self.forcing_term_rot = np.zeros((3, 10))
        if self.gripper_forcing_term is None:
            self.gripper_forcing_term = np.zeros(10)
        if self.goal_pos_diff is None:
            self.goal_pos_diff = np.zeros(3)
        return np.hstack(
            [
                self.forcing_term_pos.flatten(),
                self.forcing_term_rot.flatten(),
                self.gripper_forcing_term,
                self.goal_pos_diff,
            ]
        )


@dataclass
class Demonstration:
    ef_frame: str
    ref_frame: str
    tf_ef_to_ref_list: List[RichTrasnform]
    q_list: Optional[List[np.ndarray]]
    joint_names: List[str]
    gripper_width_list: List[float]
    tf_ref_to_base: Optional[RichTrasnform] = None  # aux info

    def __post_init__(self) -> None:
        assert len(self.tf_ef_to_ref_list) == len(self.q_list)

    def __len__(self) -> int:
        return len(self.tf_ef_to_ref_list)

    def save(self, project_name: str) -> None:
        def transform_to_vector(t: RichTrasnform) -> np.ndarray:
            rot = t.rotation
            return np.hstack([t.translation, rot.flatten()])

        dic = {}
        dic["ef_frame"] = self.ef_frame
        dic["ref_frame"] = self.ref_frame
        dic["trajectory"] = [transform_to_vector(t).tolist() for t in self.tf_ef_to_ref_list]
        dic["q_list"] = [q.tolist() for q in self.q_list]
        dic["joint_names"] = self.joint_names
        dic["gripper_width"] = self.gripper_width_list
        if self.tf_ref_to_base is not None:
            dic["tf_ref_to_base"] = transform_to_vector(self.tf_ref_to_base).tolist()
        path = project_root_path(project_name) / "demonstration.json"
        with open(path, "w") as f:
            json.dump(dic, f, indent=4)

    @classmethod
    def load(cls, project_name: str) -> "Demonstration":
        path = project_root_path(project_name) / "demonstration.json"
        with open(path, "r") as f:
            dic = json.load(f)
        ef_frame = dic["ef_frame"]
        ref_frame = dic["ref_frame"]
        trajectory = []
        for t in dic["trajectory"]:
            translation = t[:3]
            rotation = t[3:]
            rot = np.array(rotation).reshape(3, 3)
            t = RichTrasnform(translation, rot, frame_from=ef_frame, frame_to=ref_frame)
            trajectory.append(t)
        q_list = [np.array(q) for q in dic["q_list"]]
        joint_names = dic["joint_names"]
        gripper_width = dic["gripper_width"]
        if "tf_ref_to_base" in dic:
            t = dic["tf_ref_to_base"]
            translation = t[:3]
            rotation = t[3:]
            rot = np.array(rotation).reshape(3, 3)
            tf_ref_to_base = RichTrasnform(
                translation, rot, frame_from=ref_frame, frame_to="base_footprint"
            )
        return cls(
            ef_frame, ref_frame, trajectory, q_list, joint_names, gripper_width, tf_ref_to_base
        )

    @staticmethod
    def resample_sequence(sequence, k):
        # uniform resampling
        n_seq = len(sequence)
        if k > n_seq:
            raise ValueError("k cannot be larger than sequence length")
        if k < 2:
            raise ValueError("k must be at least 2")
        interval = (n_seq - 1) / (k - 1)
        indices = np.round(np.arange(0, k) * interval).astype(int)
        indices[-1] = min(indices[-1], n_seq - 1)
        return sequence[indices]

    @staticmethod
    def get_interpolated(points: np.ndarray, n_wp_resample: int) -> np.ndarray:
        assert len(points) < n_wp_resample
        n_wp_orignal = len(points)
        n_segment_original = n_wp_orignal - 1
        m_assign_base = n_wp_resample // n_segment_original
        n_wp_arr = np.array([m_assign_base] * n_segment_original)
        rem = n_wp_resample % n_segment_original
        for i in range(rem):
            n_wp_arr[i] += 1
        assert sum(n_wp_arr) == n_wp_resample
        vec_list = []
        for i_segment in range(n_segment_original):
            start = points[i_segment]
            end = points[i_segment + 1]
            tlin = np.linspace(0, 1, n_wp_arr[i_segment] + 1)
            for t in tlin[1:]:
                vec = start * (1 - t) + end * t
                vec_list.append(vec)
        assert len(vec_list) == n_wp_resample
        return np.array(vec_list)

    def get_dmp_trajectory(self, param: Optional[DMPParameter] = None) -> np.ndarray:
        # resample
        n_wp_resample = 100  # except the start point
        n_wp_orignal = len(self)
        n_segment_original = n_wp_orignal - 1
        m_assign_base = n_wp_resample // n_segment_original
        n_wp_arr = np.array([m_assign_base] * n_segment_original)
        rem = n_wp_resample % n_segment_original
        for i in range(rem):
            n_wp_arr[i] += 1
        assert sum(n_wp_arr) == n_wp_resample

        vec_list = []

        # the first point
        tf = self.tf_ef_to_ref_list[0]
        pos = tf.translation
        rot = tf.rotation
        wxyz = matrix2quaternion(rot)
        vec = np.hstack([pos, wxyz])
        vec_list.append(vec)

        for i_segment in range(n_segment_original):
            tf_start = self.tf_ef_to_ref_list[i_segment]
            tf_end = self.tf_ef_to_ref_list[i_segment + 1]
            pos_start, rot_start = tf_start.translation, tf_start.rotation
            pos_end, rot_end = tf_end.translation, tf_end.rotation

            tlin = np.linspace(0, 1, n_wp_arr[i_segment] + 1)

            # position
            pos_list = []
            for t in tlin[1:]:
                pos = pos_start * (1 - t) + pos_end * t
                pos_list.append(pos)

            # rotation
            rotations = R.from_matrix(np.array([rot_start, rot_end]))
            slerp = Slerp(np.array([0, 1]), rotations)
            interp_rots = slerp(tlin[1:])
            quat_list = []
            # https://dfki-ric.github.io/pytransform3d/_apidoc/pytransform3d.rotations.matrix_from_quaternion.html#pytransform3d.rotations.matrix_from_quaternion
            # NOTE: movement_primitives.dmp.CartesianDMP uses wxyz
            for rot in interp_rots:
                xyzw = rot.as_quat()
                quat_list.append(xyzw2wxyz(xyzw))

            assert len(pos_list) == len(quat_list)
            for pos, quat in zip(pos_list, quat_list):
                vec = np.hstack([pos, quat])
                vec_list.append(vec)

        exec_time = 1.0
        dt = 0.01
        n_weights_per_dim = 10
        T = np.linspace(0, 1, 101)

        cartesian_dmp = CartesianDMP(
            exec_time, dt=dt, n_weights_per_dim=n_weights_per_dim, int_dt=0.0001
        )
        Y = np.array(vec_list)
        cartesian_dmp.imitate(T, Y)
        cartesian_dmp.configure(start_y=Y[0], goal_y=Y[-1])

        gripper_traj_resampled = self.get_interpolated(
            np.array(self.gripper_width_list).reshape(-1, 1), 101
        )
        gripper_dmp = DMP(
            1, execution_time=exec_time, dt=dt, n_weights_per_dim=n_weights_per_dim, int_dt=0.0001
        )
        gripper_dmp.imitate(T, gripper_traj_resampled)
        gripper_dmp.configure(start_y=gripper_traj_resampled[0], goal_y=gripper_traj_resampled[-1])

        if param is not None:
            if param.forcing_term_pos is not None:
                cartesian_dmp.forcing_term_pos.weights_[:, :] = param.forcing_term_pos
            if param.forcing_term_rot is not None:
                cartesian_dmp.forcing_term_rot.weights_[:, :] = param.forcing_term_rot
            if param.goal_pos_diff is not None:
                cartesian_dmp.goal_y[:3] += param.goal_pos_diff
            if param.gripper_forcing_term is not None:
                gripper_dmp.forcing_term.weights_[:, :] = param.gripper_forcing_term

        _, cdmp_trajectory = cartesian_dmp.open_loop()
        _, gdmp_trajectory = gripper_dmp.open_loop()
        dmp_trajectory = np.hstack([cdmp_trajectory, gdmp_trajectory])
        return dmp_trajectory

    def get_dmp_trajectory_pr2(
        self,
        tf_ref_to_base: Optional[RichTrasnform] = None,  # NONE only for debug
        q_whole_init: Optional[np.ndarray] = None,  # NONE only for debug
        *,
        arm: Literal["larm", "rarm"] = "rarm",
        param: Optional[DMPParameter] = None,
        n_sample: int = 40,
    ) -> Tuple[np.ndarray, np.ndarray]:

        if tf_ref_to_base is None:
            tf_ref_to_base = self.tf_ref_to_base  # for debug

        dmp_traj = self.get_dmp_trajectory(param)
        cartesian_traj, gripper_traj = np.split(dmp_traj, [7], axis=1)
        assert isinstance(cartesian_traj, np.ndarray)

        tf_ef_to_base_list: List[RichTrasnform] = []
        for tf_ef_to_ref_arr in cartesian_traj:
            tf_ef_to_ref = RichTrasnform.from_flat_vector(
                tf_ef_to_ref_arr, self.ef_frame, self.ref_frame
            )
            tf_ef_to_base = tf_ef_to_ref * tf_ref_to_base
            tf_ef_to_base_list.append(tf_ef_to_base)

        assert arm in ["larm", "rarm"]
        spec = PR2LarmSpec() if arm == "larm" else PR2RarmSpec()
        pr2 = spec.get_robot_model()  # this value is cached
        if q_whole_init is None:
            q_whole_init = self.q_list[
                0
            ]  # this is for whole body, we need extract only control_joint_names
        pr2.angle_vector(q_whole_init)
        dic = {jname: jangle for jname, jangle in zip(self.joint_names, q_whole_init)}
        q_init = np.array([dic[jname] for jname in spec.control_joint_names])

        spec.reflect_skrobot_model_to_kin(pr2)

        lb, ub = spec.angle_bounds()
        ik_config = IKConfig(ftol=1e-7, acceptable_error=1e-4)
        q_list = []
        for t, tf_ef_to_base in enumerate(tf_ef_to_base_list):
            pos = tf_ef_to_base.translation
            rotmat = tf_ef_to_base.rotation
            quat_xyzw = wxyz2xyzw(matrix2quaternion(rotmat))
            target_vector = np.hstack([pos, quat_xyzw])
            ef_name = self.ef_frame
            cst = spec.create_pose_const([ef_name], [target_vector])
            max_trial = 100 if t == 0 else 1
            ret = solve_ik(cst, None, lb, ub, q_seed=q_init, config=ik_config, max_trial=max_trial)
            assert ret.success
            q_init = ret.q
            set_robot_state(pr2, spec.control_joint_names, ret.q)
            q_whole = pr2.angle_vector()  # ret.q for only control_joint_names, this for all joints
            q_list.append(q_whole)

        q_seq = np.array(q_list)
        # return q_seq, gripper_traj
        return self.resample_sequence(q_seq, n_sample), self.resample_sequence(
            gripper_traj, n_sample
        )
