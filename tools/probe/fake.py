"""In-process Link replacement driven by a script of payload -> raw reply bytes."""

from __future__ import annotations

from tools.probe.frames import Frame, build, split_frames


class FakeLink:
    def __init__(
        self,
        script: dict[bytes, list[bytes]],
        *,
        unsolicited: dict[bytes, list[bytes]] | None = None,
        strict_request_response: bool = False,
    ) -> None:
        self.script = script
        self.unsolicited = unsolicited or {}
        self.strict = strict_request_response
        self.sent: list[bytes] = []
        self._outstanding: bytes | None = None
        self._pending = b"".join(self.unsolicited.get(b"", []))

    def begin(self, payload: bytes) -> None:
        if self.strict and self._outstanding is not None:
            raise RuntimeError(f"outstanding command {self._outstanding!r} not yet collected")
        self.sent.append(build(payload))
        self._outstanding = payload
        raw = b"".join(self.script.get(payload, []))
        raw += b"".join(self.unsolicited.get(payload, []))
        self._pending += raw

    def exchange(self, payload: bytes, *, window: float) -> list[Frame]:
        self.begin(payload)
        return self.collect(window=window)

    def collect(self, *, window: float) -> list[Frame]:
        frames, self._pending = split_frames(self._pending)
        self._outstanding = None
        return frames
