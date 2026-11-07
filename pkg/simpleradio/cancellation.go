package simpleradio

import (
	"context"
	"sync"

	"github.com/dharmab/skyeye/pkg/athena/transcript"
)

// captureLedger tracks only unfinished observations, not audio or reply work.
// Its size is bounded by the existing 64 assemblies, 16 decode slots, 16
// receive slots, 16 intake slots, and current decoder/STT work. Completion
// removes entries immediately. The assembly owner cancels it synchronously
// before exiting, even if a downstream producer is between queue operations.
type captureLedger struct {
	mu      sync.Mutex
	ctx     context.Context
	writer  *transcript.Writer
	pending map[*captureReceipt]transcript.Record
	closed  bool
}

type captureReceipt struct{ ledger *captureLedger }

func (l *captureLedger) begin(record transcript.Record) *captureReceipt {
	l.mu.Lock()
	defer l.mu.Unlock()
	r := &captureReceipt{ledger: l}
	record.Kind, record.Outcome = "received", "assembling"
	l.writer.Log(record)
	if l.closed || l.ctx.Err() != nil {
		record.Kind, record.Outcome, record.Text = "inbound", "capture_canceled", ""
		l.writer.Log(record)
	} else {
		l.pending[r] = record
	}
	return r
}

func (r *captureReceipt) finish(text, outcome string, timings map[string]int64) {
	l := r.ledger
	l.mu.Lock()
	defer l.mu.Unlock()
	record, ok := l.pending[r]
	if !ok {
		return
	} // canceled or already completed at another boundary
	delete(l.pending, r)
	if l.ctx.Err() != nil {
		text, outcome = "", "capture_canceled"
	}
	record.Kind, record.Text, record.Outcome, record.Timings = "inbound", text, outcome, timings
	l.writer.Log(record)
}

func (l *captureLedger) cancel() {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.closed = true
	for receipt, record := range l.pending {
		delete(l.pending, receipt)
		record.Kind, record.Outcome, record.Text = "inbound", "capture_canceled", ""
		l.writer.Log(record)
	}
}

// RecordReception completes an observation shared by capture/decode/intake.
// True means its owner handled the record (including a prior cancellation);
// callers must not append a second inbound outcome. Synthetic/untracked
// transmissions return false so their caller can use its own transcript sink.
func (t Transmission) RecordReception(text, outcome string, timings map[string]int64) bool {
	if t.receipt == nil {
		return false
	}
	t.receipt.finish(text, outcome, timings)
	return true
}
