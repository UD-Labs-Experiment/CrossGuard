# CrossGuard

Research prototype for cooperative pedestrian warnings and vehicle response in CARLA 0.9.16. Rendered RGB observations pass through person detection, ground projection and tracking, a timestamped observation contract, modeled communication links, and receiver risk policies. Automatic and retrofit receiver modes are evaluated with a lead vehicle and two followers.

This is an initial, deliberately limited public source release for the accompanying IEEE manuscript. It is a new publication snapshot, not a reconstruction of the project's development history.

## Included

- `src/crossguard/`: active perception, transport, policy, simulation and FastAPI bridge implementation.
- `src/web/`: React/Vite interface source, lockfile and browser-side checks.
- `src/tools/`: scenario/study execution, offline paired analysis, export and validation utilities.
- `src/tests/`: regression tests; some evidence-dependent checks require the separately retained study files.
- `study/frozen_catalogue.json`: the unchanged Study 02, Revision 05 scenario catalogue, with its SHA-256 record.
- `src/models/MODEL_PROVENANCE.json`: original model download location and expected hash; weights are excluded.

## Scope of reproducibility

The catalogue specifies 60 robustness configurations and seven validation configurations. Each configuration has two receiver modes and assistance OFF/ON. Thus the planned robustness study contains 240 runs (120 pairs); the validation gate contains 28 separate runs (14 pairs).

This release makes the implementation and frozen experimental inputs inspectable. Raw run results, timelines, RGB recordings, video, derived analysis and the manuscript itself are not included in this initial release. The published source alone therefore cannot regenerate or independently verify the manuscript's empirical numbers. Those retained materials are planned for a later release; no availability date is implied. A fresh simulator execution is also not guaranteed to reproduce identical physical trajectories.

## Environment and offline checks

Use Python 3.11 or later. CARLA and its Python wheel must both match 0.9.16. The study was developed on Windows; some helpers use Windows-specific paths and process utilities. Create an environment at `src/.venv`, install `requirements.txt` and `src/requirements-rgb.txt`, and install `requests` and `psutil` for the study utilities. Web-only dependencies are listed in `src/requirements-web.txt`. The requirements describe the retained development environment; they are not a cross-platform lockfile.

From the repository root in PowerShell:

```powershell
$env:PYTHONPATH = 'src;src/tools'
src/.venv/Scripts/python.exe verify_release.py
```

The full suite includes an evidence-dependent dashboard check for an earlier retained Revision 03 catalogue and validation baseline, which are excluded from this snapshot. See `VERIFICATION.md` for the offline checks actually run for this release.

For frontend checks and a build:

```powershell
cd src/web
npm ci
node --test tests/*.test.js
npm run build
```

## Running the prototype

Acquire the detector separately from the URL in `src/models/MODEL_PROVENANCE.json`, check its SHA-256, and place it at `src/models/yolo11n.onnx`. Review the upstream model's terms before use; the model is not redistributed here. Install and run CARLA 0.9.16 separately on port 2000. Build the web interface, then launch `./src/run-web.ps1 -Port 8007 -Receiver autonomous` from the repository root, or use `-Receiver retrofit`.

Only one bridge may own CARLA. Use the bridge command queue for live simulation controls; independent validation clients must not tick the world. Offline analysis scripts read saved results and do not create missing evidence. The original study helpers expect the preserved `Evidence/Study_02_Town10HD_Continuation/Revision_05` layout; `study/` is a public copy of its frozen input, not a replacement results tree. Inspect each helper's arguments before use.

## Limitations and rights

The communication links and retrofit response are modeled; this is not a physical radio or phone deployment. Weather readback checks establish scenario labeling, not validated tire friction or real-world safety. CARLA, detector weights and dependencies have their own terms.

No software license is granted by this initial release. Public visibility does not by itself grant permission to reuse or redistribute the code. The repository owners will select a license separately.
