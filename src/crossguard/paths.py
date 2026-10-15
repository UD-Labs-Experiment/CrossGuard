"""Active runtime paths; generated evidence is never a Git source artifact."""
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = PROJECT_ROOT / 'src' / 'results'
CARLA_LOCK_ROOT = PROJECT_ROOT / 'src' / 'results' / 'locks'
STUDY_ROOT = PROJECT_ROOT / 'Evidence' / 'Study_01_Town10HD'
