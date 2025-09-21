# About Angle Steering, Baseline Model, and ISO Safety Limits

## Overview

The baseline vehicle model serves as our safety reference point for establishing universal control parameters across our entire vehicle fleet. This approach ensures that all vehicles operate within ISO safety standards regardless of their individual physics characteristics.

## How It Works

For any given speed and driving conditions, we query each vehicle model with the question: "What's the maximum steering angle I can apply right now while staying below the ISO standard limits for lateral acceleration and jerk?"

Each vehicle model responds differently based on its unique physics characteristics:
- **Baseline Vehicle Model**: Has the most restrictive limits - requires the smallest steering angles to stay within ISO safety thresholds
- **Current Vehicle Model**: May be able to handle larger steering angles while remaining within the same ISO safety standards, depending on its physical characteristics

## Safety Logic

Both the baseline and current vehicle models target the same ISO safety limits. The key difference is that the baseline vehicle's physics require more conservative steering inputs to stay within those standards.

By applying the baseline vehicle's more restrictive steering limits to all vehicles in the fleet:
- Every model stays well within ISO safety thresholds
- No vehicle will breach safety standards regardless of its individual characteristics
- We maintain a consistent safety margin across the entire fleet

If we instead used a less restrictive model as our baseline, any naturally more restrictive vehicles in the fleet would exceed ISO standards and create unsafe driving conditions.

## Implementation Details

### Two-Layer Validation Process

We use a two-layer validation approach to balance vehicle-specific optimization with baseline safety:

1. **Current Vehicle Model Calculation**: We first calculate the optimal steering limits from the current vehicle model, which determines the best steering angle based on that vehicle's physical characteristics and desired maneuver.

2. **Baseline Model Validation**: We then pass the current vehicle's requested steering command through the baseline model as a final safety filter.

This two-layer approach is essential because while the current vehicle model may generally be more capable, it might sometimes request smaller jerk or lateral acceleration values to achieve specific driving goals. The baseline validation ensures that regardless of what the current vehicle requests, we never exceed the safety limits of our most restrictive vehicle.

### Special Case: Driving the Baseline Vehicle

When actually driving the baseline vehicle itself (e.g., Santa Fe), we apply an additional 80% ISO cap since that vehicle would otherwise operate right at the ISO safety threshold. This extra margin ensures safe operation even for our most restrictive vehicle model.

## Example Implementation

### Baseline Vehicle: Hyundai Santa Fe
Due to its physics characteristics, the Santa Fe requires smaller steering angles to remain within ISO safety limits.

### Current Vehicle: Ioniq 5 PE
Due to different physical characteristics, the Ioniq 5 PE can handle larger steering angles while still remaining within the same ISO safety standards.

By using the Santa Fe's conservative limits across all vehicles, we ensure the Ioniq 5 PE operates well within its capabilities, while preventing the Santa Fe from exceeding its safety thresholds.

## Kia EV9 Angle-Steering Integration

This angle-steering development branch extends the Hyundai/Kia/Genesis stack to explicitly support the Kia EV9 platform:

- **Platform identification:** The Hyundai values module now encodes a three-bit CAN-FD platform identifier and exposes the `HyundaiCanfdPlatform` enum so that the EV9 can be tagged at the CarParams layer and in safety tests.【F:opendbc/car/hyundai/values.py†L126-L134】【F:opendbc/safety/tests/test_hyundai_canfd.py†L8-L15】 The Hyundai interface applies this identifier to EV9 angle-steering configurations so Panda can select the correct vehicle model.【F:opendbc/car/hyundai/interface.py†L161-L164】
- **Panda safety vehicle model:** The CAN-FD safety hooks cache the platform identifier, define EV9-specific vehicle-model parameters, and choose them when validating steering angle commands; initialization decodes the ID directly from `safetyParam`.【F:opendbc/safety/modes/hyundai_canfd.h†L48-L52】【F:opendbc/safety/modes/hyundai_canfd.h†L195-L227】【F:opendbc/safety/modes/hyundai_canfd.h†L343-L351】
- **Controller alignment:** The Hyundai angle controller now inspects the Panda platform ID and skips the legacy baseline fallback when Panda has already switched to an EV9 vehicle model, keeping both sides aligned on the same physics assumptions.【F:opendbc/car/hyundai/carcontroller.py†L214-L225】
- **Safety coverage:** Dedicated EV9 safety tests exercise the Panda EV9 vehicle model to verify the lateral acceleration and jerk checks with the EV9 limits.【F:opendbc/safety/tests/test_hyundai_canfd.py†L514-L579】
- **Supporting data:** The EV9 platform definition advertises CAN-FD angle-steering support, torque overrides align with other HKG angle platforms, and the replay guide includes EV9 environment variables for debugging this branch.【F:opendbc/car/hyundai/values.py†L622-L627】【F:opendbc/car/torque_data/override.toml†L100-L105】【F:opendbc/car/hyundai/tests/replay_route.md†L11-L16】
- **Low-speed parking mode assist:** The EV9 angle controller now only enters ADAS parking mode between 1–7 km/h when the steering request exceeds 120°, clamps commands to the 176.7° MDPS cap, forces an exit once speeds drop below 0.5 km/h, exceed 10 km/h, or after roughly 0.8 s, and enforces a 0.5 s cooldown before re-enabling while sending the matching LKAS parking frame to honor the sharper rack limit without touching higher-speed behavior.【F:opendbc/car/hyundai/carcontroller.py†L215-L290】【F:opendbc/car/hyundai/hyundaicanfd.py†L39-L78】【F:opendbc/car/hyundai/values.py†L69-L77】
