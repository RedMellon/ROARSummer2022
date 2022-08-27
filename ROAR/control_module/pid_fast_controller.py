from operator import truediv
from matplotlib.pyplot import close
from pydantic import BaseModel, Field
from ROAR.control_module.controller import Controller
from ROAR.utilities_module.vehicle_models import VehicleControl, Vehicle

from ROAR.utilities_module.data_structures_models import Transform, Location
from collections import deque
import numpy as np
import math
import logging
from ROAR.agent_module.agent import Agent
from typing import Tuple
import json
from pathlib import Path

class PIDFastController(Controller):
    def __init__(self, agent, steering_boundary: Tuple[float, float],
                 throttle_boundary: Tuple[float, float], **kwargs):
        super().__init__(agent, **kwargs)
        self.max_speed = self.agent.agent_settings.max_speed
        throttle_boundary = throttle_boundary
        self.steering_boundary = steering_boundary
        
        self.agent = agent
        self.config = json.load(Path(agent.agent_settings.pid_config_file_path).open(mode='r'))
        
        # useful variables
        self.old_pitch = 0
        self.delta_pitch = 0
        self.pitch_bypass = False
        self.force_brake = False
        self.cur_region = "town"

        self.lat_pid_controller = LatPIDController(
            agent=agent,
            config=self.config["latitudinal_controller"],
            steering_boundary=steering_boundary
        )
        self.logger = logging.getLogger(__name__)

    def run_in_series(self, next_waypoint: Transform, close_waypoint: Transform, far_waypoint: Transform, region: str, **kwargs) -> VehicleControl:

        # run lat pid controller
        steering, error, wide_error, sharp_error = self.lat_pid_controller.run_in_series(next_waypoint=next_waypoint, close_waypoint=close_waypoint, far_waypoint=far_waypoint)
        
        # set region
        if region != "":
            self.cur_region = region
            # print("Switched to: " + region)
            if region == "hills":
                self.config = json.load(Path(self.agent.agent_settings.pid_config_file_path_hills).open(mode='r'))
                self.lat_pid_controller.update_pid_config(self.config)
            if region == "town2":
                self.config = json.load(Path(self.agent.agent_settings.pid_config_file_path).open(mode='r'))
                self.lat_pid_controller.update_pid_config(self.config)
        
        current_speed = Vehicle.get_speed(self.agent.vehicle)
        
        # run region-specific controller
        if self.cur_region == "town":
            
            error = abs(round(error, 3))
            wide_error = abs(round(wide_error, 3))
            sharp_error = abs(round(sharp_error, 3))
            #print(sharp_error)
            brake_exception = float(next_waypoint.record().split(",")[5]) == 0.987654321

            if sharp_error > 0.6 and current_speed > 120: # narrow turn
                throttle = -1
                brake = 1
            else:
                throttle = 1
                brake = 0
        elif self.cur_region == "town2":
            
            error = abs(round(error, 3))
            wide_error = abs(round(wide_error, 3))
            sharp_error = abs(round(sharp_error, 3))
            #print(sharp_error)
            brake_exception = float(next_waypoint.record().split(",")[5]) == 0.987654321
            
            if brake_exception and current_speed > 70: # force brake
                throttle = -1
                brake = 1
                # print("force brake")
                self.brake_on = True
            elif sharp_error > 0.6 and current_speed > 80: # narrow turn
                throttle = -1
                brake = 1
            else:
                throttle = 1
                brake = 0
        
        elif self.cur_region == "hills":
            
            # get errors from lat pid
            error = abs(round(error, 3))
            wide_error = abs(round(wide_error, 3))
            sharp_error = abs(round(sharp_error, 3))
            # print(error, wide_error, sharp_error)

            # check for brake_exceptions
            brake_exception = float(next_waypoint.record().split(",")[5]) == 0.987654321
            # print(next_waypoint.record())

            if brake_exception and current_speed > 70: # force brake
                throttle = -1
                brake = 1
                # print("force brake")
            elif wide_error >= 0.1 and current_speed >= 80:
                danger = (pow(sharp_error, 1.4) * current_speed) - 0.80
                # print(-0.001 * pow(danger, 3) + 1)
                throttle = max(0.3, -0.04 * pow(danger, 2) + 1)
                brake = 0
            else:
                throttle = 1
                brake = 0
        
        return VehicleControl(throttle=throttle, steering=steering, brake=brake)

    @staticmethod
    def find_k_values(vehicle: Vehicle, config: dict) -> np.array:
        current_speed = Vehicle.get_speed(vehicle=vehicle)
        k_p, k_d, k_i = 1, 0, 0
        for speed_upper_bound, kvalues in config.items():
            speed_upper_bound = float(speed_upper_bound)
            if current_speed < speed_upper_bound:
                k_p, k_d, k_i = kvalues["Kp"], kvalues["Kd"], kvalues["Ki"]
                break
        return np.array([k_p, k_d, k_i])

class LatPIDController(Controller):
    def __init__(self, agent, config: dict, steering_boundary: Tuple[float, float],
                 dt: float = 0.03, **kwargs):
        super().__init__(agent, **kwargs)
        self.config = config
        self.steering_boundary = steering_boundary
        self._error_buffer = deque(maxlen=10)
        self._dt = dt

    def update_pid_config(self, updated_config):
        self.config = updated_config["latitudinal_controller"]

    def run_in_series(self, next_waypoint: Transform, close_waypoint: Transform, far_waypoint: Transform, **kwargs) -> float:
        """
        Calculates a vector that represent where you are going.
        Args:
            next_waypoint ():
            **kwargs ():

        Returns:
            lat_control
        """
        # calculate a vector that represent where you are going
        v_begin = self.agent.vehicle.transform.location.to_array()
        direction_vector = np.array([-np.sin(np.deg2rad(self.agent.vehicle.transform.rotation.yaw)),
                                     0,
                                     -np.cos(np.deg2rad(self.agent.vehicle.transform.rotation.yaw))])
        v_end = v_begin + direction_vector

        v_vec = np.array([(v_end[0] - v_begin[0]), 0, (v_end[2] - v_begin[2])])
        
        # calculate error projection
        w_vec = np.array(
            [
                next_waypoint.location.x - v_begin[0],
                0,
                next_waypoint.location.z - v_begin[2],
            ]
        )

        v_vec_normed = v_vec / np.linalg.norm(v_vec)
        w_vec_normed = w_vec / np.linalg.norm(w_vec)
        #error = np.arccos(v_vec_normed @ w_vec_normed.T)
        error = np.arccos(min(max(v_vec_normed @ w_vec_normed.T, -1), 1)) # makes sure arccos input is between -1 and 1, inclusive
        _cross = np.cross(v_vec_normed, w_vec_normed)

        # calculate close error projection
        w_vec = np.array(
            [
                close_waypoint.location.x - v_begin[0],
                0,
                close_waypoint.location.z - v_begin[2],
            ]
        )
        w_vec_normed = w_vec / np.linalg.norm(w_vec)
        #wide_error = np.arccos(v_vec_normed @ w_vec_normed.T)
        wide_error = np.arccos(min(max(v_vec_normed @ w_vec_normed.T, -1), 1)) # makes sure arccos input is between -1 and 1, inclusive

        # calculate far error projection
        w_vec = np.array(
            [
                far_waypoint.location.x - v_begin[0],
                0,
                far_waypoint.location.z - v_begin[2],
            ]
        )
        w_vec_normed = w_vec / np.linalg.norm(w_vec)
        #sharp_error = np.arccos(v_vec_normed @ w_vec_normed.T)
        sharp_error = np.arccos(min(max(v_vec_normed @ w_vec_normed.T, -1), 1)) # makes sure arccos input is between -1 and 1, inclusive

        if _cross[1] > 0:
            error *= -1
        self._error_buffer.append(error)
        if len(self._error_buffer) >= 2:
            _de = (self._error_buffer[-1] - self._error_buffer[-2]) / self._dt
            _ie = sum(self._error_buffer) * self._dt
        else:
            _de = 0.0
            _ie = 0.0

        k_p, k_d, k_i = PIDFastController.find_k_values(config=self.config, vehicle=self.agent.vehicle)

        lat_control = float(
            np.clip((k_p * error) + (k_d * _de) + (k_i * _ie), self.steering_boundary[0], self.steering_boundary[1])
        )
        return lat_control, error, wide_error, sharp_error
