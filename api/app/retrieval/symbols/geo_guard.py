"""Place-name guard for bare ticker tagging (docs/35 M1).

"TP.HCM" (Ho Chi Minh City) contains the bare token ``HCM`` — also the HOSE
ticker of HSC securities. The ``.`` before ``HCM`` is a regex word boundary,
so the bare-token pass tagged every city headline as ``HCM``. Masking the
place forms with spaces of the SAME length keeps every other character offset
intact, so evidence offsets still point into the original text.

``HCM`` stays taggable through strong evidence only: ``HCM:`` prefix,
``HOSE: HCM``, "cổ phiếu HCM", or the curated alias "Chứng khoán HSC"/"HSC".
"""

from __future__ import annotations

import re

_GEO_RE = re.compile(
    r"(?i)\bTP\.?\s?HCM\b|\bTPHCM\b|\bTP\.?\s?Hồ\s+Chí\s+Minh\b|thành\s+phố\s+hồ\s+chí\s+minh"
    r"|\bHCMC\b|\bHCM\s+City\b"
)


def mask_geo(src: str) -> str:
    """Replace place-name spans with spaces of equal length (offset-preserving)."""
    if not src:
        return src
    return _GEO_RE.sub(lambda m: " " * len(m.group(0)), src)
