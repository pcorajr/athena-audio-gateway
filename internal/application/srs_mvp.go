package application

import (
	"context"

	"github.com/dharmab/skyeye/pkg/athena/mvp"
	"github.com/dharmab/skyeye/pkg/pcm"
	"github.com/dharmab/skyeye/pkg/simpleradio"
	"github.com/rs/zerolog/log"
)

// runSRSMVP deliberately bypasses persona, GCI and the asynchronous legacy
// synthesis/transmit queues. No diagnostic audio or trace-content export.
func (a *Application) runSRSMVP(ctx context.Context) {
	a.srsMVPRunner().Run(ctx, a.srsClient.Receive())
}

func (a *Application) srsMVPRunner() *mvp.Runner {
	return &mvp.Runner{
		Gate: a.admissionGate, Bridge: a.hermesBridge, Radio: a.srsClient,
		MaxAge: a.athenaResponseMaxAge, MaxSpeech: a.athenaMaxSpeech,
		Recognize: func(ctx context.Context, audio simpleradio.Audio) (string, error) {
			if err := tryLock(ctx, a.recognizerLock); err != nil {
				return "", err
			}
			defer unlock(a.recognizerLock)
			text, err := a.recognizer.Recognize(ctx, audio, a.enableTranscriptionLogging)
			if err == nil && a.enableTranscriptionLogging {
				log.Info().Str("text", text).Msg("MVP transcript")
			}
			return text, err
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
