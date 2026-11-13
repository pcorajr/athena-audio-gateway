package application

import (
	"context"
	"os"

	"github.com/dharmab/skyeye/pkg/athena/bridge"
	"github.com/dharmab/skyeye/pkg/athena/mvp"
	"github.com/dharmab/skyeye/pkg/athena/transcript"
	"github.com/dharmab/skyeye/pkg/pcm"
	"github.com/dharmab/skyeye/pkg/simpleradio"
)

// runSRSMVP deliberately bypasses persona, GCI and the asynchronous legacy
// synthesis/transmit queues. No diagnostic audio or trace-content export.
func (a *Application) runSRSMVP(ctx context.Context) {
	a.srsMVPRunner().Run(ctx, a.srsClient.Receive())
}

func (a *Application) srsMVPRunner() *mvp.Runner {
	if client, ok := a.hermesBridge.(*bridge.MVPClient); ok {
		client.RequireProtocol15 = true
	}
	return &mvp.Runner{
		Unprompted: os.Getenv("ATHENA_UNPROMPTED") != "off",
		Transcript: transcript.Default(),
		Gate:       a.admissionGate, Bridge: a.hermesBridge, Radio: a.srsClient,
		MaxAge: a.athenaResponseMaxAge, MaxSpeech: a.athenaMaxSpeech,
		Recognize: func(ctx context.Context, audio simpleradio.Audio) (string, error) {
			if err := tryLock(ctx, a.recognizerLock); err != nil {
				return "", err
			}
			defer unlock(a.recognizerLock)
			// U30 capture goes ONLY to the private transcript sink, never the
			// recognizer's general logs or trace export.
			return a.recognizer.Recognize(ctx, audio, false)
		},
		Synthesize: func(ctx context.Context, speech string) (simpleradio.Audio, error) {
			if err := tryLock(ctx, a.speakerLock); err != nil {
				return nil, err
			}
			defer unlock(a.speakerLock)
			audio, err := a.speaker.SayContext(ctx, speech)
			if err != nil {
				return nil, err
			}
			return simpleradio.Audio(pcm.F32AdjustVolume(audio, a.volume)), nil
		},
	}
}
