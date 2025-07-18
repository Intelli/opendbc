import math
import numpy as np
from opendbc.car.carlog import carlog
from opendbc.car.vehicle_model import VehicleModel

try:
  # TODO-SP: We shouldn't really import params from here, but it's the easiest way to get the params for
  #  live tuning temporarily while we understand the angle steering better
  from openpilot.common.params import Params
  PARAMS_AVAILABLE = True
except ImportError:
  carlog.warning("Unable to import Params from openpilot.common.params.")
  PARAMS_AVAILABLE = False

from opendbc.can.packer import CANPacker
from opendbc.car import ACCELERATION_DUE_TO_GRAVITY, Bus, DT_CTRL, apply_driver_steer_torque_limits, common_fault_avoidance, \
  make_tester_present_msg, structs, rate_limit
from opendbc.car.common.conversions import Conversions as CV
from opendbc.car.hyundai import hyundaicanfd, hyundaican
from opendbc.car.hyundai.hyundaicanfd import CanBus
from opendbc.car.hyundai.values import HyundaiFlags, Buttons, CarControllerParams, CAR
from opendbc.car.interfaces import CarControllerBase, ISO_LATERAL_ACCEL

from opendbc.sunnypilot.car.hyundai.escc import EsccCarController
from opendbc.sunnypilot.car.hyundai.interceptors.adas_drv_interceptor import AdasDrvEcuInterceptorCarController
from opendbc.sunnypilot.car.hyundai.longitudinal.controller import LongitudinalController
from opendbc.sunnypilot.car.hyundai.mads import MadsCarController

VisualAlert = structs.CarControl.HUDControl.VisualAlert
LongCtrlState = structs.CarControl.Actuators.LongControlState

# EPS faults if you apply torque while the steering angle is above 90 degrees for more than 1 second
# All slightly below EPS thresholds to avoid fault
MAX_ANGLE = 85
MAX_ANGLE_FRAMES = 89
MAX_ANGLE_CONSECUTIVE_FRAMES = 2

MAX_ANGLE_RATE = 5
# Add extra tolerance for average banked road since safety doesn't have the roll
AVERAGE_ROAD_ROLL = 0.06  # ~3.4 degrees, 6% superelevation. higher actual roll lowers lateral acceleration
MAX_LATERAL_ACCEL = ISO_LATERAL_ACCEL + (ACCELERATION_DUE_TO_GRAVITY * AVERAGE_ROAD_ROLL)  # ~3.6 m/s^2
MAX_LATERAL_JERK = 3.0 + (ACCELERATION_DUE_TO_GRAVITY * AVERAGE_ROAD_ROLL)  # ~3.6 m/s^3

# Until we can do a better fingerprinting from Panda, we need to use a baseline model for safety
#  to ensure we have common safety limits for all the angle steering. Even if the limits are not optimal for all models.
ANGLE_SAFETY_BASELINE_MODEL = "GENESIS_GV80_2025"


def get_safety_CP():
  # We use the ANGLE_SAFETY_BASELINE_MODEL platform for lateral limiting to match safety
  # The goal is to pick the least-permissive model for angle steering, to have a common denominator for safety.
  from opendbc.car.tesla.interface import CarInterface
  return CarInterface.get_non_essential_params(ANGLE_SAFETY_BASELINE_MODEL)

def get_max_angle_delta(v_ego_raw: float, VM: VehicleModel, freq=100.):
  max_curvature_rate_sec = MAX_LATERAL_JERK / (v_ego_raw ** 2)  # (1/m)/s
  max_angle_rate_sec = math.degrees(VM.get_steer_from_curvature(max_curvature_rate_sec, v_ego_raw, 0))  # deg/s
  return max_angle_rate_sec / float(freq) # hz

def get_max_angle(v_ego_raw: float, VM: VehicleModel):
  max_curvature = MAX_LATERAL_ACCEL / (v_ego_raw ** 2)  # 1/m
  return math.degrees(VM.get_steer_from_curvature(max_curvature, v_ego_raw, 0))  # deg

def sp_smooth_angle(v_ego_raw: float, apply_angle: float, apply_angle_last: float) -> float:
  """
  Smooth the steering angle change based on vehicle speed and an optional smoothing offset.

  This function helps prevent abrupt steering changes by blending the new desired angle (`apply_angle`)
  with the previously applied angle (`apply_angle_last`). The blend factor (alpha) is dynamically calculated
  based on the vehicle's current speed using a predefined lookup table.

  Behavior:
    - At low speeds, the smoothing is strong, keeping the steering more stable.
    - At higher speeds, the smoothing is relaxed, allowing quicker responses.
    - If the angle change is negligible (≤ 0.1 deg), smoothing is skipped for responsiveness.

  Parameters:
    v_ego_raw (float): Raw vehicle speed in m/s.
    apply_angle (float): New target steering angle in degrees.
    apply_angle_last (float): Previously applied steering angle in degrees.

  Returns:
    float: Smoothed steering angle.
  """
  if abs(apply_angle - apply_angle_last) > 0.1:
    adjusted_alpha = np.interp(v_ego_raw, CarControllerParams.SMOOTHING_ANGLE_VEGO_MATRIX, CarControllerParams.SMOOTHING_ANGLE_ALPHA_MATRIX)
    adjusted_alpha_limited = float(min(float(adjusted_alpha), 1.))  # Limit the smoothing factor to 1 if adjusted_alpha is greater than 1
    return (apply_angle * adjusted_alpha_limited) + (apply_angle_last * (1 - adjusted_alpha_limited))
  return apply_angle

def process_hud_alert(enabled, fingerprint, hud_control):
  sys_warning = (hud_control.visualAlert in (VisualAlert.steerRequired, VisualAlert.ldw))

  # initialize to no line visible
  # TODO: this is not accurate for all cars
  sys_state = 1
  if hud_control.leftLaneVisible and hud_control.rightLaneVisible or sys_warning:  # HUD alert only display when LKAS status is active
    sys_state = 3 if enabled or sys_warning else 4
  elif hud_control.leftLaneVisible:
    sys_state = 5
  elif hud_control.rightLaneVisible:
    sys_state = 6

  # initialize to no warnings
  left_lane_warning = 0
  right_lane_warning = 0
  if hud_control.leftLaneDepart:
    left_lane_warning = 1 if fingerprint in (CAR.GENESIS_G90, CAR.GENESIS_G80) else 2
  if hud_control.rightLaneDepart:
    right_lane_warning = 1 if fingerprint in (CAR.GENESIS_G90, CAR.GENESIS_G80) else 2

  return sys_warning, sys_state, left_lane_warning, right_lane_warning

def parse_tq_rdc_gain(val):
  """
  Returns the float value divided by 100 if val is not None, else returns None.
  """
  if val is not None:
    return float(val) / 100
  return None

class CarController(CarControllerBase, EsccCarController, LongitudinalController, AdasDrvEcuInterceptorCarController, MadsCarController):
  def __init__(self, dbc_names, CP, CP_SP):
    CarControllerBase.__init__(self, dbc_names, CP, CP_SP)
    EsccCarController.__init__(self, CP, CP_SP)
    AdasDrvEcuInterceptorCarController.__init__(self, CP, CP_SP)
    MadsCarController.__init__(self)
    LongitudinalController.__init__(self, CP, CP_SP)
    self.CAN = CanBus(CP)
    self.params = CarControllerParams(CP)
    self.packer = CANPacker(dbc_names[Bus.pt])
    self.angle_limit_counter = 0

    # Vehicle model used for lateral limiting
    self.safety_cp = get_safety_CP()
    self.VM = VehicleModel(self.safety_cp)

    self.accel_last = 0
    self.apply_torque_last = 0
    self.car_fingerprint = CP.carFingerprint
    self.last_button_frame = 0

    self.apply_angle_last = 0
    self.angle_torque_reduction_gain = 0

    # For future parametrization / tuning
    self.ramp_down_reduction_gain_rate = self.params.ANGLE_RAMP_DOWN_TORQUE_REDUCTION_RATE
    self.ramp_up_reduction_gain_rate = self.params.ANGLE_RAMP_UP_TORQUE_REDUCTION_RATE
    self.min_torque_reduction_gain = self.params.ANGLE_MIN_TORQUE_REDUCTION_GAIN
    self.max_torque_reduction_gain = self.params.ANGLE_MAX_TORQUE_REDUCTION_GAIN
    self.active_torque_reduction_gain = self.params.ANGLE_ACTIVE_TORQUE_REDUCTION_GAIN
    self.angle_torque_override_cycles = self.params.ANGLE_TORQUE_OVERRIDE_CYCLES
    self.angle_enable_smoothing_factor = True

    self._params = Params() if PARAMS_AVAILABLE else None
    if PARAMS_AVAILABLE:
      self.min_torque_reduction_gain = parse_tq_rdc_gain(self._params.get("HkgTuningAngleMinTorqueReductionGain")) or self.min_torque_reduction_gain
      self.max_torque_reduction_gain = parse_tq_rdc_gain(self._params.get("HkgTuningAngleMaxTorqueReductionGain")) or self.max_torque_reduction_gain
      self.active_torque_reduction_gain = parse_tq_rdc_gain(self._params.get("HkgTuningAngleActiveTorqueReductionGain")) or self.active_torque_reduction_gain
      self.angle_torque_override_cycles = int(self._params.get("HkgTuningOverridingCycles") or self.angle_torque_override_cycles)
      self.angle_enable_smoothing_factor = self._params.get_bool("EnableHkgTuningAngleSmoothingFactor")


  def update(self, CC, CC_SP, CS, now_nanos):
    EsccCarController.update(self, CS)
    AdasDrvEcuInterceptorCarController.update(self, CS)
    MadsCarController.update(self, self.CP, CC, CC_SP, self.frame)
    if self.frame % 2 == 0:
      LongitudinalController.update(self, CC, CS)

    actuators = CC.actuators
    hud_control = CC.hudControl

    # steering torque
    if not self.CP.flags & HyundaiFlags.CANFD_ANGLE_STEERING:
      self.angle_limit_counter, apply_steer_req = common_fault_avoidance(abs(CS.out.steeringAngleDeg) >= MAX_ANGLE, CC.latActive,
                                                                         self.angle_limit_counter, MAX_ANGLE_FRAMES,
                                                                         MAX_ANGLE_CONSECUTIVE_FRAMES)
      new_torque = int(round(actuators.torque * self.params.STEER_MAX))
      apply_torque = apply_driver_steer_torque_limits(new_torque, self.apply_torque_last, CS.out.steeringTorque, self.params)

    # angle control
    else:
      self.apply_angle_last, apply_torque = self.update_angle_steering_control(CS, CC,actuators)
      # Safety clamp
      apply_torque = float(np.clip(apply_torque, self.min_torque_reduction_gain, self.max_torque_reduction_gain))
      apply_steer_req = CC.latActive and apply_torque != 0

    if not CC.latActive:
      apply_torque = 0

    # Hold torque with induced temporary fault when cutting the actuation bit
    # FIXME: we don't use this with CAN FD?
    torque_fault = CC.latActive and not apply_steer_req

    self.apply_torque_last = apply_torque

    # accel + longitudinal
    accel = float(np.clip(actuators.accel, CarControllerParams.ACCEL_MIN, CarControllerParams.ACCEL_MAX))
    stopping = actuators.longControlState == LongCtrlState.stopping
    set_speed_in_units = hud_control.setSpeed * (CV.MS_TO_KPH if CS.is_metric else CV.MS_TO_MPH)

    can_sends = []

    # *** common hyundai stuff ***

    # tester present - w/ no response (keeps relevant ECU disabled)
    if self.frame % 100 == 0 and not ((self.CP.flags & HyundaiFlags.CANFD_CAMERA_SCC) or self.ESCC.enabled or self.interceptor.available) and \
            self.CP.openpilotLongitudinalControl:
      # for longitudinal control, either radar or ADAS driving ECU
      addr, bus = 0x7d0, self.CAN.ECAN if self.CP.flags & HyundaiFlags.CANFD else 0
      if self.CP.flags & HyundaiFlags.CANFD_LKA_STEERING.value:
        addr, bus = 0x730, self.CAN.ECAN
      can_sends.append(make_tester_present_msg(addr, bus, suppress_response=True))

      # for blinkers
      if self.CP.flags & HyundaiFlags.ENABLE_BLINKERS:
        can_sends.append(make_tester_present_msg(0x7b1, self.CAN.ECAN, suppress_response=True))

    # *** CAN/CAN FD specific ***
    if self.CP.flags & HyundaiFlags.CANFD:
      can_sends.extend(self.create_canfd_msgs(apply_steer_req, apply_torque, set_speed_in_units, accel,
                                              stopping, hud_control, CS, CC))
    else:
      can_sends.extend(self.create_can_msgs(apply_steer_req, apply_torque, torque_fault, set_speed_in_units, accel,
                                            stopping, hud_control, actuators, CS, CC))

    new_actuators = actuators.as_builder()
    new_actuators.torque = apply_torque / self.params.STEER_MAX
    new_actuators.torqueOutputCan = apply_torque
    new_actuators.steeringAngleDeg = self.apply_angle_last
    new_actuators.accel = self.tuning.actual_accel

    self.frame += 1
    return new_actuators, can_sends

  def create_can_msgs(self, apply_steer_req, apply_torque, torque_fault, set_speed_in_units, accel, stopping, hud_control, actuators, CS, CC):
    can_sends = []

    # HUD messages
    sys_warning, sys_state, left_lane_warning, right_lane_warning = process_hud_alert(CC.enabled, self.car_fingerprint,
                                                                                      hud_control)

    can_sends.append(hyundaican.create_lkas11(self.packer, self.frame, self.CP, apply_torque, apply_steer_req,
                                              torque_fault, CS.lkas11, sys_warning, sys_state, CC.enabled,
                                              hud_control.leftLaneVisible, hud_control.rightLaneVisible,
                                              left_lane_warning, right_lane_warning,
                                              self.lkas_icon))

    # Button messages
    if not self.CP.openpilotLongitudinalControl:
      if CC.cruiseControl.cancel:
        can_sends.append(hyundaican.create_clu11(self.packer, self.frame, CS.clu11, Buttons.CANCEL, self.CP))
      elif CC.cruiseControl.resume:
        # send resume at a max freq of 10Hz
        if (self.frame - self.last_button_frame) * DT_CTRL > 0.1:
          # send 25 messages at a time to increases the likelihood of resume being accepted
          can_sends.extend([hyundaican.create_clu11(self.packer, self.frame, CS.clu11, Buttons.RES_ACCEL, self.CP)] * 25)
          if (self.frame - self.last_button_frame) * DT_CTRL >= 0.15:
            self.last_button_frame = self.frame

    if self.frame % 2 == 0 and self.CP.openpilotLongitudinalControl:
      # TODO: unclear if this is needed
      jerk = 3.0 if actuators.longControlState == LongCtrlState.pid else 1.0
      use_fca = self.CP.flags & HyundaiFlags.USE_FCA.value
      can_sends.extend(hyundaican.create_acc_commands(self.packer, CC.enabled, accel, jerk, int(self.frame / 2),
                                                      hud_control, set_speed_in_units, stopping,
                                                      CC.cruiseControl.override, use_fca, self.CP,
                                                      CS.main_cruise_enabled, self.tuning, self.ESCC))

    # 20 Hz LFA MFA message
    if self.frame % 5 == 0 and self.CP.flags & HyundaiFlags.SEND_LFA.value:
      can_sends.append(hyundaican.create_lfahda_mfc(self.packer, CC.enabled, self.lfa_icon))

    # 5 Hz ACC options
    if self.frame % 20 == 0 and self.CP.openpilotLongitudinalControl:
      can_sends.extend(hyundaican.create_acc_opt(self.packer, self.CP, self.ESCC))

    # 2 Hz front radar options
    if self.frame % 50 == 0 and self.CP.openpilotLongitudinalControl and not self.ESCC.enabled:
      can_sends.append(hyundaican.create_frt_radar_opt(self.packer))

    return can_sends

  def create_canfd_msgs(self, apply_steer_req, apply_torque, set_speed_in_units, accel, stopping, hud_control, CS, CC):
    can_sends = []

    lka_steering = self.CP.flags & HyundaiFlags.CANFD_LKA_STEERING
    lka_steering_long = lka_steering and self.CP.openpilotLongitudinalControl

    if self.interceptor.available:  # ADAS_DRV_INTERCEPT
      can_sends.extend(self.interceptor.create_adas_drv_intercept_msg(self.packer, self.CAN, self.CP.openpilotLongitudinalControl))

    # steering control
    can_sends.extend(hyundaicanfd.create_steering_messages(self.packer, self.CP, self.CAN, CC.enabled, apply_steer_req, apply_torque, self.apply_angle_last
                                                           , self.lkas_icon))

    # prevent LFA from activating on LKA steering cars by sending "no lane lines detected" to ADAS ECU
    if self.frame % 5 == 0 and lka_steering:
      can_sends.append(hyundaicanfd.create_suppress_lfa(self.packer, self.CAN, CS.lfa_block_msg,
                                                        self.CP.flags & HyundaiFlags.CANFD_LKA_STEERING_ALT))

    # LFA and HDA icons
    if self.frame % 5 == 0 and (not lka_steering or lka_steering_long):
      can_sends.append(hyundaicanfd.create_lfahda_cluster(self.packer, self.CAN, CC.enabled, self.lfa_icon))

    # blinkers
    if lka_steering and self.CP.flags & HyundaiFlags.ENABLE_BLINKERS:
      can_sends.extend(hyundaicanfd.create_spas_messages(self.packer, self.CAN, CC.leftBlinker, CC.rightBlinker))

    if self.CP.openpilotLongitudinalControl:
      if lka_steering:
        if not self.interceptor.available: # This should probably be more of "if available AND enabled", but at this time, we dont have the "enabled" properly configured (we assume if present, its enabled)
          can_sends.extend(hyundaicanfd.create_adrv_messages(self.packer, self.CAN, self.frame))
      else:
        can_sends.extend(hyundaicanfd.create_fca_warning_light(self.packer, self.CAN, self.frame))
      if self.frame % 2 == 0:
        can_sends.append(hyundaicanfd.create_acc_control(self.packer, self.CAN, CC.enabled, self.accel_last, accel, stopping, CC.cruiseControl.override,
                                                         set_speed_in_units, hud_control, CS.main_cruise_enabled, self.tuning))
        self.accel_last = accel
    else:
      # button presses
      if (self.frame - self.last_button_frame) * DT_CTRL > 0.25:
        # cruise cancel
        if CC.cruiseControl.cancel:
          if self.CP.flags & HyundaiFlags.CANFD_ALT_BUTTONS:
            can_sends.append(hyundaicanfd.create_acc_cancel(self.packer, self.CP, self.CAN, CS.cruise_info))
            self.last_button_frame = self.frame
          else:
            for _ in range(20):
              can_sends.append(hyundaicanfd.create_buttons(self.packer, self.CP, self.CAN, CS.buttons_counter + 1, Buttons.CANCEL))
            self.last_button_frame = self.frame

        # cruise standstill resume
        elif CC.cruiseControl.resume:
          if self.CP.flags & HyundaiFlags.CANFD_ALT_BUTTONS:
            # TODO: resume for alt button cars
            pass
          else:
            for _ in range(20):
              can_sends.append(hyundaicanfd.create_buttons(self.packer, self.CP, self.CAN, CS.buttons_counter + 1, Buttons.RES_ACCEL))
            self.last_button_frame = self.frame

    return can_sends

  def apply_hyundai_steer_angle_limits(self, CS, CC, apply_angle: float) -> float:
    lat_active = CC.latActive
    v_ego_raw = CS.out.vEgoRaw
    steering_angle = CS.out.steeringAngleDeg
    limits = CarControllerParams.ANGLE_LIMITS
    apply_angle = np.clip(apply_angle, -819.2, 819.1)

    if self.angle_enable_smoothing_factor and abs(v_ego_raw) < CarControllerParams.SMOOTHING_ANGLE_MAX_VEGO:
      apply_angle = sp_smooth_angle(v_ego_raw, apply_angle, self.apply_angle_last)

    # *** max lateral jerk limit ***
    max_angle_delta = get_max_angle_delta(max(v_ego_raw, 1), self.VM)

    # prevent fault
    max_angle_delta = min(max_angle_delta, MAX_ANGLE_RATE)
    new_apply_angle = rate_limit(apply_angle, self.apply_angle_last, -max_angle_delta, max_angle_delta)

    # *** max lateral accel limit ***
    max_angle = get_max_angle(max(v_ego_raw, 1), self.VM)
    new_apply_angle = np.clip(new_apply_angle, -max_angle, max_angle)

    # *** torque reduction gain based on angle ***
    # error = abs(steering_angle - new_apply_angle)
    # normalized_error = np.clip(error / max_angle_delta, 0.0, 1.0)
    # target_torque_reduction_gain = normalized_error

    # angle is current angle when inactive
    if not lat_active:
      new_apply_angle = steering_angle
      # target_torque_reduction_gain = 0

    # prevent fault
    return float(np.clip(new_apply_angle, -limits.STEER_ANGLE_MAX, limits.STEER_ANGLE_MAX))

  def calculate_angle_torque_reduction_gain(self, CS, target_torque_reduction_gain):
    """ Calculate the angle torque reduction gain based on the current steering state. """
    if CS.out.steeringPressed:  # User is overriding
      torque_delta = self.apply_torque_last - self.min_torque_reduction_gain
      adaptive_ramp_rate = max(torque_delta / self.angle_torque_override_cycles, 0.004) # the minimum rate of change we've seen
      return max(self.apply_torque_last - adaptive_ramp_rate, self.min_torque_reduction_gain)
    else:
      # EU vehicles have been seen to "idle" at 0.384, while US vehicles have been seen idling at "0.92" for LFA.
      target_torque = np.clip(target_torque_reduction_gain, self.active_torque_reduction_gain, self.max_torque_reduction_gain)

      if self.apply_torque_last > target_torque:
        return max(self.apply_torque_last - self.ramp_down_reduction_gain_rate, target_torque)
      else:
        return min(self.apply_torque_last + self.ramp_up_reduction_gain_rate, target_torque)

  def update_angle_steering_control(self, CS, CC, actuators):
    new_angle = self.apply_hyundai_steer_angle_limits(CS, CC, actuators.steeringAngleDeg)
    torque_reduction_gain = self.calculate_angle_torque_reduction_gain(CS, abs(actuators.torque))
    return new_angle, torque_reduction_gain