from types import SimpleNamespace

import pytest

from opendbc.can import CANPacker
from opendbc.car.hyundai.hyundaicanfd import AprkCommand, create_spas_messages


def _extract_motorola(data: bytes, start_bit: int, length: int, signed: bool = False) -> int:
  start2 = start_bit + length - 1
  start_byte = start2 // 8
  end_bit = start2 - length + 1
  end_byte = end_bit // 8
  unused_bits = end_bit % 8
  if unused_bits < 0:
    unused_bits += 8

  value = 0
  for i in range(start_byte, end_byte - 1, -1):
    value = (value << 8) | data[i]
  value >>= unused_bits
  mask = (1 << length) - 1
  value &= mask
  if signed and (value & (1 << (length - 1))):
    value -= 1 << length
  return value


def test_create_spas_messages_active():
  pytest.importorskip("numpy", reason="numpy required for CAN packer")
  packer = CANPacker("hyundai_canfd_generated")
  can_iface = SimpleNamespace(ECAN=0)

  cmd = AprkCommand(
    enabled=True,
    command_state=0x4B,
    command_phase=0x06,
    slot_side=6,
    path_step=12,
    steer_angle_deg=172.3,
    left_limit_deg=176.7,
    right_limit_deg=176.7,
    selected_gear=2,
    path_distance_m=0.32,
    status_word=0x0315,
    brake_hold_active=False,
    target_speed_mps=1.4,
    curvature0=0x10,
    curvature1=0x5F,
    curvature2=0x90,
    curvature3=0x4C,
    enable_mask=0x02,
  )

  msgs = create_spas_messages(packer, can_iface, left_blink=False, right_blink=True, aprk_cmd=cmd)
  assert len(msgs) == 2

  spas1_addr, spas1_dat, spas1_bus = msgs[0]
  spas2_addr, spas2_dat, spas2_bus = msgs[1]

  assert spas1_addr == 0x165
  assert spas2_addr == 0x16A
  assert spas1_bus == 0
  assert spas2_bus == 0

  assert _extract_motorola(spas1_dat, 24, 8) == 0x4B
  assert _extract_motorola(spas1_dat, 88, 8) == 12
  assert pytest.approx(_extract_motorola(spas1_dat, 96, 16, signed=True) * 0.1, rel=0, abs=1e-6) == 172.3
  assert pytest.approx(_extract_motorola(spas1_dat, 112, 16) * 0.1, rel=0, abs=1e-6) == 176.7
  assert pytest.approx(_extract_motorola(spas1_dat, 128, 16) * 0.1, rel=0, abs=1e-6) == 176.7
  assert _extract_motorola(spas1_dat, 152, 8) == 2

  assert _extract_motorola(spas2_dat, 24, 8) == 6
  assert _extract_motorola(spas2_dat, 32, 8) == 0x06
  assert pytest.approx(_extract_motorola(spas2_dat, 56, 8) * 0.01, rel=0, abs=1e-6) == 0.32
  assert _extract_motorola(spas2_dat, 112, 16) == 0x0315
  assert _extract_motorola(spas2_dat, 128, 1) == 0
  assert pytest.approx(_extract_motorola(spas2_dat, 152, 8) * 0.01, rel=0, abs=1e-6) == 1.4
  assert _extract_motorola(spas2_dat, 160, 8) == 0x10
  assert _extract_motorola(spas2_dat, 168, 8) == 0x5F
  assert _extract_motorola(spas2_dat, 176, 8) == 0x90
  assert _extract_motorola(spas2_dat, 184, 8) == 0x4C
  assert _extract_motorola(spas2_dat, 248, 8) == 0x02


def test_create_spas_messages_defaults():
  pytest.importorskip("numpy", reason="numpy required for CAN packer")
  packer = CANPacker("hyundai_canfd_generated")
  can_iface = SimpleNamespace(ECAN=0)

  msgs = create_spas_messages(packer, can_iface, left_blink=False, right_blink=False, aprk_cmd=AprkCommand())
  spas1_addr, spas1_dat, spas1_bus = msgs[0]
  spas2_addr, spas2_dat, spas2_bus = msgs[1]

  assert spas1_addr == 0x165
  assert spas2_addr == 0x16A
  assert spas1_bus == 0
  assert spas2_bus == 0

  assert _extract_motorola(spas1_dat, 24, 8) == 0x02
  assert _extract_motorola(spas1_dat, 96, 16, signed=True) == 0
  assert _extract_motorola(spas1_dat, 112, 16) == 0
  assert _extract_motorola(spas1_dat, 128, 16) == 0
  assert _extract_motorola(spas1_dat, 152, 8) == 0

  assert _extract_motorola(spas2_dat, 24, 8) == 0
  assert _extract_motorola(spas2_dat, 32, 8) == 0
  assert _extract_motorola(spas2_dat, 56, 8) == 0
  assert _extract_motorola(spas2_dat, 112, 16) == 0
  assert _extract_motorola(spas2_dat, 128, 1) == 0
  assert _extract_motorola(spas2_dat, 248, 8) == 0
