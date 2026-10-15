"""Independent offline post-export integrity checks; never grants scene acceptance."""
import argparse
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from town10_continuation import digest, read, verified_catalogue
from town10_unattended_robustness import atomic_json

VIEWS = ('top', 'driver', 'person', 'opposite', 'driver_b', 'driver_c')


def contained(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise RuntimeError('Evidence path leaves root')
    return path


def unchanged_inputs(root, snapshot):
    if not snapshot:
        raise RuntimeError('Empty simulation input snapshot')
    changed = [name for name, sha in snapshot.items()
               if not contained(root, name).is_file() or digest(contained(root, name)) != sha]
    if changed:
        raise RuntimeError('Original simulation inputs changed: ' + ', '.join(changed[:10]))


def verify_sources(sources, manifests, runs, states):
    if len(sources) != 201:
        raise RuntimeError('Source map must contain 201 frames')
    lookup = {state: {e['frame']: e for e in manifests[state]} for state in states}
    maximum_error = 0.0
    for index, row in enumerate(sources):
        target = index / 10.0
        if abs(row['video_s'] - target) > 1e-8 or set(row['sources']) != set(states):
            raise RuntimeError('Source map output timing/state mismatch')
        for state in states:
            entry = row['sources'][state]
            if lookup[state].get(entry['frame']) != entry:
                raise RuntimeError('Source entry differs from original camera manifest')
            error = abs(entry['time_s'] - target)
            maximum_error = max(maximum_error, error)
            if error > .051 or entry.get('missing') or set(entry['views']) != set(VIEWS):
                raise RuntimeError('Source alignment or synchronized camera coverage failed')
            nearest = min(manifests[state], key=lambda e: abs(e['time_s'] - target))
            if nearest != entry:
                raise RuntimeError('Source map does not reference nearest actual recorded frame')
            for filename in entry['views'].values():
                if not contained(runs[state] / 'assets/pictures', filename).is_file():
                    raise RuntimeError('Original recorded camera image missing')
    return maximum_error


def decode_video(path, width, capture_factory=None):
    import cv2
    cv2.setNumThreads(1)
    decoder = (capture_factory or cv2.VideoCapture)(str(path))
    count = 0
    try:
        if not decoder.isOpened():
            raise RuntimeError('Video decoder could not open media')
        while True:
            ok, frame = decoder.read()
            if not ok:
                break
            if frame.shape[:2] != (1210, width):
                raise RuntimeError('Decoded video dimensions differ')
            count += 1
        fps = decoder.get(cv2.CAP_PROP_FPS)
        if count != 201 or abs(fps - 10) > .01:
            raise RuntimeError(f'Full decode failed: {count} frames, {fps} fps')
        return {'decoded_frames': count, 'fps': fps, 'dimensions': [width, 1210]}
    finally:
        decoder.release()


def check(root, catalogue_sha256):
    root = root.resolve()
    reportdir = root / 'Reports/Independent_Review'
    reportdir.mkdir(parents=True, exist_ok=True)
    report = {'status': 'CHECKING', 'passed': False, 'planned_videos': 360,
              'checked_videos': 0, 'scene_review_status': 'PENDING',
              'note': 'Offline media integrity only. No scene acceptance or CARLA calls.',
              'videos': [], 'catalogue_sha256': catalogue_sha256}
    def checkpoint():
        report['updated_utc'] = datetime.now(timezone.utc).isoformat()
        atomic_json(reportdir / 'media_integrity_check.json', report)
    checkpoint()
    try:
        if digest(root / 'frozen_catalogue.json') != catalogue_sha256:
            raise RuntimeError('Catalogue checksum mismatch')
        configurations = [c for c in verified_catalogue(root)['tests'] if c['test'].startswith('Robustness_')]
        if len(configurations) != 60:
            raise RuntimeError('Expected 60 robustness configurations')
        exportdir = root / 'Reports/Video_Export'
        progress = read(exportdir / 'progress.json')
        preservation = read(exportdir / 'simulation_input_preservation.json')
        if progress.get('completed_videos') != 360 or progress.get('failed_videos') != 0 or progress.get('status') != 'COMPLETE_PENDING_INDEPENDENT_REVIEW':
            raise RuntimeError('Export has not successfully completed all 360 videos')
        if preservation.get('passed') is not True or preservation.get('changed_files'):
            raise RuntimeError('Exporter simulation input preservation did not pass')
        snapshot = read(exportdir / 'simulation_input_hashes.json')
        unchanged_inputs(root, snapshot)
        themes = {f'T{n:02}': read(exportdir / f'progress_T{n:02}.json') for n in range(1, 7)}
        for cfg in configurations:
            theme = themes[cfg['theme']]
            if theme.get('catalogue_sha256') != catalogue_sha256 or len(theme.get('completed_modes', {})) != 20:
                raise RuntimeError('Theme checkpoint identity/coverage differs')
            folder = root / cfg['relative_path']
            for mode in ('Automatic', 'Retrofit'):
                saved = theme['completed_modes'][f'{cfg["test"]}/{mode}']
                runs = {s: folder / mode / f'Assistance_{s}' for s in ('OFF', 'ON')}
                for run in runs.values():
                    for source in run.glob('*.json'):
                        if str(source.relative_to(root)) not in snapshot:
                            raise RuntimeError('Original run JSON absent from pre-export snapshot')
                for relative, expected in saved['source_identity'].items():
                    if digest(contained(folder, relative)) != expected:
                        raise RuntimeError('Mode checkpoint source identity differs')
                manifests = {s: read(run / 'camera_manifest.json') for s, run in runs.items()}
                paths = [runs[s] / f'assets/videos/{mode}_Assistance_{s}.mp4' for s in ('OFF', 'ON')]
                paths.append(folder / mode / f'Comparison/{mode}_Assistance_OFF_vs_ON.mp4')
                saved_videos = {v['path']: v['sha256'] for v in saved['videos']}
                if set(saved_videos) != {str(path.relative_to(root)) for path in paths}:
                    raise RuntimeError('Checkpoint expected video paths differ')
                for index, path in enumerate(paths):
                    sha = digest(path)
                    if sha != saved_videos[str(path.relative_to(root))]:
                        raise RuntimeError('Video hash differs from exporter checkpoint')
                    proof = read(path.with_suffix('.verification.json'))
                    width = 2560 if index == 2 else 1280
                    if proof.get('passed') is not True or proof.get('decoded_frames') != 201 or proof.get('dimensions') != [width, 1210] or abs(proof.get('fps', 0) - 10) > .01:
                        raise RuntimeError('Exporter verification sidecar failed')
                    states = ('OFF', 'ON') if index == 2 else (('OFF', 'ON')[index],)
                    error = verify_sources(read(path.with_suffix('.source_frames.json')), manifests, runs, states)
                    result = decode_video(path, width)
                    result.update(path=str(path.relative_to(root)), sha256=sha, source_alignment_max_s=error,
                                  verification_sha256=digest(path.with_suffix('.verification.json')),
                                  source_mapping_sha256=digest(path.with_suffix('.source_frames.json')))
                    if digest(path) != sha:
                        raise RuntimeError('Media changed during independent check')
                    report['videos'].append(result)
                    report['checked_videos'] += 1
                    checkpoint()
        unchanged_inputs(root, snapshot)
        report.update(status='MEDIA_INTEGRITY_PASSED_SCENE_REVIEW_PENDING', passed=True,
                      original_input_files_verified=len(snapshot))
    except Exception:
        report.update(status='FAILED', error=traceback.format_exc())
    checkpoint()
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--catalogue-sha256', default='88ed12426883f20bdfda16cd084ba7c8cb2781a981b33e27373579b991b5571f')
    args = parser.parse_args()
    result = check(args.root, args.catalogue_sha256)
    print(json.dumps({k: v for k, v in result.items() if k != 'videos'}, indent=2))
    sys.exit(0 if result['passed'] else 1)
