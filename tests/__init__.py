"""Offline regression tests for Screenwriter Studio."""
import os
import tempfile

# Tests must never read the developer's saved model choices.
os.environ.setdefault("STUDIO_SETTINGS_PATH", os.path.join(tempfile.mkdtemp(prefix="studio-test-"), "studio_settings.json"))
