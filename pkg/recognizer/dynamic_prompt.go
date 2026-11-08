package recognizer

import (
	"os"
	"strings"
	"sync"
	"time"
)

// dynamicPromptMaxChars bounds the appended vocabulary. Whisper keeps only the
// last ~224 prompt tokens, so the dynamic part must stay short.
const dynamicPromptMaxChars = 600

// dynamicPromptMaxAge: a file older than this is stale (adapter stopped) and
// is ignored.
const dynamicPromptMaxAge = 30 * time.Minute

// dynamicPrompt reads vocabulary (callsign, nearby airfields, brevity words)
// that the Athena adapter writes while the mission runs. The file is re-read
// only when its modification time or size changes.
type dynamicPrompt struct {
	path  string
	mu    sync.Mutex
	mtime time.Time
	size  int64
	text  string
}

func (d *dynamicPrompt) load() string {
	if d == nil || strings.TrimSpace(d.path) == "" {
		return ""
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	info, err := os.Stat(d.path)
	if err != nil || info.IsDir() {
		d.text, d.mtime, d.size = "", time.Time{}, 0
		return ""
	}
	if time.Since(info.ModTime()) > dynamicPromptMaxAge {
		return ""
	}
	if info.ModTime().Equal(d.mtime) && info.Size() == d.size {
		return d.text
	}
	b, err := os.ReadFile(d.path)
	if err != nil {
		return d.text
	}
	text := strings.Join(strings.Fields(string(b)), " ")
	if len(text) > dynamicPromptMaxChars {
		text = text[:dynamicPromptMaxChars]
		if i := strings.LastIndexAny(text, " ,."); i > 0 {
			text = text[:i]
		}
	}
	d.text, d.mtime, d.size = text, info.ModTime(), info.Size()
	return d.text
}

// WithDynamicPromptFile appends the contents of a file to the prompt on every
// recognition. The Athena adapter keeps it current with the pilot callsign and
// the airfields near ownship (Creech, Nellis, ...) so whisper hears them.
func WithDynamicPromptFile(path string) Option {
	return func(o *recognizerOptions) {
		o.dynamic = &dynamicPrompt{path: path}
	}
}

// withDynamic joins a base prompt with the current dynamic vocabulary.
func withDynamic(base string, d *dynamicPrompt) string {
	extra := d.load()
	if extra == "" {
		return base
	}
	if base == "" {
		return extra
	}
	return base + " " + extra
}
