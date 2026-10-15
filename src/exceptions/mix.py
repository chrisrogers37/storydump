"""Allocation refusal shared without service import cycles."""

from src.exceptions.base import RefusalError


class MixInvalid(RefusalError):
    """The mix cannot be stored as sent. `reason` is one of: not_a_list ·
    empty_source · duplicate_source · bad_ratio · sum_not_one ·
    too_many_sources (from `normalize`) · unknown_source · all_off (from
    `set_mix`, against the connected folders)."""

    _prefix = "mix invalid"
