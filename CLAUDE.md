# railctl

A CLI that drives a YaMoRC YD7010 command station over XpressNet on a USB serial port, and reads and writes CVs on ZIMO decoders. macOS only. Python 3.11+, `typer` as the sole runtime dependency, everything else stdlib.

## The rule this project exists to enforce

**A capability must never be recorded as absent because the instrument measuring it was broken.** It happened four times while probing the real hardware, and each time the fix was in the tool, not the station.

Three outcomes stay distinguishable end to end — in the dataclass, in the JSON (`true` / `false` / `null`), in the human text (`yes` / `no` / `unknown`) and in the exit code:

- **true** — measured working.
- **false** — the station said so. Only a `61 82` *Unsupported* reply earns this.
- **unknown** — no answer, an unparsed answer, or never tried.

Silence is `unknown`. On this hardware a POM CV read returns nothing at all, and that is why `pom_read` is `null` rather than `false` — see `docs/probe-results.md`, section R1, and the one deliberate exception the doctor makes, which records its own provenance.

When you touch a parser, an error path or a capability field, ask which of the three a caller will see, and whether a defect in your own code could produce the wrong one.

## Commands

Dependencies are managed by **uv**. Never `pip`.

```
uv sync --frozen          # what CI does
uv run pytest             # the suite
uv run ruff check .
uv run ruff format --check .
uv run pytest -m hardware -s      # needs the YD7010 attached; deselected by default
```

**Never add `-q` to a pytest command.** `addopts` already carries it, and a second one raises the quiet level to 2, at which pytest prints no summary line — so the count you were about to compare against never appears.

## Things that will bite you

- **Layering is enforced mechanically** by `tests/test_layering.py`, which greps rather than imports, so it also covers code no test exercises. `station/` and `cli/` may not name framing bytes, port names, `socket`, or any word starting with `tty` — not even inside a comment. All CV arithmetic lives in `xbus/cv.py`. Only `errors.py` defines a class whose name ends in `Error`, `Exception` or `Timeout`. When a guard fires on your code, fix the code; narrowing the guard to silence one false positive once blinded it to 21 classes.
- **Restoring a file with `git checkout` leaves stale bytecode.** Same size, same-second mtime, so Python keeps running the `.pyc` built from the broken version. After any deliberate break-and-restore, re-run with `PYTHONDONTWRITEBYTECODE=1`.
- **Hypothesis derandomizes itself when it sees `CI` in the environment**, so a CI job that names no profile runs `default` with a value never declared. Every CI job sets `HYPOTHESIS_PROFILE` explicitly.
- **`FakeTransport` raises on an unscripted request rather than timing out.** A test that wants silence must script it: `expect(request, reply=b"")`. Silence by omission is a different thing.
- **`transport.written` holds framed bytes**, prefix included, because that is what `Link.write()` was given. Comparing it against a bare telegram silently never matches.
- **CV numbering conventions disagree and all four live in `xbus/cv.py`.** POM (`E6 30`) and Z21 (`23 11`) are zero-based; Lenz direct (`22 15`) and extended (`22 18`–`1B`) are one-based. A request can be zero-based and its echo one-based in the same exchange.
- **`tools/probe/` is frozen M1 output.** Its mutation baseline in `docs/test-hardening.md` was measured against that exact AST; do not reformat it for a lint rule.
- **The port's baud rate goes through `getattr(termios, f"B{rate}")`, never the literal.** On Darwin the constant equals the literal, so the literal worked locally and failed on every CI run — Linux speed constants are small indices and `tcsetattr` rejects the literal with `EINVAL`.

## Checking CI

Read the run's conclusion, not a shell exit status:

```
gh run view <id> --json conclusion --jq '.conclusion'
gh pr checks <n>
```

**Never pipe `gh run watch --exit-status` into `tail` or `head`.** A pipeline exits with the status of its last command, so the pipe returns 0 whatever the run did — and a red build gets reported as green. This has happened here; a whole milestone merged with `main` red underneath it.

The same trap applies to any check where the interesting result is on stdout and the verdict is in the exit status.

## Hardware

The probe and the tools must **never write a decoder CV** unless the change explicitly asks for it, and a write must be read back to be believed — a station echo proves the station produced a value, not that the decoder kept it.

`railctl` auto-detects the port; the CDC interface index picks the bus (1 LocoNet, 3 XpressNet, 5 telemetry). Do not read `TC` from the telemetry stream to decide whether a locomotive is present: it reports 0 mA for a standing sound decoder that is demonstrably alive.

## Where things are written down

- `docs/probe-results.md` — what the hardware actually does, measured, with the request and reply bytes for each claim. This is the authority; a documentation summary that contradicts it is wrong.
- `docs/superpowers/specs/` — the approved design.
- `docs/superpowers/plans/` — implementation plans, one per milestone group. A plan carries the exact code and expected output for each step, because it is executed by someone who sees one task at a time.
- `docs/test-hardening.md` — the mutation baseline for the frozen probe.

Commits use Conventional Commits.
