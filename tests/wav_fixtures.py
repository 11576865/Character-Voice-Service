"""Small real PCM WAV fixtures shared by adapter and API contract tests."""
import io
import wave


def make_pcm_wav(*, sample_rate=24000, channels=1, sample_width=2, frames=128):
    data = bytes((i * 17) % 256 for i in range(frames * channels * sample_width))
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(sample_width)
        output.setframerate(sample_rate)
        output.writeframes(data)
    return stream.getvalue()
