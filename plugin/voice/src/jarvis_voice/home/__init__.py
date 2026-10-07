"""Home control: Apple TV, Sony Bravia, Tuya / Smart Life and Home Assistant from the voice helper.

The mod's ``home_control`` tool sends ``home`` commands to the helper, which
answers them with ``HomeService``. Device libraries are imported only when a
device of theirs is used. See docs/HOME.md.
"""

from .service import HomeService

__all__ = ["HomeService"]
