package simpleradio

import (
	"context"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/dharmab/skyeye/pkg/athena/transcript"
)

func TestCancellationOwnerFollowsRealCaptureAcrossQueues(t *testing.T) {
	for _, boundary := range []string{"assembly", "decode", "receive"} {
		t.Run(boundary, func(t *testing.T) {
			c, tr, _ := mvpTestClient()
			c.transcript = &transcript.Writer{Root: t.TempDir()}
			c.rxChan = make(chan Transmission, 1)
			encoded, err := c.encodeMVP(make(Audio, frameSize))
			if err != nil {
				t.Fatal(err)
			}
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			in := make(chan voiceDatagram)
			out := make(chan transmissionPackets, 1)
			captureDone := make(chan struct{})
			go func() { defer close(captureDone); c.receiveMVPVoice(ctx, in, out) }()
			count := 26
			if boundary == "assembly" {
				count = 2
			}
			for i := 1; i <= count; i++ {
				p := encoded[0]
				p.PacketID = uint64(i)
				p.OriginGUID, p.RelayGUID = []byte(tr.route.origin), []byte(tr.route.origin)
				select {
				case in <- voiceDatagram{data: p.Encode(), epoch: c.mvpEpoch}:
				case <-time.After(2 * time.Second):
					t.Fatal("capture did not receive fixture")
				}
			}
			var completed transmissionPackets
			var decoded Transmission
			decodeDone := make(chan struct{})
			if boundary != "assembly" {
				select {
				case completed = <-out:
				case <-time.After(2 * time.Second):
					t.Fatal("assembly not completed")
				}
				if completed.receipt == nil {
					t.Fatal("assembly lost receipt")
				}
				out <- completed // deterministically park at the decode boundary
			}
			if boundary == "receive" {
				go func() { defer close(decodeDone); c.decodeVoice(ctx, out) }()
				select {
				case decoded = <-c.rxChan:
				case <-time.After(2 * time.Second):
					t.Fatal("decode not completed")
				}
				if decoded.receipt != completed.receipt {
					t.Fatal("decoder lost receipt")
				}
				c.rxChan <- decoded // accepted receive backlog, with no intake yet
			}
			cancel()
			<-captureDone // cancellation outcomes must exist before owner exit
			if boundary == "receive" {
				<-decodeDone
			} else {
				c.decodeVoice(ctx, out)
			}
			if boundary != "assembly" {
				// A producer finishing after cancellation, or a late intake, must
				// not overwrite/duplicate the owner's terminal cancellation.
				c.captureFailure(completed, "decode_failed_or_empty")
				Transmission{receipt: completed.receipt}.RecordReception("late synthetic STT", "recognized", nil)
			}
			seen, terminal := map[string]int{}, map[string]int{}
			for _, rec := range captureRecords(t, c.transcript.Root) {
				if rec.Kind == "received" {
					seen[rec.ID]++
				}
				if rec.Kind == "inbound" {
					terminal[rec.ID]++
					if rec.Text != "" || rec.Outcome != "capture_canceled" {
						t.Errorf("not canceled: %+v", rec)
					}
				}
			}
			if len(seen) != 1 || len(terminal) != 1 {
				t.Fatalf("observed=%v terminal=%v", seen, terminal)
			}
			for id := range seen {
				if terminal[id] != 1 {
					t.Fatalf("terminal %s=%d", id, terminal[id])
				}
			}
		})
	}
}

func TestCancellationReceiptProducerRacesExactlyOnce(t *testing.T) {
	ctx, cancel := context.WithCancel(t.Context())
	w := &transcript.Writer{Root: t.TempDir()}
	ledger := &captureLedger{ctx: ctx, writer: w, pending: make(map[*captureReceipt]transcript.Record)}
	first := ledger.begin(transcript.Record{ID: "before-cancel"})
	first.finish("synthetic recognized", "recognized", nil)
	var receipts []*captureReceipt
	for i := 0; i < 16; i++ {
		receipts = append(receipts, ledger.begin(transcript.Record{ID: fmt.Sprintf("race-%d", i)}))
	}
	cancel()
	// Hold the owner lock until every producer is ready to race the canceler.
	ledger.mu.Lock()
	var ready, done sync.WaitGroup
	ready.Add(len(receipts) + 2)
	done.Go(func() { ready.Done(); ledger.cancel() })
	done.Go(func() {
		ready.Done()
		ledger.begin(transcript.Record{ID: "late-producer"}).finish("late", "recognized", nil)
	})
	for _, receipt := range receipts {
		done.Go(func() { ready.Done(); receipt.finish("late", "recognized", nil) })
	}
	ready.Wait()
	ledger.mu.Unlock()
	done.Wait()
	ledger.cancel() // idempotent shutdown
	if len(ledger.pending) != 0 {
		t.Fatal("terminal receipts retained")
	}
	counts := map[string]int{}
	for _, rec := range captureRecords(t, w.Root) {
		if rec.Kind != "inbound" {
			continue
		}
		counts[rec.ID]++
		if rec.ID == "before-cancel" {
			if rec.Text != "synthetic recognized" || rec.Outcome != "recognized" {
				t.Fatal("rewrote completed observation")
			}
		} else if rec.Text != "" || rec.Outcome != "capture_canceled" {
			t.Errorf("not canceled: %+v", rec)
		}
	}
	if len(counts) != len(receipts)+2 {
		t.Fatalf("lost terminal records: %v", counts)
	}
	for id, n := range counts {
		if n != 1 {
			t.Errorf("%s terminal count=%d", id, n)
		}
	}
}
