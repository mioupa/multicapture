"""Common parts for the mac-spike runners (stdlib only)."""
import os
import sys

SPIKE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(os.path.dirname(SPIKE_DIR))
MEDIA_DIR = os.path.join(SPIKE_DIR, "media")
RESULTS_DIR = os.path.join(SPIKE_DIR, "results")
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
