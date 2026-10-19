import os

os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
for key, value in {
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
}.items():
    os.environ[key] = value
for key in ("ARTEL_TASK", "ARTEL_STAGE", "ARTEL_ROUND"):
    os.environ.pop(key, None)
