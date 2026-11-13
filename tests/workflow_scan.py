"""The one pattern that says a workflow line runs pytest.

Both `test_assert_tests_ran.py` and `test_ci_scope.py` scan the workflows for it.
Two copies of a pattern kept in step by a comment drift the first time one is
fixed and the other is not, and either drifting skips a CI leg in silence.
"""

import re

#: `uv run`, anything uv itself takes, then `pytest`. **Whatever sits between is
#: the point.** Matching the literal `uv run pytest` skipped every `uv run --group
#: raster pytest …` in silence, and that is the typesetting job: the one leg whose
#: suite skips itself when its group is absent. A pattern spelling out which
#: options may appear skips the next option it does not know in the same silence,
#: so nothing between the two words is constrained.
PYTEST_INVOCATION = re.compile(r"\buv run\b.*?\bpytest\b")
