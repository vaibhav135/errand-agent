"""Suite-wide safety: tests never call a real model API.

Building a real model (to check configuration) is fine; any actual request to a
non-test model fails the test instead of spending money or needing a network.
"""

from pydantic_ai import models

models.ALLOW_MODEL_REQUESTS = False
