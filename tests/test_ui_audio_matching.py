"""Independent positive/negative controls for the speaker waveform oracle."""
import argparse
import tempfile
import wave
from pathlib import Path

import numpy as np
from ui_audio_loopback import match_clip


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--fox',type=Path,required=True)
    args=parser.parse_args()
    rng=np.random.default_rng(41027)
    for name in ('TromboneHover','TromboneClick'):
        clip=args.fox/'Flatout/Data/Assets/Sounds/UI'/(name+'.wav')
        with wave.open(str(clip),'rb') as wav:
            source_rate=wav.getframerate()
            source=np.frombuffer(wav.readframes(wav.getnframes()),dtype=np.int16).reshape(-1,wav.getnchannels()).mean(axis=1)/32768
        rate=48000
        reference=np.interp(np.arange(int(len(source)*rate/source_rate))*source_rate/rate,np.arange(len(source)),source)
        output=rng.normal(0,.0002,int(rate*3))
        start=int(.47*rate)
        output[start:start+len(reference)]+=.65*reference
        positive=match_clip(output,rate,clip)
        negative=match_clip(rng.normal(0,.001,int(rate*3)),rate,clip)
        silence=match_clip(np.zeros(int(rate*3)),rate,clip)
        assert positive['correlation']>.98 and abs(positive['gain']-.65)<.01,positive
        assert abs(positive['offset_seconds']-.47)<.001,positive
        assert negative['correlation']<.2 and silence['correlation']==0,(negative,silence)
        print(name,'resampled positive / wrong waveform / silence controls PASS',flush=True)


if __name__=='__main__':
    main()
