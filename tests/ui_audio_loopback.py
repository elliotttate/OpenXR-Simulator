"""Speaker-output recording and waveform matching; never opens a microphone."""
import sys
import wave
from pathlib import Path

import numpy as np


class OutputRecording:
    def __init__(self, dependencies, destination):
        sys.path.insert(0, str(dependencies))
        import pyaudiowpatch as audio
        self.audio, self.destination = audio, Path(destination)
        self.manager = audio.PyAudio()
        self.tracks = []
        for device in self.manager.get_loopback_device_info_generator():
            assert device["isLoopbackDevice"], "Only speaker loopback endpoints are allowed"
            track = dict(device=device, blocks=[])

            def callback(data, count, timing, status, track=track):
                track["blocks"].append(data)
                return None, audio.paContinue

            stream = self.manager.open(format=audio.paInt16, channels=int(device["maxInputChannels"]),
                rate=int(device["defaultSampleRate"]), input=True, input_device_index=device["index"],
                frames_per_buffer=512, stream_callback=callback)
            track["stream"] = stream
            self.tracks.append(track)
        assert self.tracks, "No WASAPI speaker loopback endpoint is available"

    def finish(self, clips):
        reports = []
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            for index, track in enumerate(self.tracks):
                track["stream"].stop_stream()
                track["stream"].close()
                device = track["device"]
                channels, rate = int(device["maxInputChannels"]), int(device["defaultSampleRate"])
                pcm = b"".join(track["blocks"])
                path = self.destination.with_name(self.destination.stem + f"-{index}.wav")
                with wave.open(str(path), "wb") as wav:
                    wav.setnchannels(channels)
                    wav.setsampwidth(2)
                    wav.setframerate(rate)
                    wav.writeframes(pcm)
                samples = np.frombuffer(pcm, dtype=np.int16).reshape(-1, channels).astype(float) / 32768
                reports.append(dict(device=device["name"], file=str(path), rate=rate,
                    seconds=len(samples)/rate, peak=float(np.max(np.abs(samples))) if len(samples) else 0,
                    matches={name: match_clip(samples, rate, clip) for name, clip in clips.items()}))
        finally:
            self.manager.terminate()
        return reports


def match_clip(samples, rate, clip):
    with wave.open(str(clip), "rb") as wav:
        assert wav.getsampwidth() == 2
        original_rate = wav.getframerate()
        reference = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16).reshape(-1, wav.getnchannels()).mean(axis=1) / 32768
    energy = np.cumsum(reference * reference)
    # The original two-second click has a long quiet tail. Match its audible
    # attack/decay, preserving all original samples in the shipping asset.
    end = min(len(reference), int(np.searchsorted(energy, energy[-1] * .999)) + 128)
    reference = reference[:end]
    reference = np.interp(np.arange(int(len(reference)*rate/original_rate))*original_rate/rate,
                          np.arange(len(reference)), reference)
    signal = samples.mean(axis=1) if samples.ndim == 2 else samples
    if len(signal) < len(reference) or not len(reference):
        return dict(correlation=0, gain=0, offset_seconds=0)
    reference = reference-reference.mean()
    signal = signal-signal.mean()
    size = 1 << (len(signal)+len(reference)-1).bit_length()
    product = np.fft.irfft(np.fft.rfft(signal,size)*np.fft.rfft(reference[::-1],size),size)
    dots = product[len(reference)-1:len(signal)]
    accumulated = np.concatenate(([0.0],np.cumsum(signal*signal)))
    windows = accumulated[len(reference):]-accumulated[:-len(reference)]
    reference_energy = np.sum(reference*reference)
    score = dots/np.sqrt(np.maximum(windows*reference_energy,1e-24))
    index = int(np.argmax(score))
    return dict(correlation=float(score[index]), gain=float(dots[index]/reference_energy),
                offset_seconds=index/rate, template_seconds=len(reference)/rate)
