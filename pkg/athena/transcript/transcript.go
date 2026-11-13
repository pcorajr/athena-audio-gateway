// Package transcript writes private, append-only radio records shared with the
// Python adapter. The JSONL is authoritative; TXT is a one-line tail view.
package transcript

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

type Record struct {
	Time        time.Time        `json:"time"`
	Kind        string           `json:"kind"`
	ID          string           `json:"transmission_id"`
	FrequencyHz uint64           `json:"frequency_hz"`
	Modulation  string           `json:"modulation"`
	Speaker     string           `json:"speaker"`
	Text        string           `json:"text"`
	Outcome     string           `json:"outcome"`
	Route       string           `json:"route,omitempty"`
	Model       string           `json:"model,omitempty"`
	Part        int              `json:"part,omitempty"`
	Timings     map[string]int64 `json:"timings_ms,omitempty"`
}

type Writer struct{ Root string }

func Default() *Writer {
	root := os.Getenv("ATHENA_RADIO_TRANSCRIPT_DIR")
	if root == "" {
		home, err := os.UserHomeDir()
		if err != nil {
			return &Writer{}
		}
		root = filepath.Join(home, ".hermes", "cache", "athena-radio")
	}
	return &Writer{Root: root}
}

// Log deliberately reports a bounded code, never error bodies or audio. Retry
// happens on the NEXT record, not by replaying a potentially appended record.
func (w *Writer) Log(r Record) {
	if w == nil {
		return
	}
	if w.Append(r) != nil {
		_, _ = fmt.Fprintln(os.Stderr, "athena_radio_transcript_write_failed")
	}
}

func (w *Writer) Append(r Record) error {
	if w.Root == "" {
		return errors.New("transcript_root_unavailable")
	}
	if err := os.MkdirAll(w.Root, 0700); err != nil {
		return errors.New("transcript_directory_failed")
	}
	info, err := os.Lstat(w.Root)
	if err != nil || !info.IsDir() || (runtime.GOOS != "windows" && info.Mode().Perm()&0077 != 0) {
		return errors.New("transcript_directory_not_private")
	}
	lock, err := privateFile(filepath.Join(w.Root, ".transcript.lock"))
	if err != nil {
		return err
	}
	defer lock.Close()
	if err = lockFile(lock); err != nil {
		return err
	}
	defer unlockFile(lock)
	if r.Time.IsZero() {
		r.Time = time.Now()
	}
	r.Time = r.Time.Local()
	raw, err := json.Marshal(r)
	if err != nil {
		return errors.New("transcript_encoding_failed")
	}
	day := r.Time.Format("20060102")
	line := func(s string) string {
		return strings.Map(func(c rune) rune {
			if c < 32 || c == 127 || c == '\u2028' || c == '\u2029' {
				return ' '
			}
			return c
		}, s)
	}
	timings := ""
	if len(r.Timings) > 0 {
		rawTimings, err := json.Marshal(r.Timings)
		if err != nil {
			return errors.New("transcript_encoding_failed")
		}
		timings = " timings_ms=" + string(rawTimings)
	}
	txt := fmt.Sprintf("%s %.3f %s %s [%s %s %s %s part=%d%s] %s\n", r.Time.Format(time.RFC3339Nano), float64(r.FrequencyHz)/1000000, line(r.Modulation), line(r.Speaker), line(r.Kind), line(r.ID), line(r.Route), line(r.Outcome), r.Part, timings, line(r.Text))
	for _, item := range []struct {
		suffix string
		data   []byte
	}{{"jsonl", append(raw, '\n')}, {"txt", []byte(txt)}} {
		f, err := privateFile(filepath.Join(w.Root, "transcript-"+day+"."+item.suffix))
		if err != nil {
			return err
		}
		n, writeErr := f.Write(item.data)
		closeErr := f.Close()
		if writeErr != nil || closeErr != nil || n != len(item.data) {
			return errors.New("transcript_append_failed")
		}
	}
	return nil
}
