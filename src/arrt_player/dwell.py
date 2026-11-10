"""How long one slideshow item stays on the screen.

The owner's rule (2026-11-10): a still shows for the minimum. A clip of length
`D` plays the smallest whole number of times that reaches the minimum, and the
total is cut off at the maximum. So a two-minute clip with a 30 s minimum and a
60 s maximum shows for 60 s, and an 18 s clip with the same limits plays twice,
for 36 s. A clip whose second play would pass the maximum is cut partway
through, exactly as a long clip is.

**A length of zero or none is a still.** mpv reports `duration` 0.0 for an image
(measured on the Pi, mpv 0.40), and a clip whose length it cannot tell has no
loop count to compute; showing either for the minimum is the rule's own answer
for something that does not move.
"""

import math
from dataclasses import dataclass
from typing import Final

#: The shortest minimum the page accepts. Below this an item is gone before a
#: person across the room has looked up, and a listing of stills would churn the
#: cache and the e-paper label faster than either is worth.
FLOOR_SECONDS: Final[float] = 5.0

#: The longest maximum the page accepts: an hour on one item is a still life,
#: not a slideshow, and a typo of an extra zero is far likelier than intent.
CEILING_SECONDS: Final[float] = 3600.0


class DwellLimitsInvalid(ValueError):
    """The limits cannot describe a slideshow: out of range, or a minimum above the maximum."""


@dataclass(frozen=True)
class DwellLimits:
    """The owner's minimum and maximum, checked once so every item can trust them."""

    minimum: float
    maximum: float

    def __post_init__(self) -> None:
        problem = limits_problem(self.minimum, self.maximum)
        if problem is not None:
            raise DwellLimitsInvalid(problem)


def limits_problem(minimum: float, maximum: float) -> str | None:
    """Why a minimum and maximum cannot be used, in words for the settings page; None when they can."""
    if not (math.isfinite(minimum) and math.isfinite(maximum)):
        return "the minimum and maximum must be numbers of seconds"
    if minimum < FLOOR_SECONDS:
        return f"the minimum must be at least {FLOOR_SECONDS:g} seconds"
    if maximum > CEILING_SECONDS:
        return f"the maximum must be at most {CEILING_SECONDS:g} seconds"
    if minimum > maximum:
        return "the minimum must not be more than the maximum"
    return None


def dwell_seconds(duration: float | None, limits: DwellLimits) -> float:
    """How long an item of this length stays up: the minimum for a still, whole plays up to the maximum for a clip."""
    if duration is None or not math.isfinite(duration) or duration <= 0:
        return limits.minimum
    plays = math.ceil(limits.minimum / duration)
    return min(plays * duration, limits.maximum)
