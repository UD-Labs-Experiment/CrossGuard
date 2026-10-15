# Initial source release verification

Verified on 15 October 2026 in a fresh publication staging directory, using the existing development Python 3.12.10 environment and Node.js 24.12.0. No live CARLA simulation was run for this release.

- `python verify_release.py`: 320 offline Python regression tests passed; frozen catalogue SHA-256 and requested study denominators verified.
- The full discovery command was also tried: 320 checks passed, and one evidence-dependent check failed because its separately retained Revision 03 catalogue and validation baseline are intentionally absent. `verify_release.py` explicitly identifies and excludes exactly that check. The original test remains available unchanged.
- `npm ci --ignore-scripts`: installed the committed frontend lockfile; npm reported zero vulnerabilities at verification time.
- `node --test tests/*.test.js`: all 16 browser-side checks passed.
- `npm run build`: production frontend compiled successfully. Built files and installed dependencies are excluded from Git.
- Filename and text review excluded secrets, environment files, model weights, local history, runtime logs, results, and large binary artifacts. The unchanged frozen catalogue hash is `88ed12426883f20bdfda16cd084ba7c8cb2781a981b33e27373579b991b5571f`.

## Snapshot provenance

The selected active source files originate from local development commit `c6dc85485fa1c8f709839602adc3b5a95a6c67e0`. The private development Git history is not included. The study catalogue and its hash were copied unchanged from the separately retained Study 02, Revision 05 inputs.

The only alteration to the copied implementation is the default `--agy` executable in `src/tools/town10_unattended_robustness.py`: the development machine's absolute user path is replaced by `agy`, resolved through PATH. The command-line override remains available. Public README, ignore rules, this verification record and `verify_release.py` were added for the release. Historical status notes, internal workspace navigation and model weights were excluded.

These checks verify the released source and input integrity. They do not establish independent reproduction of the paper's experimental outcomes or exact numerical replay of the simulator.
