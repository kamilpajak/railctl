# railctl

Drive a YaMoRC YD7010 and read or write ZIMO decoder CVs over XpressNet.

## Install

```sh
uv sync
```

## Status

M2 scaffolding, no protocol code yet. The package currently contains the version string, the
exception tree and the exit-code map. The `railctl` console script is declared but not runnable
until the CLI arrives in M6. What the hardware actually answers is recorded in
`docs/probe-results.md`.
