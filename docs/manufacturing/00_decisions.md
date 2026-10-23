# Manufacturing Overhaul Decision Brief

Research snapshot: 2026-08-08.

## Product decisions

These are the execution baseline for the manufacturing overhaul.

- Machine authority: approved-job autonomy. A human approves an immutable job/setup; the deterministic arbiter may then start, hold, monitor, and notify. Alarm recovery, changed inputs, and resume require fresh approval. Software commands never substitute for E-stop/interlocks.
- Controller rollout: Shapeoko 3 XL / Grbl 1.1 first; FluidNC and grblHAL next; proprietary controllers remain post/file-only until explicit vendor support.
- Twin: deterministic engineering twin with fixed integer ticks, controller state, 3-axis kinematics, fixtures, stock removal, collision events, virtual sensors/cameras, fault injection, snapshot/restore, and replay. It is not safety certification.
- Metrology: touch probe/tool setter plus calibrated fixed camera/fiducials. Camera-derived results are advisory; machine offsets require qualified probe/calibration evidence and approval.
- Slicer: maintained PrusaSlicer sidecar fork pinned to upstream tags with a small patch queue and complete AGPL corresponding-source compliance.
- Printer adapters: Moonraker first, OctoPrint second, capability-probed PrusaLink third, direct Marlin later.
- Predictive model: Forecast-to-Compress Manufacturing Latent Dynamics Perceptron. Reuse reviewed permissive encoders/detectors; train temporal prediction, fusion, uncertainty, event selection, and compression heads on owned/licensed data.
- Additional manufacturing implementation now: probing and quality. Other modes receive extensible capability schemas only unless later selected.
- Edge: cross-platform Windows/Linux local gateway with durable state and safety arbitration; cloud/mobile is an optional authenticated relay.
- Media: short rolling buffers with event-centric evidence clips and structured telemetry, not continuous recording by default.

## Hard safety and truthfulness rules

- NC generation, transfer, monitoring, machine execution, and safety integration are separate capability levels.
- One command owner per device. Observers are read-only and capability-negotiated.
- No transparent resume after reset, hard limit, alarm, disconnect, or power loss.
- Simulation success never bypasses controller limits, physical interlocks, setup verification, or operator approval.
- Model output proposes actions; deterministic code and hardware own authorization and state transitions.
- Every executable approval binds machine identity, immutable artifact hash, job/setup revision, tool/fixture/WCS snapshot, preflight evidence, approver identity, and expiry.
- Missing/stale/unknown telemetry fails closed.
- Camera perception is advisory until separately validated; it never serves as a guarding or E-stop function.
- Copyleft components remain out-of-process behind coarse documented contracts unless the product deliberately adopts compatible licensing.
