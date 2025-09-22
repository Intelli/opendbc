# Kia EV9 APRK Findings

## What We Know
- The parking command frame we originally targeted (`0x38C` / `RSPA11`) only exists in the legacy CAN bus layout. EV9 CAN-FD traffic never carries it; instead the factory automatic parking ECU (`APRK`) publishes `SPAS1` (`0x165`), `SPAS2` (`0x16A`), and `LFA_ALT` (`0x0CB`).
- Fresh captures from the OEM parallel-park sequence confirm that `LFA_ALT` includes the live steering command (`ADAS_StrAnglReqVal`) and APRK status bits. During the maneuver `ADAS_ActvACISta` stays at `0` while `ADAS_ActvACILvl2Sta` stays at `1`, and the requested angle hits the DBC clamp at `119.9°` as the steering wheel swings past ±500°.
- `SPAS2.BLINKER_CONTROL` toggles to `4` (right-side park) exactly while `carState.rightBlinker=True`; the APRK ECU is responsible for blinker output. `SPAS1.NEW_SIGNAL_1` floats around `1.2` (scaled), indicating an active placeholder field that likely reports steering-phase state.
- `carControl`/`controlsState`/`carOutput` logs show openpilot completely idle (`latActive=False`, torque command zero) throughout the capture. All steering authority and blinkers remained with APRK, so forcing state bits from openpilot is futile unless we mirror APRK payloads precisely.
- The reverted EV9 parking patch imposed heuristic limits (`PARKING_MODE_MAX_DURATION=0.8s`, `PARKING_MODE_EXIT_SPEED_MIN≈0.08 m/s`, status nybbles forced to `2`) that conflict with stock behaviour. APRK keeps the wheel saturated and pauses near standstill, so the controller kept tripping its own cooldown and never produced valid APRK fields.
- Hyundai CAN-FD DBC entries for `SPAS1`, `SPAS2`, and several APRK-related fields still carry placeholder names. Without naming and scaling these signals we cannot encode the correct command set or expose telemetry to tooling/tests.

## Current Objective
- Use APRK/SPAS1+SPAS2 commands to exceed the 120° steering clamp that the standard ADAS path enforces on EV9.
- Engage only when openpilot requests >120° at crawl speeds and we are in a drive gear; immediately hand control back to stock angle control once the request falls below 119° or the driver intervenes.
- APRK should only stay active long enough to swing past the clamp, after which we drop the APRK fields to keep the factory system in charge.

## Detailed Capture Notes (Sep 21, 2025 drive)
- `can` log excerpt:
  - `LFA_ALT` (`0x0CB`) payloads `d7657110af04…` → decoded: checksum `0x65D7`, counter `0x71`, `ADAS_ActvACISta=0`, `ADAS_ActvACILvl2Sta=1`, `ADAS_StrAnglReqVal=119.9°`, torque reduction gain `0.0`.
  - Later frame `ca107610ce3f…` shows the angle request dropping toward `-5°` while status nybbles remain `0/1`.
  - `SPAS1` (`0x165`) payload `ea48974b…0c00…` → checksum `0x48EA`, counter `0x97`, placeholder word `0x000C` toggled during the active park phase.
  - `SPAS2` (`0x16A`) payload `13699406…` → checksum `0x6913`, counter `0x94`, `BLINKER_CONTROL=4` (right blink command) while APRK is steering.
  - `LKAS_ALT` (`0x110`) remains at zero torque (`StrTqReqVal=-1024`, `ActToiSta=0`), proving APRK is sourcing LFA angle to the MDPS without camera torque overlay.
- `carState` frames line up: reverse gear, `rightBlinker=True`, wheel at `+509.8°` with ~0.25 m/s creep; later neutral frame holds `-498.5°` with brake applied while APRK stabilises.
- `controlsState.lateralControlState.angleState.active=False` and `carControl.latActive=False` through the entire window; APRK owns steering.

### APRK signal breakdown (same capture)
- `SPAS1` (`0x165`, 24 B, little-end data words shown as 16‑bit):
  - Word0/Word1 carry checksum/counter; the high byte of Word1 behaves as an APRK state byte (`0x02` standby, `0x48` slot-confirm, `0x4B` active steering).
  - Word2 stayed at `0x006D` while APRK was engaged (likely a calibrated steer-rate or slot size constant); word3 oscillated between `0x2D2D` and `0x2D0D` as the manoeuvre progressed, indicating the planned wheel sweep limits (~4.5°/1.3° when interpreted with a 0.1° scale).
  - Word5 held `0x000C` (1.2 with the current 0.1° scale) whenever `APRK` was live; treat this as `APRK_PathSegmentStep` until more variation is observed.
  - Word6 matched the `LFA_ALT` request exactly (`0x0077` = 119.9° during full-lock) → rename the placeholder signal to `APRK_SteerAngleCmdDeg` (scale 0.1°).
  - Word7/Word8 map to per-side steering limits (`0x002D`/`0x000D` while finishing, `0x002D`/`0x002D` while sweeping); encode as left/right angle caps.
  - Word9 flipped from `0x0000` in standby to `0x0001` once APRK selected reverse; expect a forward phase to surface `0x0002` (needs confirmation).
- `SPAS2` (`0x16A`, 32 B):
  - Checksum/counter behave as expected; the high byte of Word1 (`0x01` vs `0x06`) tracks coarse APRK phase (initialisation vs active control).
  - Word2 toggled between `0x0001` and `0x0002`; on this right-side park the sequence was `1→2→1`, suggesting “slot side” or “segment index”.
  - Word3’s low byte carried travel distance commands (`0x0E` ≈ 0.14 m pre-move, `0x81` ≈ 1.29 m during the main reverse). 
  - Word7 flipped among `0x0303`, `0x0313`, `0x0315`; treat as APRK sub-state bits (slot valid, path locked, etc.).
  - Word8 set bit7 (`0x0080`) only while APRK was sending a brake/hold request.
  - Word9’s low byte matched the commanded crawl speed (0x64 → 1.00 m/s, 0x90 → 1.44 m/s, 0x00 when stationary).
  - Word10/Word11 low bytes (`0x5F`, `0x4C`) tracked the path curvature/heading offsets while the wheel was at ±500°. Their high bytes changed with manoeuvre progress, so keep raw 16-bit values until scaling is confirmed.
  - Word15 was `0x0200` throughout the active steer window; likely a boolean enable mask.
- `MDPS` (`0x0EA`) confirmed APRK state transitions: `PaModeSta` jumped between `3` and `5`, wheel angle (`PaStrAnglVal`) matched the ~±500° telemetry, and `PaPlugInSta` stayed `1` while APRK owned the rack.

## Implementation Notes (Sep 21, 2025)
- DBC placeholders for `SPAS1`/`SPAS2` are now wired up to descriptive APRK signal names, including angle command/limits, gear hints, slot side, path distance, curvature bytes, and enable masks.
- `hyundaicanfd.create_spas_messages` accepts an `AprkCommand` dataclass so the controller can populate APRK fields exactly as logged. Default behaviour keeps the legacy blinker passthrough when APRK is inactive.
- EV9 CAR controller treats APRK as a low-speed “high angle assist”: it arms once openpilot asks for >120° while travelling 1–20 kph in drive, generates SPAS requests up to the 176.7° OEM ceiling, and drops straight back to ADAS steering the moment speed falls below the active floor, the requested angle relaxes, the driver adds torque, or the ~2 s timer expires. This keeps APRK out of true parking manoeuvres but gives us the extra rack sweep we need for tight turns.
- The APRK command stream mirrors the capture’s handshake bytes: standby frames use phase `0x01` with status word `0x0303`, active steering uses phase `0x06` with status `0x0315`, and both are exposed as constants in `CarControllerParams` to keep the packer/tests aligned. Per-frame curvature bytes are synthesised from the vehicle model so SPAS receives coherent path hints even though we are not replaying a recorded park trace.
- Regression coverage (`test_aprk_spas.py`) checks the packed APRK frames match the decoded field values from the drive capture.

## Testing Notes
- Run the full validation via `UV_CACHE_DIR=$PWD/.uv_cache JAVA_HOME=/Library/Java/JavaVirtualMachines/jdk-23.jdk/Contents/Home ./test.sh`; the bundled `setup.sh` will provision the virtualenv and safety toolchain (adjust `UV_CACHE_DIR` to any writable path if your environment restricts the default ~/.cache).
- Expect the MISRA stage to print the known Hyundai CAN-FD essential-type warning (hyundai_canfd.h:350); it predates these APRK changes and remains tolerated per repo guidance. Confirm no new violations stem from the staged code, and otherwise treat existing failures as outside the scope of this branch.
