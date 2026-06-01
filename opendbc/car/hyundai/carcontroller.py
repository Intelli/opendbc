import numpy as np
from opendbc.car.vehicle_model import VehicleModel
from opendbc.car.common.filter_simple import FirstOrderFilter

from opendbc.can import CANPacker
from opendbc.car import Bus, DT_CTRL, make_tester_present_msg, structs, rate_limit
from opendbc.car.lateral import apply_driver_steer_torque_limits, common_fault_avoidance, apply_steer_angle_limits_vm
from opendbc.car.common.conversions import Conversions as CV
from opendbc.car.hyundai import hyundaicanfd, hyundaican
from opendbc.car.hyundai.hyundaicanfd import CanBus
from opendbc.car.hyundai.values import HyundaiFlags, Buttons, CarControllerParams, CAR
from opendbc.car.interfaces import CarControllerBase

from opendbc.sunnypilot.car.hyundai.escc import EsccCarController
from opendbc.sunnypilot.car.hyundai.icbm import IntelligentCruiseButtonManagementInterface
from opendbc.sunnypilot.car.hyundai.longitudinal.controller import LongitudinalController
from opendbc.sunnypilot.car.hyundai.lead_data_ext import LeadDataCarController
from opendbc.sunnypilot.car.hyundai.mads import MadsCarController

VisualAlert = structs.CarControl.HUDControl.VisualAlert
LongCtrlState = structs.CarControl.Actuators.LongControlState

# EPS faults if you apply torque while the steering angle is above 90 degrees for more than 1 second
# All slightly below EPS thresholds to avoid fault
MAX_ANGLE = 85
MAX_ANGLE_FRAMES = 89
MAX_ANGLE_CONSECUTIVE_FRAMES = 2

# On some HKG CAN and CAN FD non-CANFD_ALT_BUTTONS, the cancel button (CF_Clu_CruiseSwState / CRUISE_BUTTONS = 4) is
# a pause/resume toggle, not a dedicated cancel. Firing it mid-brake inadvertently can cause a re-enable attempt
# and triggers the "SCC Conditions Not Met" alert. Delaying the button send lets factory SCC disengage
# naturally on brake press. We send ~100 ms later if it fails to do so, or if we want to cancel for another reason.
CANCEL_BUTTON_DELAY_FRAMES = 10

MAX_ANGLE_RATE = 5
ANGLE_SAFETY_BASELINE_MODEL = "KIA_EV9"
EV9_ANGLE_LIMIT_SPEED_THRESHOLD_DEFAULT = 32.0 / 3.6
ANGLE_OVERRIDE_EFFORT_MIN_PERCENT = 10.0
ANGLE_OVERRIDE_EFFORT_MAX_PERCENT = 100.0
ANGLE_OVERRIDE_EFFORT_DEFAULT_PERCENT = 10.0
ANGLE_OVERRIDE_GAIN_MIN_FLOOR = 0.10
ANGLE_OVERRIDE_STEER_THRESHOLD_HYSTERESIS = 40.0
SHARED_AUTONOMY_MODE_STOCK = 0
SHARED_AUTONOMY_MODE_IMPROVED = 1
SHARED_AUTONOMY_MODE_IMPROVED_LEGACY = 2
DISABLED_RELEASE_LOW_DEMAND_HOLD_S = 1.0
DISABLED_RELEASE_LOW_DEMAND_ANGLE_DELTA_DEG = 1.0
DISABLED_REENTRY_GUARD_AFTER_UNLATCH_S = 2.0
DISABLED_REENTRY_GRIP_DWELL_S = 0.1
MANUAL_OVERRIDE_KEEP_ACTIVE_ENTER_ANGLE_DEG = 90.0
MANUAL_OVERRIDE_KEEP_ACTIVE_EXIT_ANGLE_DEG = 15.0


def get_baseline_safety_cp():
  from opendbc.car.hyundai.interface import CarInterface
  return CarInterface.get_non_essential_params(ANGLE_SAFETY_BASELINE_MODEL)


def compute_torque_reduction_gain(steering_torque, v_ego, lat_active, steering_pressed, override_effort_scale, last_base_gain):
  if lat_active:
    ceiling = np.interp(v_ego, [0.5, 1.5], [1.0, 0.85])
    shelf = np.interp(v_ego, [2, 11], [0.45, 0.6])
    floor = np.interp(v_ego, [2, 22], [0.1, 0.3])
    bp1 = np.interp(v_ego, [2, 11], [75, 125])
    bp2 = np.interp(v_ego, [2, 11], [125, 150])
    bp3 = np.interp(v_ego, [2, 11], [175, 275])
    bp4 = np.interp(v_ego, [2, 22], [400, 700])
    target = np.interp(abs(steering_torque), [bp1, bp2, bp3, bp4], [ceiling, shelf, shelf, floor])

  else:
    target = 0.0
  base_gain = round(rate_limit(target, last_base_gain, -0.014, 0.004) / 0.004) * 0.004
  gain = base_gain

  # Manual steering override effort tuning:
  # scale down torque reduction gain only while the driver is actively overriding.
  if lat_active and steering_pressed:
    override_effort_scale = float(np.clip(override_effort_scale,
                                          ANGLE_OVERRIDE_EFFORT_MIN_PERCENT / 100.0,
                                          ANGLE_OVERRIDE_EFFORT_MAX_PERCENT / 100.0))
    gain = round((gain * override_effort_scale) / 0.004) * 0.004
    # Keep reduction gain above a minimum floor to avoid EPS temporary faults
    # from near-zero ACI reduction gain while angle control stays active.
    gain = max(gain, ANGLE_OVERRIDE_GAIN_MIN_FLOOR)

  return float(np.clip(base_gain, 0.0, 1.0)), float(np.clip(gain, 0.0, 1.0))


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


def parse_scaled_value(val, scale=10):
  if val is not None:
    return float(val) / scale
  return None


class CarController(CarControllerBase, EsccCarController, LeadDataCarController, LongitudinalController, MadsCarController,
                    IntelligentCruiseButtonManagementInterface):
  def __init__(self, dbc_names, CP, CP_SP):
    CarControllerBase.__init__(self, dbc_names, CP, CP_SP)
    EsccCarController.__init__(self, CP, CP_SP)
    MadsCarController.__init__(self)
    LeadDataCarController.__init__(self, CP)
    LongitudinalController.__init__(self, CP, CP_SP)
    IntelligentCruiseButtonManagementInterface.__init__(self, CP, CP_SP)
    self.CAN = CanBus(CP)
    self.params = CarControllerParams(CP)
    self.packer = CANPacker(dbc_names[Bus.pt])
    self.angle_limit_counter = 0
    self.angle_filter = FirstOrderFilter(0.0, 0.2, DT_CTRL)

    # Vehicle model used for lateral limiting
    self.VM = VehicleModel(CP)
    self.BASELINE_VM = VehicleModel(get_baseline_safety_cp())
    self.apply_angle_last = 0

    self.accel_last = 0
    self.apply_torque_base_last = 0
    self.apply_torque_last = 0
    self.car_fingerprint = CP.carFingerprint
    self.last_button_frame = 0
    self.cancel_counter = 0

    self.ev9_angle_limit_speed_threshold = EV9_ANGLE_LIMIT_SPEED_THRESHOLD_DEFAULT
    if self.CP_SP.hkgTuningAngleCustomLimitMaxSpeedKph > 0.0:
      self.ev9_angle_limit_speed_threshold = self.CP_SP.hkgTuningAngleCustomLimitMaxSpeedKph / 3.6

    override_effort_percent = ANGLE_OVERRIDE_EFFORT_DEFAULT_PERCENT
    if self.CP_SP.hkgTuningAngleOverrideEffortPercent > 0.0:
      override_effort_percent = float(np.clip(self.CP_SP.hkgTuningAngleOverrideEffortPercent,
                                              ANGLE_OVERRIDE_EFFORT_MIN_PERCENT,
                                              ANGLE_OVERRIDE_EFFORT_MAX_PERCENT))
    self.angle_override_effort_scale = override_effort_percent / 100.0
    self.shared_autonomy_mode = int(np.clip(self.CP_SP.hkgSharedAutonomyMode,
                                            SHARED_AUTONOMY_MODE_STOCK,
                                            SHARED_AUTONOMY_MODE_IMPROVED_LEGACY))
    self.override_active = False
    self.disabled_torque_override_active = False
    self.disabled_manual_override_latched = False
    self.disabled_low_demand_release_timer = 0.0
    self.disabled_reentry_guard_timer = 0.0
    self.disabled_reentry_grip_dwell_timer = 0.0
    self.manual_override_keep_active_latched = False

    self.apply_angle_last = 0

  def _get_override_active(self, steering_torque, steering_pressed):
    # Keep stock behavior when override effort tuning is effectively disabled.
    if self.angle_override_effort_scale >= 0.999:
      return steering_pressed

    torque_abs = abs(steering_torque)
    enter_threshold = float(self.params.STEER_THRESHOLD)
    exit_threshold = max(0.0, enter_threshold - ANGLE_OVERRIDE_STEER_THRESHOLD_HYSTERESIS)

    if self.override_active:
      self.override_active = torque_abs >= exit_threshold
    else:
      self.override_active = torque_abs >= enter_threshold

    return self.override_active

  def _get_disabled_torque_override_active(self, steering_torque, hands_on_grip):
    torque_abs = abs(steering_torque)
    enter_threshold = float(self.params.STEER_THRESHOLD)
    exit_threshold = max(0.0, enter_threshold - ANGLE_OVERRIDE_STEER_THRESHOLD_HYSTERESIS)

    if self.disabled_torque_override_active:
      self.disabled_torque_override_active = torque_abs >= exit_threshold
    else:
      # Do not enter override on touch alone. Require touch + torque.
      self.disabled_torque_override_active = hands_on_grip and torque_abs >= enter_threshold

    return self.disabled_torque_override_active

  def update(self, CC, CC_SP, CS, now_nanos):
    EsccCarController.update(self, CS)
    LeadDataCarController.update(self, CC_SP)
    MadsCarController.update(self, self.CP, CC, CC_SP, self.frame)
    if self.frame % 5 == 0:
      LongitudinalController.update(self, CC, CS)

    actuators = CC.actuators
    hud_control = CC.hudControl
    apply_torque_base = self.apply_torque_base_last

    # steering torque
    if not self.CP.flags & HyundaiFlags.CANFD_ANGLE_STEERING:
      self.angle_limit_counter, apply_steer_req = common_fault_avoidance(abs(CS.out.steeringAngleDeg) >= MAX_ANGLE, CC.latActive,
                                                                         self.angle_limit_counter, MAX_ANGLE_FRAMES,
                                                                         MAX_ANGLE_CONSECUTIVE_FRAMES)
      new_torque = int(round(actuators.torque * self.params.STEER_MAX))
      apply_torque = apply_driver_steer_torque_limits(new_torque, self.apply_torque_last, CS.out.steeringTorque, self.params)
      apply_torque_base = 0

    # angle control
    else:
      v_ego_raw = CS.out.vEgoRaw
      desired_angle = float(np.clip(actuators.steeringAngleDeg, -self.params.ANGLE_LIMITS.STEER_ANGLE_MAX, self.params.ANGLE_LIMITS.STEER_ANGLE_MAX))

      self.angle_filter.update_alpha(float(np.interp(CS.out.vEgo, [5, 10, 20], [0.2, 0.1, 0.0])))
      desired_angle = self.angle_filter.update(desired_angle)

      max_lat_accel = self.params.ANGLE_LIMITS.MAX_LATERAL_ACCEL
      max_lat_jerk = self.params.ANGLE_LIMITS.MAX_LATERAL_JERK
      if (self.CP.carFingerprint == CAR.KIA_EV9) and (v_ego_raw <= self.ev9_angle_limit_speed_threshold):
        self.params.ANGLE_LIMITS.MAX_LATERAL_ACCEL = 4.2
        self.params.ANGLE_LIMITS.MAX_LATERAL_JERK = 4.2

      apply_angle = apply_steer_angle_limits_vm(desired_angle, self.apply_angle_last, v_ego_raw, CS.out.steeringAngleDeg, CC.latActive, self.params, self.VM)

      # if we are not the baseline model, we use the baseline model for further limits to prevent a panda block since it is hardcoded for baseline model.
      if self.CP.carFingerprint != ANGLE_SAFETY_BASELINE_MODEL:
        apply_angle = apply_steer_angle_limits_vm(apply_angle or desired_angle, self.apply_angle_last, v_ego_raw, CS.out.steeringAngleDeg, CC.latActive,
                                                  self.params, self.BASELINE_VM)

      self.params.ANGLE_LIMITS.MAX_LATERAL_ACCEL = max_lat_accel
      self.params.ANGLE_LIMITS.MAX_LATERAL_JERK = max_lat_jerk

      improved_manual_control_enabled = self.shared_autonomy_mode != SHARED_AUTONOMY_MODE_STOCK
      hands_on_grip = bool(getattr(CS, "hands_on_steering_grip", 0)) if improved_manual_control_enabled else False
      touch_torque_override = False
      torque_override_active = False
      if self.shared_autonomy_mode == SHARED_AUTONOMY_MODE_STOCK:
        torque_override_active = self._get_override_active(CS.out.steeringTorque, CS.out.steeringPressed)
      else:
        self.override_active = False
        if CC.latActive:
          touch_torque_override = self._get_disabled_torque_override_active(CS.out.steeringTorque, hands_on_grip)
      override_active = torque_override_active or (improved_manual_control_enabled and touch_torque_override)
      apply_torque_base, apply_torque = compute_torque_reduction_gain(CS.out.steeringTorque, v_ego_raw, CC.latActive,
                                                                       override_active, self.angle_override_effort_scale,
                                                                       self.apply_torque_base_last)
      # For angle steering, keep angle-control active state aligned with lateral activity
      # rather than reduction-gain magnitude.
      apply_steer_req = CC.latActive

      # Failsafe if we detected we'd violate safety
      if apply_angle is None:
        apply_torque_base = 0
        apply_torque = 0
        apply_angle = CS.out.steeringAngleDeg
        apply_steer_req = False
      # Shared autonomy modes:
      # - Stock: legacy behavior.
      # - Improved Manual Control (mode 1 or legacy mode 2):
      #   latch manual control only below the EV9 limits speed with explicit driver intent
      #   (hands-on + torque override).
      manual_override_detected = False
      if CC.latActive and improved_manual_control_enabled:
        if self.disabled_reentry_guard_timer > 0.0:
          self.disabled_reentry_guard_timer = max(0.0, self.disabled_reentry_guard_timer - DT_CTRL)
        if hands_on_grip:
          self.disabled_reentry_grip_dwell_timer += DT_CTRL
        else:
          self.disabled_reentry_grip_dwell_timer = 0.0

        car_steer_demand_low = abs(desired_angle - CS.out.steeringAngleDeg) <= DISABLED_RELEASE_LOW_DEMAND_ANGLE_DELTA_DEG
        manual_control_speed_allowed = v_ego_raw <= self.ev9_angle_limit_speed_threshold
        driver_intent_override = manual_control_speed_allowed and hands_on_grip and touch_torque_override
        reentry_guard_active = self.disabled_reentry_guard_timer > 0.0
        reentry_allowed = (not reentry_guard_active) or (self.disabled_reentry_grip_dwell_timer >= DISABLED_REENTRY_GRIP_DWELL_S)

        if not self.disabled_manual_override_latched:
          self.disabled_manual_override_latched = driver_intent_override and reentry_allowed
          self.disabled_low_demand_release_timer = 0.0

        if self.disabled_manual_override_latched:
          manual_override_detected = True
          # Primary release path: hands off means return control immediately.
          if not hands_on_grip:
            self.disabled_manual_override_latched = False
            self.disabled_torque_override_active = False
            self.disabled_low_demand_release_timer = 0.0
            self.disabled_reentry_guard_timer = DISABLED_REENTRY_GUARD_AFTER_UNLATCH_S
            self.disabled_reentry_grip_dwell_timer = 0.0
            manual_override_detected = False
          else:
            # Secondary release path: steering not pressed and car demand remains low for 1s.
            if not CS.out.steeringPressed and car_steer_demand_low:
              self.disabled_low_demand_release_timer += DT_CTRL
            else:
              self.disabled_low_demand_release_timer = 0.0

            if self.disabled_low_demand_release_timer >= DISABLED_RELEASE_LOW_DEMAND_HOLD_S:
              self.disabled_manual_override_latched = False
              self.disabled_torque_override_active = False
              self.disabled_low_demand_release_timer = 0.0
              self.disabled_reentry_guard_timer = DISABLED_REENTRY_GUARD_AFTER_UNLATCH_S
              self.disabled_reentry_grip_dwell_timer = 0.0
              manual_override_detected = False
      else:
        self.disabled_torque_override_active = False
        self.disabled_manual_override_latched = False
        self.disabled_low_demand_release_timer = 0.0
        self.disabled_reentry_guard_timer = 0.0
        self.disabled_reentry_grip_dwell_timer = 0.0

      if manual_override_detected:
        # Keep angle control active only in very high-angle turns to avoid
        # temporary LKAS/MDPS warnings, while preserving lighter manual feel
        # in normal turns.
        steering_angle_abs = abs(CS.out.steeringAngleDeg)
        if self.manual_override_keep_active_latched:
          self.manual_override_keep_active_latched = steering_angle_abs >= MANUAL_OVERRIDE_KEEP_ACTIVE_EXIT_ANGLE_DEG
        else:
          self.manual_override_keep_active_latched = steering_angle_abs >= MANUAL_OVERRIDE_KEEP_ACTIVE_ENTER_ANGLE_DEG
        keep_active_in_high_angle = self.manual_override_keep_active_latched
        apply_torque = ANGLE_OVERRIDE_GAIN_MIN_FLOOR if keep_active_in_high_angle else 0
        apply_angle = CS.out.steeringAngleDeg
        apply_steer_req = keep_active_in_high_angle
        self.angle_filter.x = apply_angle
      else:
        self.manual_override_keep_active_latched = False

      # After we've used the last angle wherever we needed it, we now update it.
      self.apply_angle_last = apply_angle

      if not CC.latActive:
        self.apply_angle_last = float(np.clip(CS.out.steeringAngleDeg, -self.params.ANGLE_LIMITS.STEER_ANGLE_MAX, self.params.ANGLE_LIMITS.STEER_ANGLE_MAX))
        self.angle_filter.x = self.apply_angle_last

    if not CC.latActive:
      apply_torque_base = 0
      apply_torque = 0
      self.override_active = False
      self.disabled_torque_override_active = False
      self.disabled_manual_override_latched = False
      self.disabled_low_demand_release_timer = 0.0
      self.disabled_reentry_guard_timer = 0.0
      self.disabled_reentry_grip_dwell_timer = 0.0
      self.manual_override_keep_active_latched = False

    self.apply_torque_base_last = apply_torque_base
    self.apply_torque_last = apply_torque

    # accel + longitudinal
    accel = float(np.clip(actuators.accel, CarControllerParams.ACCEL_MIN, CarControllerParams.ACCEL_MAX))
    stopping = actuators.longControlState == LongCtrlState.stopping
    set_speed_in_units = hud_control.setSpeed * (CV.MS_TO_KPH if CS.is_metric else CV.MS_TO_MPH)

    can_sends = []

    # *** common hyundai stuff ***

    # tester present - w/ no response (keeps relevant ECU disabled)
    if self.frame % 100 == 0 and not ((self.CP.flags & HyundaiFlags.CANFD_CAMERA_SCC) or self.ESCC.enabled) and \
            self.CP.openpilotLongitudinalControl:
      # for longitudinal control, either radar or ADAS driving ECU
      addr, bus = 0x7d0, self.CAN.ECAN if self.CP.flags & HyundaiFlags.CANFD else 0
      if self.CP.flags & HyundaiFlags.CANFD_LKA_STEER_MSG.value:
        addr, bus = 0x730, self.CAN.ECAN
      can_sends.append(make_tester_present_msg(addr, bus, suppress_response=True))

      # for blinkers
      if self.CP.flags & HyundaiFlags.CANFD_ENABLE_BLINKERS:
        can_sends.append(make_tester_present_msg(0x7b1, self.CAN.ECAN, suppress_response=True))

    # Delay the cancel button send so the brake can disengage factory SCC first.
    # Reset whenever openpilot is no longer requesting cancel.
    self.cancel_counter = self.cancel_counter + 1 if CC.cruiseControl.cancel else 0

    # *** CAN/CAN FD specific ***
    if self.CP.flags & HyundaiFlags.CANFD:
      can_sends.extend(self.create_canfd_msgs(apply_steer_req, apply_torque, set_speed_in_units, accel,
                                              stopping, hud_control, CS, CC))
    else:
      # Hold torque with induced temporary fault when cutting the actuation bit
      # FIXME: we don't use this with CAN FD?
      torque_fault = CC.latActive and not apply_steer_req

      can_sends.extend(self.create_can_msgs(apply_steer_req, apply_torque, torque_fault, set_speed_in_units, accel,
                                            stopping, hud_control, actuators, CS, CC))

    # Intelligent Cruise Button Management
    can_sends.extend(IntelligentCruiseButtonManagementInterface.update(self, CS, CC_SP, self.packer, self.frame, self.last_button_frame, self.CAN))

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
      if self.cancel_counter > CANCEL_BUTTON_DELAY_FRAMES:
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
                                                      self.lead_data, hud_control, set_speed_in_units, stopping,
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

    lka_steering = self.CP.flags & HyundaiFlags.CANFD_LKA_STEER_MSG
    lka_steering_long = lka_steering and self.CP.openpilotLongitudinalControl

    # steering control
    can_sends.extend(hyundaicanfd.create_steering_messages(self.packer, self.CP, self.CAN, CC.enabled, apply_steer_req, apply_torque, self.apply_angle_last
                                                           , self.lkas_icon))

    # prevent LFA from activating on LKA steering cars by sending "no lane lines detected" to ADAS ECU
    if self.frame % 5 == 0 and lka_steering:
      can_sends.append(hyundaicanfd.create_suppress_lfa(self.packer, self.CAN, CS.lfa_block_msg,
                                                        self.CP.flags & HyundaiFlags.CANFD_LKA_STEER_MSG_ALT))

    # LFA and HDA icons
    if self.frame % 5 == 0 and (not lka_steering or lka_steering_long):
      can_sends.append(hyundaicanfd.create_lfahda_cluster(self.packer, self.CAN, CC.enabled, self.lfa_icon))

    # blinkers
    if lka_steering and self.CP.flags & HyundaiFlags.CANFD_ENABLE_BLINKERS:
      can_sends.extend(hyundaicanfd.create_spas_messages(self.packer, self.CAN, CC.leftBlinker, CC.rightBlinker))

    if self.CP.openpilotLongitudinalControl:
      if lka_steering:
        can_sends.extend(hyundaicanfd.create_adrv_messages(self.packer, self.CAN, self.frame))
      else:
        can_sends.extend(hyundaicanfd.create_fca_warning_light(self.packer, self.CAN, self.frame))
      if self.frame % 2 == 0:
        can_sends.append(hyundaicanfd.create_acc_control(self.packer, self.CAN, CC.enabled, self.accel_last, accel, stopping, CC.cruiseControl.override,
                                                         set_speed_in_units, hud_control, self.lead_data, CS.main_cruise_enabled, self.tuning))
        self.accel_last = accel
    else:
      # button presses
      if (self.frame - self.last_button_frame) * DT_CTRL > 0.25:
        # cruise cancel
        if CC.cruiseControl.cancel:
          # Here we send ACC message to cancel, not buttons. Don't delay
          if self.CP.flags & HyundaiFlags.CANFD_ALT_BUTTONS:
            can_sends.append(hyundaicanfd.create_acc_cancel(self.packer, self.CP, self.CAN, CS.cruise_info))
            self.last_button_frame = self.frame
          elif self.cancel_counter > CANCEL_BUTTON_DELAY_FRAMES:
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
