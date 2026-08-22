"""railctl - drive a YaMoRC YD7010 and read or write ZIMO decoder CVs over XpressNet.

`__version__` is the single source of truth, and every part of the tool that
reports a version reads THIS attribute rather than the installed distribution's
metadata. `[tool.hatch.version]` in pyproject.toml reads this file, so a BUILT
distribution carries the same number - but an installed one goes stale the moment
this line changes in a checkout nobody has reinstalled. That is not hypothetical:
on the 0.2.0 bump `railctl version` said 0.2.0 while `railctl backup` stamped every
file `railctl 0.1.0`, because that one call site read the metadata instead.
"""

__version__ = "0.2.0"
