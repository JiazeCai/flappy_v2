# Helpers
import os, sys
sys.path.append('/Users/mintaekim/Desktop/HRL/Flappy/Integrated/Flappy_Integrated/flappy_v2')
sys.path.append('/Users/mintaekim/Desktop/HRL/Flappy/Integrated/Flappy_Integrated/flappy_v2/envs')
import numpy as np
from typing import Dict, Union
from collections import deque
import matplotlib.pyplot as plt
import pandas as pd
import time
# Gym
import gymnasium as gym
from gymnasium.spaces import Box
from gymnasium import utils
from gymnasium.utils import seeding
# Mujoco
import mujoco as mj
from mujoco_gym.mujoco_env import MujocoEnv
# Flappy
from dynamics import Flappy
from parameter import Simulation_Parameter
from aero_force import aero
from action_filter import ActionFilterButter
from env_randomize import EnvRandomizer
from utility_functions import *
import utility_trajectory as ut
from rotation_transformations import *
from R_body import R_body


DEFAULT_CAMERA_CONFIG = {"trackbodyid": 0, "distance": 12.0}
TRAJECTORY_TYPES = {"linear": 0, "circular": 1, "setpoint": 2}

class FlappyEnv(MujocoEnv, utils.EzPickle):
    metadata = {"render_modes": ["human", "rgb_array", "depth_array"]}
    
    def __init__(
        self,
        max_timesteps = 10000,
        is_visual     = False,
        randomize     = False,
        debug         = False,
        lpf_action    = True,
        traj_type     = False,
        # MujocoEnv
        # xml_file: str = "../assets/Flappy_v8_FixedAxis.xml",
        xml_file: str = "../assets/Flappy_v8_JointInput.xml",
        # xml_file: str = "../assets/Flappy_v8_Base.xml",
        frame_skip: int = 1,
        default_camera_config: Dict[str, Union[float, int]] = DEFAULT_CAMERA_CONFIG,
        reset_noise_scale: float = 0.01,
        **kwargs
    ):
        # Dynamics simulator
        self.p = Simulation_Parameter()
        self.sim = Flappy(p=self.p, render=is_visual)

        # Frequency
        self.max_timesteps         = max_timesteps
        self.timestep: int         = 0
        self.sim_freq              = self.sim.freq # NOTE: 2000Hz for hard coding
        # self.dt                    = 1.0 / self.sim_freq # NOTE: 1/2000s for hard coding
        self.policy_freq: float    = 30.0 # NOTE: 30Hz but the real control frequency might not be exactly 30Hz because we round up the num_sims_per_env_step
        self.num_sims_per_env_step = self.sim_freq // self.policy_freq # 2000//30 = 66
        self.secs_per_env_step     = self.num_sims_per_env_step / self.sim_freq # 66/2000 = 0.033s
        self.policy_freq: int      = int(1.0/self.secs_per_env_step) # 1000/33 = 30Hz

        self.is_visual          = is_visual
        self.randomize          = randomize
        self.debug              = debug
        self.is_plotting_joint  = False
        self.traj_type          = traj_type
        self.noisy              = False
        self.randomize_dynamics = False # True to randomize dynamics
        self.lpf_action         = lpf_action # Low Pass Filter
        self.is_aero            = False

        # Observation, need to be reduce later for smoothness
        self.n_state            = 84 # NOTE: change to the number of states *we can measure*
        self.n_action           = 8  # NOTE: change to the number of action
        self.history_len_short  = 4
        self.history_len_long   = 10
        self.history_len        = self.history_len_short
        self.previous_obs       = deque(maxlen=self.history_len)
        self.previous_act       = deque(maxlen=self.history_len)
        self.last_act_norm      = np.zeros(self.n_action)
        self.action_space       = Box(low=-100, high=100, shape=(self.n_action,))
        self.observation_space  = Box(low=-np.inf, high=np.inf, shape=(13,)) # NOTE: change to the actual number of obs to actor policy

        # NOTE: the low & high does not actually limit the actions output from MLP network, manually clip instead
        self.pos_lb = np.array([-5,-5,0.5]) # fight space dimensions: xyz
        self.pos_ub = np.array([5,5,5])
        self.speed_bound = 10.0

        self.xa = np.zeros(3 * self.p.n_Wagner)

        # MujocoEnv
        self.model = mj.MjModel.from_xml_path(xml_file)
        self.model.opt.timestep = self.dt
        self.data = mj.MjData(self.model)
        self.body_list = ["Base","L1","L2","L3","L4","L5","L6","L7","L1R","L2R","L3R","L4R","L5R","L6R","L7R"]
        self.joint_list = ['J1','J2','J3','J5','J6','J7','J10','J1R','J2R','J3R','J5R','J6R','J7R','J10R']
        self.bodyID_dic, self.jntID_dic, self.posID_dic, self.jvelID_dic = self.get_bodyIDs(self.body_list)
        self.jID_dic = self.get_jntIDs(self.joint_list)
        # Joint Input
        self.Angle_data = pd.read_csv("../assets/JointAngleData.csv", header=None)
        self.J5_m = self.Angle_data.loc[:,0]  # Mujoco reference joint angle (θ_5)
        self.J6_m = self.Angle_data.loc[:,1]  # Mujoco reference joint angle (θ_6)
        self.J5v_m = self.sim.flapping_freq/2 * self.Angle_data.loc[:,2]  # Mujoco reference joint angle velocity (θ_5_dot)
        self.J6v_m = self.sim.flapping_freq/2 * self.Angle_data.loc[:,3]  # Mujoco reference joint angle velocity (θ_6_dot)
        self.t_m = np.linspace(0, 1.0/self.sim.flapping_freq, num=len(self.J5_m))
        # Record Early Stage
        self.SimTime = []
        self.ua_ = []
        self.JointAng = [[],[]]
        self.JointAng_ref = [[],[]]
        self.JointVel = [[],[]]
        self.JointVel_ref = [[],[]]

        utils.EzPickle.__init__(self, xml_file, frame_skip, reset_noise_scale, **kwargs)
        MujocoEnv.__init__(self, xml_file, frame_skip, observation_space=self.observation_space, default_camera_config=default_camera_config, **kwargs)
        
        self.metadata = {
            "render_modes": ["human", "rgb_array", "depth_array"],
            "render_fps": int(np.round(1.0 / self.dt))
        }
        self.observation_structure = {
            "qpos": self.data.qpos.size,
            "qvel": self.data.qvel.size,
        }
        self._reset_noise_scale = reset_noise_scale

        # Info for normalizing the state
        self._init_action_filter()
        # self._init_env_randomizer() # NOTE: Take out dynamics randomization first 
        self._seed()
        self.reset()
        self._init_env()

    @property
    def dt(self):
        # NOTE: For some reason, this does not set model.opt.timestep
        #       To do so, go to xml and set timestep=""
        return 1e-3

    def _init_env(self):
        print("Environment created")
        action = self.action_space.sample()
        print("Sample action: {}".format(action))
        print("Control range: {}".format(self.model.actuator_ctrlrange))
        # print("Actual control range: {}".format(np.vstack([self.action_lower_bounds_actual, self.action_upper_bounds_actual])))
        print("Time step(dt): {}".format(self.dt))
        
    def _init_action_filter(self):
        self.action_filter = ActionFilterButter(
            lowcut        = None,
            highcut       = [4],
            sampling_rate = self.policy_freq,
            order         = 2,
            num_joints    = self.n_action,
        )

    def _seed(self, seed=None):
        self.np_random, _seeds = seeding.np_random(seed)
        return [seed]

    def reset(self, seed=None, randomize=None):
        if randomize is None:
            randomize = self.randomize
        self._reset_env(randomize)
        self.action_filter.reset()
        # self.env_randomizer.randomize_dynamics()
        # self._set_dynamics_properties()
        self._update_data(step=False)
        obs = self._get_obs()
        info = self._get_reset_info
        return obs, info
    
    def _reset_env(self, randomize=False):
        self.timestep    = 0 # discrete timestep, k
        self.time_in_sec = 0.0 # time
        self.reset_model()
        # use action
        self.last_act   = np.zeros(self.n_action)
        self.reward     = None
        self.terminated = None
        self.info       = {}

    def reset_model(self):
        noise_low = -self._reset_noise_scale
        noise_high = self._reset_noise_scale

        qpos = self.init_qpos + self.np_random.uniform(
            size=self.model.nq, low=noise_low, high=noise_high
        )
        qvel = self.init_qvel + self.np_random.uniform(
            size=self.model.nv, low=noise_low, high=noise_high
        )
        self.set_state(qpos, qvel)
        return self._get_obs()

    def _init_env_randomizer(self):
        self.env_randomizer = EnvRandomizer(self.sim)

    def _set_dynamics_properties(self):
        if self.randomize_dynamics:
            self.sim.set_dynamics()

    def _get_obs(self):
        return self.data.sensordata

    def _act_norm2actual(self, act):
        return self.action_lower_bounds_actual + (act + 1)/2.0 * (self.action_upper_bounds_actual - self.action_lower_bounds_actual)

    def step(self, action_normalized, restore=False):
        # assert action_normalized.shape[0] == self.n_action and -1.0 <= action_normalized.all() <= 1.0
        # action = self._act_norm2actual(action_normalized)
        action = action_normalized
        if self.timestep == 0: self.action_filter.init_history(action)
        # post-process action
        if self.lpf_action: action_filtered = self.action_filter.filter(action)
        else: action_filtered = np.copy(action)

        self.do_simulation(action_filtered, self.frame_skip)
        obs = self._get_obs()
        reward, reward_dict = self._get_reward(action_normalized)
        self.info["reward_dict"] = reward_dict

        if self.render_mode == "human": self.render()

        self._update_data(step=True)
        self.last_act_norm = action_normalized
        terminated = self._terminated(obs)
        if terminated and self.timestep < 10: reward -= 10
        truncated = False
        
        # Plot recorded data
        if self.is_plotting_joint and self.timestep == 1000:
            self._plot_joint()
        
        return obs, reward, terminated, truncated, self.info
    
    def do_simulation(self, ctrl, n_frames) -> None:
        if np.array(ctrl).shape != (self.model.nu,):
            raise ValueError(f"Action dimension mismatch. Expected {(self.model.nu,)}, found {np.array(ctrl).shape}")
        self._step_mujoco_simulation(ctrl, n_frames)

    def _step_mujoco_simulation(self, ctrl, n_frames):
        if self.timestep < 100:
            ctrl = self._launch_control(ctrl)
        self._apply_control(ctrl=ctrl)
        mj.mj_step(self.model, self.data, nstep=n_frames)

    def _launch_control(self, ctrl):
        ctrl[2:6] = np.array([0.6, 0.6, 0.6, 0.6])
        ctrl[6:] = np.array([0.0, 0.0])
        return ctrl

    def _apply_control(self, ctrl):
        self.data.actuator("Motor1").ctrl[0] = ctrl[2]  # data.ctrl[1] # front
        self.data.actuator("Motor2").ctrl[0] = ctrl[3]  # data.ctrl[2] # back
        self.data.actuator("Motor3").ctrl[0] = ctrl[4]  # data.ctrl[3] # left
        self.data.actuator("Motor4").ctrl[0] = ctrl[5]  # data.ctrl[4] # right
        self.data.actuator("Motor5").ctrl[0] = ctrl[6]  # data.ctrl[5] # left H
        self.data.actuator("Motor6").ctrl[0] = ctrl[7]  # data.ctrl[6] # right H

        # NOTE: If Using Custom Aero
        if self.is_aero:
            self.xd, R_body = self._get_original_states()
            fa, ua, self.xd = aero(self.model, self.data, self.xa, self.xd, R_body)
            # Apply Aero forces
            self.data.qfrc_applied[self.jvelID_dic["L3"]] = ua[0]
            self.data.qfrc_applied[self.jvelID_dic["L7"]] = ua[1]
            self.data.xfrc_applied[self.bodyID_dic["Base"]] = [*ua[2:5], *ua[5:8]]
            # Integrate Aero States
            self.xa = self.xa + fa * self.dt

        # Joint Input Data
        _J5 = np.interp(np.round(self.data.time,3), self.t_m, self.J5_m, period=1.0/self.sim.flapping_freq)
        _J6 = np.interp(np.round(self.data.time,3), self.t_m, self.J6_m, period=1.0/self.sim.flapping_freq)
        J5_d = _J5 - np.deg2rad(11.345825599281223)  # convert to Mujoco Reference
        J6_d = _J6 + np.deg2rad(27.45260202) - _J5
        # Apply angles to Joints
        self.data.actuator("J5_angle").ctrl[0] = J5_d
        self.data.actuator("J6_angle").ctrl[0] = J6_d
        # NOTE: Record joint angles
        self._record_joint(_J5=_J5, _J6=_J6)

    def _record_joint(self, _J5, _J6):
        J5 = self.data.qpos[self.posID_dic["L3"]] + np.deg2rad(11.345825599281223) # Get angle from mujoco
        J6 = self.data.qpos[self.posID_dic["L7"]] - np.deg2rad(27.45260202) + J5
        self.SimTime.append(np.round((self.timestep + 1) * self.dt, 3))
        self.JointAng_ref[0].append(_J5)
        self.JointAng_ref[1].append(_J6)
        J5v_d = np.interp(self.data.time, self.t_m, self.J5v_m, period=1.0 / self.sim.flapping_freq) # Get velocity just for comparison
        J6v_d = np.interp(self.data.time, self.t_m, self.J6v_m, period=1.0 / self.sim.flapping_freq)
        self.JointVel_ref[0].append(J5v_d)
        self.JointVel_ref[1].append(J6v_d)
        self.JointAng[0].append(J5)
        self.JointAng[1].append(J6)
        self.JointVel[0].append(self.data.qvel[self.jvelID_dic["L3"]])
        self.JointVel[1].append(self.data.qvel[self.jvelID_dic["L7"]])

    def _plot_joint(self):
        plt.figure()
        plt.subplot(2,1,1)
        plt.title("Joint Angle (MuJoCo vs. Matlab)")
        plt.plot(self.SimTime[100:], np.rad2deg(self.JointAng_ref[0][100:]), 'b--', label='J5 ref')
        plt.plot(self.SimTime[100:], np.rad2deg(self.JointAng[0][100:]), 'b', label='J5 real')
        plt.xlabel('Time, t (s)')
        plt.ylabel('J5 Angle, θ_5 (deg)')
        plt.legend()

        plt.subplot(2,1,2)
        plt.plot(self.SimTime[100:], np.rad2deg(self.JointAng_ref[1][100:]), 'g--', label='J6 ref')
        plt.plot(self.SimTime[100:], np.rad2deg(self.JointAng[1][100:]), 'g', label='J6 real')
        plt.xlabel('Time, t (s)')
        plt.ylabel('J6 Angle, θ_6 (deg)')
        plt.legend()

        plt.figure()
        plt.subplot(2,1,1)
        plt.title("Joint Angle Velocity (MuJoCo vs. Matlab)")
        plt.plot(self.SimTime[100:], np.rad2deg(self.JointVel_ref[0][100:]), 'b--', label='J5 ref')
        plt.plot(self.SimTime[100:], np.rad2deg(self.JointVel[0][100:]), 'b', label='J5 real')
        plt.xlabel('Time, t (s)')
        plt.ylabel('J5 Speed, θ_5 (deg/s)')
        # plt.ylim([-2000,2000])
        plt.legend()

        plt.subplot(2,1,2)
        plt.plot(self.SimTime[100:], np.rad2deg(self.JointVel_ref[1][100:]), 'g--', label='J6 ref')
        plt.plot(self.SimTime[100:], np.rad2deg(self.JointVel[1][100:]), 'g', label='J6 real')
        plt.xlabel('Time, t (s)')
        plt.ylabel('J6 Speed, θ_6 (deg/s)')
        # plt.ylim([-3000,3000])
        plt.legend()
        plt.show()

    # NOTE: For aero()
    def _get_original_states(self):
        xd = np.array([0.0] * 22)
        xd[0] = self.data.qpos[self.posID_dic["L3"]] + np.deg2rad(11.345825599281223)  # xd[0:1] left wing angles [shoulder, elbow]
        xd[1] = self.data.qpos[self.posID_dic["L7"]] - np.deg2rad(27.45260202) + xd[0]
        xd[2:5] = self.data.sensordata[0:3]  # inertial position (x, y, z)
        xd[5] = self.data.qvel[self.jvelID_dic["L3"]]  # Joint 5 velocity
        xd[6] = self.data.qvel[self.jvelID_dic["L7"]]  # Joint 6 velocity
        xd[7:10] = self.data.sensordata[7:10]  # Inertial Frame Linear Velocity
        xd[10:13] = self.data.sensordata[10:13]  # Body Frame Angular velocity

        if np.linalg.norm(self.data.sensordata[3:7]) == 0:
            self.data.sensordata[3:7] = [1,0,0,0]
        R_body = quat2rot(self.data.sensordata[3:7])
        xd[13:23] = list(np.transpose(R_body).flatten())
        return xd, R_body

    def _update_data(self, step=True):
        # NOTE: Need to be modifid to obs states and ground truth states 
        self.obs_states = self._get_obs()
        self.gt_states = self._get_obs()
        if step:
            self.timestep += 1
            self.time_in_sec += self.secs_per_env_step
            # self.time_in_sec = self.sim.time
            # self.reference_generator.update_ref_env(self.time_in_sec)

    def _get_reward(self, action_normalized):
        names = ['position_error', 'velocity_error', 'angular_velocity', 'orientation_error', 'input', 'delta_acs']

        w_position         = 5.0
        w_velocity         = 1.0
        w_angular_velocity = 5.0
        w_orientation      = 10.0
        w_input            = 5.0
        w_delta_act        = 0.1

        reward_weights = np.array([w_position, w_velocity, w_angular_velocity, w_orientation, w_input, w_delta_act])
        weights = reward_weights / np.sum(reward_weights)  # weight can be adjusted later

        scale_pos       = 1.0
        scale_vel       = 1.0
        scale_ang_vel   = 1.0
        scale_ori       = 1.0
        scale_input     = 1.0 # action already normalized
        scale_delta_act = 1.0

        desired_pos_norm     = np.array([0.0, 0.0, 2.0]).reshape(3,1)/5 # x y z 
        desired_vel_norm     = np.array([0.0, 0.0, 0.0]).reshape(3,1)/self.speed_bound # vx vy vz
        desired_ang_vel_norm = np.array([0.0, 0.0, 0.0]).reshape(3,1)/10 # \omega_x \omega_y \omega_z
        desired_ori_norm     = np.array([0.0, 0.0, 0.0]).reshape(3,1)/np.pi # roll, pitch, yaw
        
        obs = self._get_obs()
        current_pos_norm     = obs[0:3]/5 # [-5,5] -> [-1,1]
        current_vel_norm     = obs[7:10]/self.speed_bound # [-5,5] -> [-1,1]
        current_ang_vel_norm = obs[10:13]/10 # [-10,10] -> [-1,1]
        current_ori_norm     = quat2euler_raw(obs[3:7])/np.pi

        pos_err       = np.linalg.norm(current_pos_norm - desired_pos_norm) # [0,1]
        vel_err       = np.linalg.norm(current_vel_norm - desired_vel_norm) # [0,1]
        ang_vel_err   = np.linalg.norm(current_ang_vel_norm - desired_ang_vel_norm) # [0,1]
        ori_err       = np.linalg.norm(current_ori_norm - desired_ori_norm) # [0,1]
        input_err     = np.linalg.norm(action_normalized) # It's not an error but let's just call it
        delta_act_err = np.linalg.norm(action_normalized - self.last_act_norm) # It's not an error but let's just call it

        rewards = np.exp(-np.array([scale_pos, scale_vel, scale_ang_vel, scale_ori, scale_input, scale_delta_act]
                         * np.array([pos_err, vel_err, ang_vel_err, ori_err, input_err, delta_act_err])))
        total_reward = np.sum(weights * rewards)
        reward_dict = dict(zip(names, weights * rewards))

        return total_reward, reward_dict

    def _terminated(self, obs):
        if not((obs[0:3] <= self.pos_ub).all() 
           and (obs[0:3] >= self.pos_lb).all()):
            print("Out of position bounds: {pos}  |  Timestep: {timestep}  |  Time: {time}s".format(pos=np.round(obs[0:3],2), timestep=self.timestep, time=round(self.timestep*self.dt,2)))
            return True
        if not(np.linalg.norm(obs[7:10]) <= self.speed_bound):
            print("Out of speed bounds: {vel}  |  Timestep: {timestep}  |  Time: {time}s".format(vel=np.round(obs[7:10],2), timestep=self.timestep, time=round(self.timestep*self.dt,2)))
            return True
        if self.timestep >= self.max_timesteps:
            print("Max step reached: Timestep: {timestep}  |  Time: {time}s".format(timestep=self.max_timesteps, time=round(self.timestep*self.dt,2)))
            return True
        else:
            return False

    def get_bodyIDs(self, body_list):
        bodyID_dic = {}
        jntID_dic = {}
        posID_dic = {}
        jvelID_dic = {}
        for bodyName in body_list:
            mjID = mj.mj_name2id(self.model, mj.mjtObj.mjOBJ_BODY, bodyName)
            jntID = self.model.body_jntadr[mjID]   # joint ID
            jvelID = self.model.body_dofadr[mjID]  # joint velocity
            posID = self.model.jnt_qposadr[jntID]  # joint position
            bodyID_dic[bodyName] = mjID
            jntID_dic[bodyName] = jntID
            posID_dic[bodyName] = posID
            jvelID_dic[bodyName] = jvelID
        return bodyID_dic, jntID_dic, posID_dic, jvelID_dic

    def get_jntIDs(self, jnt_list):
        jointID_dic = {}
        for jointName in jnt_list:
            jointID = mj.mj_name2id(self.model, mj.mjtObj.mjOBJ_JOINT, jointName)
            jointID_dic[jointName] = jointID
        return jointID_dic

    def close(self):
        if self.mujoco_renderer is not None:
            self.mujoco_renderer.close()