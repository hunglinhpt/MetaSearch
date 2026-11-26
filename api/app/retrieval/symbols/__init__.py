"""Symbol-tagging helpers for news/event entity extraction (docs/35 M1).

Small, pure, separately tested modules used by ``retrieval.events``:

- ``geo_guard``   — mask place names ("TP.HCM") before bare-token tagging.
- ``index_codes`` — market index codes never tag as equities; reported apart.
- ``evidence``    — per-symbol evidence records (method, span, offset, field).
"""
