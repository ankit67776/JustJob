"""A fixed config for tests, so editing config.toml never breaks them."""
import tomllib

TEST_CONFIG = """
[profile]
summary = "Test candidate"
years_experience = 3
max_years_required = 4
skills = ["python", "pytorch", "llm", "fastapi", "react"]
target_roles = ["AI Engineer"]
avoid = []

[filters.title]
role_words = ["engineer", "engineers", "developer", "scientist"]
domain_words = ["software", "backend", "full stack", "frontend", "ml", "machine learning", "ai", "data",
                "research", "inference", "platform", "applied", "forward deployed"]
standalone = ["swe", "sde", "sde 1", "member of technical staff"]
exclude = ["staff", "lead", "principal", "manager", "intern", "iv", "sales", "solutions engineer",
           "recruiter", "ios", "android"]
exclude_unless_years_fit = ["senior", "sr", "iii"]

[filters.location]
onsite = ["*"]
remote = true
remote_regions = ["*"]

[ranking]
title_top = ["ai", "ml", "machine learning", "llm"]
title_next = ["full stack", "backend"]
remote_bonus = 10

[fetch]
workers = 2
registry = false
max_age_days = 30

[sources]

[ai]
enabled = true
provider = "none"
fallback = []
batch_size = 10
max_calls_per_run = 30
min_interval_seconds = 0
description_chars = 2500

[ai.providers]

[digest]
min_score = 10
max_items = 60

[notify]
"""


def make_cfg(**location) -> dict:
    cfg = tomllib.loads(TEST_CONFIG)
    cfg["filters"]["location"].update(location)
    return cfg
