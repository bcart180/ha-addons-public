"""μ-law (PCMU) decode and WAV writing for Twilio Media Streams.

Twilio inbound audio is G.711 μ-law at 8 kHz. xAI Realtime accepts that codec
as audio/pcmu, so the live bridge does not convert. Files on disk are PCM16
WAV so Home Assistant's media browser and ordinary players can open them.
"""
import struct

MULAW_RATE = 8000
# 30 minutes of 8 kHz μ-law. Longer calls stop growing the buffer.
MAX_MULAW_BYTES = MULAW_RATE * 60 * 30


def mulaw_byte_to_pcm(u_val: int) -> int:
    """G.711 μ-law byte to a 16-bit linear sample (Sun/SoX ulaw2linear)."""
    u = (~u_val) & 0xFF
    t = ((u & 0x0F) << 3) + 0x84
    t <<= (u & 0x70) >> 4
    return (0x84 - t) if (u & 0x80) else (t - 0x84)


def mulaw_to_pcm16(data: bytes) -> bytes:
    out = bytearray(len(data) * 2)
    for i, raw in enumerate(data):
        struct.pack_into("<h", out, i * 2, mulaw_byte_to_pcm(raw))
    return bytes(out)


def pcm16_wav(pcm: bytes, rate: int = MULAW_RATE) -> bytes:
    """Mono 16-bit little-endian WAV. Header is 44 bytes."""
    channels = 1
    bits = 16
    byte_rate = rate * channels * bits // 8
    block_align = channels * bits // 8
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(pcm),
        b"WAVE",
        b"fmt ",
        16,
        1,
        channels,
        rate,
        byte_rate,
        block_align,
        bits,
        b"data",
        len(pcm),
    )
    return header + pcm


def mulaw_wav(data: bytes, rate: int = MULAW_RATE) -> bytes:
    return pcm16_wav(mulaw_to_pcm16(data), rate)


MULAW_SILENCE = 0xFF
# A caller timestamp further ahead than this is treated as a bad clock and the
# frame is appended instead of punching a long silence hole.
MAX_GAP_SAMPLES = MULAW_RATE * 30
_MULAW_CLIP = 32635


def ms_to_sample(ms: float) -> int:
    """8 kHz sample index for a stream-relative millisecond timestamp."""
    if ms <= 0:
        return 0
    return int(round(ms * MULAW_RATE / 1000.0))


def pcm16_to_mulaw(sample: int) -> int:
    """16-bit linear sample to G.711 μ-law (inverse of mulaw_byte_to_pcm)."""
    if sample < 0:
        sign = 0x80
        sample = -sample
    else:
        sign = 0
    if sample > _MULAW_CLIP:
        sample = _MULAW_CLIP
    sample += 0x84
    exponent = 7
    mask = 0x4000
    while exponent > 0 and (sample & mask) == 0:
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa)) & 0xFF


def mix_mulaw(caller: bytes, assistant: bytes) -> bytes:
    """Sum two μ-law timelines into one μ-law buffer.

    The shorter side is μ-law silence. A silent sample (0xFF) leaves the other
    side's byte unchanged, so a stretch with only one talker is not re-encoded.
    Where both are non-silent the linear samples are added and clamped to
    int16, then encoded again. Overlap can clip.
    """
    n = max(len(caller), len(assistant))
    if n == 0:
        return b""
    out = bytearray(n)
    caller_n = len(caller)
    assistant_n = len(assistant)
    for i in range(n):
        c = caller[i] if i < caller_n else MULAW_SILENCE
        a = assistant[i] if i < assistant_n else MULAW_SILENCE
        if c == MULAW_SILENCE:
            out[i] = a
        elif a == MULAW_SILENCE:
            out[i] = c
        else:
            mixed = mulaw_byte_to_pcm(c) + mulaw_byte_to_pcm(a)
            if mixed > 32767:
                mixed = 32767
            elif mixed < -32768:
                mixed = -32768
            out[i] = pcm16_to_mulaw(mixed)
    return bytes(out)


class MulawTimeline:
    """μ-law samples placed on a stream-relative timeline.

    Gaps render as silence (0xFF). Later writes at the same index win.
    `truncate` drops every sample at or after an index (Twilio `clear`).
    """

    def __init__(self, max_bytes: int = MAX_MULAW_BYTES):
        self.max_bytes = max_bytes
        self.segments = []  # list[(start_sample, bytearray)]
        self.voiced = 0
        self.truncated = False

    def end_sample(self) -> int:
        end = 0
        for start, data in self.segments:
            end = max(end, start + len(data))
        return min(end, self.max_bytes)

    def place(self, start: int, data: bytes) -> int:
        """Write `data` at `start`. Returns samples kept."""
        if not data:
            return 0
        if start < 0:
            data = data[-start:]
            start = 0
            if not data:
                return 0
        room = self.max_bytes - start
        if room <= 0:
            self.truncated = True
            return 0
        if len(data) > room:
            data = data[:room]
            self.truncated = True
        if self.segments:
            last_start, last = self.segments[-1]
            if last_start + len(last) == start:
                last.extend(data)
                self.voiced += len(data)
                return len(data)
        buf = bytearray(data)
        self.segments.append((start, buf))
        self.voiced += len(buf)
        return len(buf)

    def truncate(self, sample: int) -> int:
        """Drop samples with index >= `sample`. Returns samples removed."""
        if sample < 0:
            sample = 0
        kept = []
        removed = 0
        for start, data in self.segments:
            end = start + len(data)
            if end <= sample:
                kept.append((start, data))
            elif start >= sample:
                removed += len(data)
            else:
                keep_n = sample - start
                removed += len(data) - keep_n
                kept.append((start, data[:keep_n]))
        self.segments = kept
        self.voiced = sum(len(data) for _, data in kept)
        return removed

    def render(self) -> bytes:
        """Contiguous μ-law from sample 0 through the last placed sample."""
        if not self.segments:
            return b""
        end = min(self.end_sample(), self.max_bytes)
        if end <= 0:
            return b""
        buf = bytearray(b"\xff" * end)
        for start, data in self.segments:
            if start >= end:
                continue
            n = min(len(data), end - start)
            buf[start:start + n] = data[:n]
        return bytes(buf)
