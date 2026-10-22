import os

os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
for key, value in {
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
}.items():
    os.environ[key] = value
for key in ("ARTEL_TASK", "ARTEL_STAGE", "ARTEL_ROUND", "ARTEL_CHILD"):
    os.environ.pop(key, None)

# The suite and every child it starts are the first OOM victims on a shared machine (Task 45).
from artel import platform as _platform  # noqa: E402

_platform.prefer_oom_kill()
