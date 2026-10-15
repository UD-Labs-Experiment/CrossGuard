"""Rename the visible Town10HD study folders without rewriting captured records.

The command is deliberately explicit: it only touches directories directly
under Evidence/Study_01_Town10HD/Theme_* and the study's relative-path plans,
configs, and Markdown navigation. Raw result JSON, requested/effective
capture configs, provenance, Failed, and diagnostics are left unchanged.

Run with ``--apply`` to perform the rename. Without it, the command only
prints the planned moves and validates their targets.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path


THEMES = {
    "Theme_01": ("Theme_01_Crossing_Location_and_Occlusion", {
        "Test_003": "Test_003_Morning_Bus_Westbound_Crossing",
        "Test_004": "Test_004_Noon_Truck_Southbound_Crossing",
    }),
    "Theme_02": ("Theme_02_Daylight_and_Weather", {
        "Test_005": "Test_005_Afternoon_Rain",
        "Test_006": "Test_006_Overcast_Visibility",
    }),
    "Theme_03": ("Theme_03_Encounter_Difficulty_and_Secondary_Collisions", {
        "Test_001": "Test_001_Close_Following_Synchronized_Encounter",
        "Test_002": "Test_002_Marginal_Stopping_Distance",
    }),
    "Theme_04": ("Theme_04_Communication_Failures_and_Redundancy", {
        "Test_007": "Test_007_Separate_Channel_Outages",
        "Test_008": "Test_008_All_Channel_Outage_Delayed_Recovery",
    }),
    "Theme_05": ("Theme_05_Missing_Observations_and_Physical_Occlusion", {
        "Test_009": "Test_009_Brief_Missing_Camera_Observations",
        "Test_010": "Test_010_Parked_Occluder_Observation_Gap",
    }),
    "Theme_06": ("Theme_06_Waiting_and_Retreating_Nuisance_Responses", {
        "Test_011": "Test_011_Waiting_Pedestrian",
        "Test_012": "Test_012_Retreating_Pedestrian",
    }),
}

NAMES = {
    "Test_003": "Morning bus at westbound crossing",
    "Test_004": "Noon truck at southbound crossing",
    "Test_005": "Afternoon rain",
    "Test_006": "Overcast visibility",
    "Test_001": "Close-following synchronized encounter",
    "Test_002": "Marginal stopping distance",
    "Test_007": "Separate radio/cellular/optical outages (sequential windows)",
    "Test_008": "All-channel outage and delayed recovery",
    "Test_009": "Brief missing camera observations",
    "Test_010": "Additional parked occluder plus observation gap",
    "Test_011": "Waiting pedestrian",
    "Test_012": "Retreating pedestrian",
}


def study_root() -> Path:
    return Path(__file__).resolve().parents[2] / "Evidence" / "Study_01_Town10HD"


def inside(root: Path, path: Path) -> Path:
    root = root.resolve()
    resolved = path.resolve()
    if root not in resolved.parents:
        raise RuntimeError(f"path escapes Study_01_Town10HD: {path}")
    return resolved


def all_moves(root: Path) -> list[tuple[Path, Path]]:
    moves: list[tuple[Path, Path]] = []
    for old_theme, (new_theme, tests) in THEMES.items():
        old_theme_path = inside(root, root / old_theme)
        new_theme_path = inside(root, root / new_theme)
        moves.append((old_theme_path, new_theme_path))
        for old_test, new_test in tests.items():
            moves.append((old_theme_path / old_test, new_theme_path / new_test))
    return moves


def validate(root: Path, apply: bool) -> list[tuple[Path, Path]]:
    root = inside(root.parent, root)
    if root.name != "Study_01_Town10HD":
        raise RuntimeError(f"unexpected study root: {root}")
    moves = all_moves(root)
    for source, target in moves:
        inside(root, source)
        inside(root, target)
        if not source.exists():
            raise RuntimeError(f"missing source folder: {source}")
        if target.exists():
            raise RuntimeError(f"target already exists: {target}")
    # Explicitly prove that protected sibling trees are outside the move set.
    for protected in (root / "Failed", root / "diagnostics"):
        if not protected.exists():
            raise RuntimeError(f"protected tree missing: {protected}")
    if any(source.parts[len(root.parts)] in {"Failed", "diagnostics"} for source, _ in moves):
        raise RuntimeError("move set overlaps protected evidence")
    if not apply:
        return moves
    return moves


def rename_transaction(root: Path, moves: list[tuple[Path, Path]]) -> None:
    token = uuid.uuid4().hex[:10]
    theme_moves = [(source, target) for source, target in moves if source.parent == root]
    test_moves = [(source, target) for source, target in moves if source.parent != root]
    temporary: list[tuple[Path, Path]] = []
    try:
        for source, _ in theme_moves:
            temp = root / f".__rename_{source.name}_{token}"
            source.rename(temp)
            temporary.append((temp, source))
        for temp, original in temporary:
            target = next(target for source, target in theme_moves if source == original)
            temp.rename(target)
        for source, target in test_moves:
            # The theme has moved; derive its new parent from the mapping.
            old_theme = source.parent.name
            new_theme = THEMES[old_theme][0]
            moved_source = root / new_theme / source.name
            moved_target = root / new_theme / target.name
            moved_source.rename(moved_target)
    except Exception:
        # Best-effort rollback for an interrupted local rename.
        for source, target in reversed(test_moves):
            new_theme = THEMES[source.parent.name][0]
            moved_target = root / new_theme / target.name
            moved_source = root / new_theme / source.name
            if moved_target.exists() and not moved_source.exists():
                moved_target.rename(moved_source)
        for temp, original in reversed(temporary):
            new_theme = next(target for source, target in theme_moves if source == original)
            if new_theme.exists() and not temp.exists():
                new_theme.rename(temp)
            if temp.exists() and not original.exists():
                temp.rename(original)
        raise


def rename_relative(value: str) -> str:
    parts = value.replace("\\", "/").split("/")
    if len(parts) != 2:
        return value
    theme, test = parts
    if theme not in THEMES or test not in THEMES[theme][1]:
        return value
    return f"{THEMES[theme][0]}/{THEMES[theme][1][test]}"


def update_json(path: Path, only_root_test_config: bool = False) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    changed = False
    if isinstance(data, dict) and isinstance(data.get("relative_path"), str):
        updated = rename_relative(data["relative_path"])
        changed |= updated != data["relative_path"]
        data["relative_path"] = updated
    if isinstance(data, dict) and isinstance(data.get("tests"), list):
        for entry in data["tests"]:
            if isinstance(entry, dict) and isinstance(entry.get("relative_path"), str):
                updated = rename_relative(entry["relative_path"])
                changed |= updated != entry["relative_path"]
                entry["relative_path"] = updated
    if changed:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def update_docs(root: Path) -> None:
    replacements: dict[str, str] = {}
    for old_theme, (new_theme, tests) in THEMES.items():
        replacements[old_theme] = new_theme
        for old_test, new_test in tests.items():
            replacements[f"{old_theme}/{old_test}"] = f"{new_theme}/{new_test}"
    for path in root.rglob("*.md"):
        if any(part in {"Failed", "diagnostics"} for part in path.relative_to(root).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # Some generated reports use a legacy code page; leave those
            # capture artifacts byte-for-byte unchanged.
            continue
        updated = text
        for old, new in sorted(replacements.items(), key=lambda item: -len(item[0])):
            updated = updated.replace(old, new)
        if updated != text:
            path.write_text(updated, encoding="utf-8")


def update_relocation(root: Path) -> None:
    path = root / "folder_relocation.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["active_package"] = rename_relative("Theme_03/Test_001")
    data.setdefault("named_study_paths", {})
    for old_theme, (new_theme, tests) in THEMES.items():
        data["named_study_paths"][old_theme] = new_theme
        for old_test, new_test in tests.items():
            data["named_study_paths"][f"{old_theme}/{old_test}"] = f"{new_theme}/{new_test}"
    data["raw_evidence_note"] = (
        "Requested/effective configurations retain capture-time paths unchanged. "
        "Use current named relative locations and asset resolver. No raw pictures, "
        "metrics or videos were regenerated."
    )
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_named_docs(root: Path) -> None:
    theme_titles = {
        "Theme_01": "Crossing Location and Occlusion",
        "Theme_02": "Daylight and Weather",
        "Theme_03": "Encounter Difficulty and Secondary Collisions",
        "Theme_04": "Communication Failures and Redundancy",
        "Theme_05": "Missing Observations and Physical Occlusion",
        "Theme_06": "Waiting and Retreating Nuisance Responses",
    }
    lines = [
        "# Town10HD theme and test names",
        "",
        "Local labels are `Test 1` and `Test 2` within each theme. Stable folder "
        "identifiers retain the `Test_XXX` IDs used by the frozen catalogue.",
        "",
        "| Theme | Name | Test 1 | Test 2 |",
        "| --- | --- | --- | --- |",
    ]
    for old_theme, (new_theme, tests) in THEMES.items():
        old_test_names = list(tests)
        first, second = old_test_names
        lines.append(
            f"| {old_theme.replace('Theme_', 'T')} | {theme_titles[old_theme]} | "
            f"[`{tests[first]}`]({new_theme}/{tests[first]}) — {NAMES[first]} | "
            f"[`{tests[second]}`]({new_theme}/{tests[second]}) — {NAMES[second]} |"
        )
    (root / "theme_test_names.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for old_theme, (new_theme, tests) in THEMES.items():
        test_ids = list(tests)
        body = [f"# {old_theme.replace('Theme_', 'T')} — {theme_titles[old_theme]}", ""]
        body.append(f"- Test 1: [`{tests[test_ids[0]]}`]({tests[test_ids[0]]}) — {NAMES[test_ids[0]]}")
        body.append(f"- Test 2: [`{tests[test_ids[1]]}`]({tests[test_ids[1]]}) — {NAMES[test_ids[1]]}")
        (root / new_theme / "README.md").write_text("\n".join(body) + "\n", encoding="utf-8")


def update_root_readme(root: Path) -> None:
    path = root / "README.md"
    text = path.read_text(encoding="utf-8")
    marker = "\nThe named study layout is indexed in [theme and test names](theme_test_names.md)."
    if marker.strip() not in text:
        text = text.rstrip() + marker + "\n"
    path.write_text(text, encoding="utf-8")


def update_metadata(root: Path) -> None:
    for filename in ("study_48_plan.json", "batch_12_plan.json"):
        update_json(root / filename)
    for old_theme, (new_theme, tests) in THEMES.items():
        for old_test, new_test in tests.items():
            config = root / new_theme / new_test / "test_config.json"
            update_json(config, only_root_test_config=True)
    update_relocation(root)
    update_docs(root)
    write_named_docs(root)
    update_root_readme(root)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="perform the validated rename and metadata update")
    args = parser.parse_args()
    root = study_root()
    # Use the current frozen catalogue, including an already-applied descriptive
    # theme rename. Local folder numbers and stable capture IDs are distinct.
    plan_path=root/'study_48_plan.json'
    plan=json.loads(plan_path.read_text())
    mapping={};moves=[];rows=[]
    for old_theme,(named_theme,tests) in THEMES.items():
        for local,(stable_id,global_name) in enumerate(tests.items(),1):
            entry=next(c for c in plan['tests'] if c['test']==stable_id)
            source=inside(root,root/entry['relative_path'])
            local_name=f'Test_{local:03}_'+global_name.split('_',2)[2]
            target=inside(root,root/named_theme/local_name)
            if not source.exists():raise RuntimeError(f'Missing current test: {source}')
            if target.exists() and source!=target:raise RuntimeError(f'Target occupied: {target}')
            mapping[entry['relative_path']]=target.relative_to(root).as_posix()
            mapping[f'{old_theme}/{stable_id}']=target.relative_to(root).as_posix()
            moves.append((source,target));rows.append((old_theme,named_theme,local,stable_id,local_name,target))
    for source, target in moves:
        print(f"{source.relative_to(root)} -> {target.relative_to(root)}")
    if args.apply:
        for source,target in moves:
            if source!=target:source.rename(target)
        for filename in ('study_48_plan.json','batch_12_plan.json'):
            path=root/filename;data=json.loads(path.read_text())
            for entry in data['tests']:
                entry['relative_path']=mapping.get(entry['relative_path'],entry['relative_path'])
            path.write_text(json.dumps(data,indent=2),encoding='utf8')
        lines=['# Town10HD theme and test names','',
          'Folder numbers start at 1 within each theme. Stable global IDs remain in captured records.','',
          '| Theme | Local test | Stable ID | Visible folder |','|---|---|---|---|']
        for old_theme,named_theme,local,stable_id,local_name,target in rows:
            cfg_path=target/'test_config.json';cfg=json.loads(cfg_path.read_text())
            cfg['relative_path']=target.relative_to(root).as_posix()
            cfg_path.write_text(json.dumps(cfg,indent=2),encoding='utf8')
            lines.append(f'| {old_theme} | Test {local} | {stable_id} | [{local_name}]({named_theme}/{local_name}) |')
        for named_theme in dict.fromkeys(row[1] for row in rows):
            body=['# '+named_theme.replace('_',' '),'']
            body += [f'- Test {local}: [{NAMES[stable_id]}]({local_name}) (stable ID {stable_id})' for _,theme,local,stable_id,local_name,_ in rows if theme==named_theme]
            (root/named_theme/'README.md').write_text('\n'.join(body)+'\n',encoding='utf8')
        (root/'theme_test_names.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
        body=['# Current Town10HD robustness study','',
          'The active study contains twelve named tests and 48 planned completed simulations: Automatic OFF/ON and Retrofit OFF/ON for each test.','',
          '[Theme and test folder index](theme_test_names.md) · [Frozen configurations](study_48_plan.json) · [Efficiency report](efficiency_report.md)','',
          'Each theme contains Test_001 and Test_002 with descriptive names. The global test ID in each configuration and image label preserves capture provenance.','',
          'Within each test: Automatic/OFF and Automatic/ON hold numeric evidence; Automatic/assets/OFF and ON hold pictures and event sets. Comparison videos are under Automatic/assets. Retrofit follows the same layout.','',
          'Failed contains preserved earlier evidence and failed/interrupted attempts; diagnostics contains separate sudden-braking evidence. Neither contributes to the 48 completed-study-run total. Earlier cleanup has not been repeated.','',
          'Raw capture-time paths are immutable. Use [current relocation mapping](folder_relocation_named.json) for the visible folders.','']
        body += [f'- [{theme.replace("_"," ")}]({theme}/README.md)' for theme in dict.fromkeys(row[1] for row in rows)]
        (root/'README.md').write_text('\n'.join(body)+'\n',encoding='utf8')
        (root/'folder_relocation_named.json').write_text(json.dumps({'paths':mapping,'raw_records_preserved':True},indent=2),encoding='utf8')
        print("Applied named Town10HD evidence layout.")
    else:
        print("Dry run only; pass --apply to rename.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
