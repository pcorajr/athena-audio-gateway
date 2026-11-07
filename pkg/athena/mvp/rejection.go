package mvp

import (
	"reflect"
	"time"

	"github.com/dharmab/skyeye/pkg/simpleradio"
)

// Interfaces can hold a typed nil from application composition.
func nilBackend(value any) bool {
	if value == nil {
		return true
	}
	v := reflect.ValueOf(value)
	switch v.Kind() {
	case reflect.Chan, reflect.Func, reflect.Interface, reflect.Map, reflect.Pointer, reflect.Slice:
		return v.IsNil()
	default:
		return false
	}
}

func (r *Runner) configurationRejection(t simpleradio.Transmission) string {
	switch {
	case r.Gate == nil:
		return "gate_unavailable"
	case nilBackend(r.Radio):
		return "radio_unavailable"
	case nilBackend(r.Bridge):
		return "bridge_unavailable"
	case r.Recognize == nil:
		return "recognizer_unavailable"
	case r.Synthesize == nil:
		return "synthesizer_unavailable"
	case r.MaxAge <= 0:
		return "invalid_max_age"
	case t.ReceivedAt.IsZero():
		return "received_at_missing"
	case t.ReceivedAt.After(time.Now()):
		return "received_at_future"
	default:
		return ""
	}
}

func (r *Runner) radioRejection(t simpleradio.Transmission) string {
	if radio, ok := r.Radio.(interface {
		MVPRejection(simpleradio.Transmission) string
	}); ok {
		return radio.MVPRejection(t)
	}
	if !r.Radio.ValidMVP(t) {
		return "radio_invalid"
	}
	return ""
}
