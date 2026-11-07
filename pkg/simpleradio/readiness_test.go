package simpleradio

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/dharmab/skyeye/pkg/athena/transcript"
	"github.com/dharmab/skyeye/pkg/simpleradio/types"
)

func TestPacket108ReadinessOnMetadataSyncWithoutPTT(t *testing.T) {
	c, tr, _ := mvpTestClient()
	c.transcript = &transcript.Writer{Root: t.TempDir()}
	peer := c.clients[tr.route.origin]
	c.syncMVPClients(nil)
	c.syncMVPClient(peer)
	c.syncMVPClient(peer) // no duplicate status for unchanged metadata
	duplicate := peer
	duplicate.GUID = types.NewGUID()
	c.syncMVPClient(duplicate)
	c.removeClient(duplicate)
	c.removeClient(peer)
	c.invalidateMVP()
	paths, _ := filepath.Glob(filepath.Join(c.transcript.Root, "*.jsonl"))
	raw, err := os.ReadFile(paths[0])
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"pilot_missing", "ready", "pilot_ambiguous", "ready", "pilot_missing", "transport_unavailable"}
	lines := strings.Split(strings.TrimSpace(string(raw)), "\n")
	if len(lines) != len(want) {
		t.Fatalf("unexpected records: %s", raw)
	}
	for i, line := range lines {
		var rec transcript.Record
		if err := json.Unmarshal([]byte(line), &rec); err != nil {
			t.Fatal(err)
		}
		if rec.Kind != "radio_readiness" || rec.Outcome != want[i] || rec.Speaker != "Athena" || rec.Text != "" || rec.FrequencyHz != 133000000 || rec.Modulation != "AM" {
			t.Fatalf("unsafe/incorrect readiness: %+v", rec)
		}
	}
	if strings.Contains(string(raw), string(peer.GUID)) || strings.Contains(string(raw), peer.Name) {
		t.Fatal("peer metadata leaked")
	}
}
