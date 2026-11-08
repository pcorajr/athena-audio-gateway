package recognizer

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestDynamicPromptAppendedAndReloaded(t *testing.T) {
	t.Parallel()
	path := filepath.Join(t.TempDir(), "dyn.txt")
	o := recognizerOptions{athenaMode: true, promptOverride: "Athena, picture."}
	WithDynamicPromptFile(path)(&o)

	if got := o.initialPrompt("Athena"); got != "Athena, picture." {
		t.Fatalf("missing file must leave the base prompt, got %q", got)
	}
	if err := os.WriteFile(path, []byte("Prometheus. Creech,\n Nellis."), 0o600); err != nil {
		t.Fatal(err)
	}
	if got := o.initialPrompt("Athena"); got != "Athena, picture. Prometheus. Creech, Nellis." {
		t.Fatalf("got %q", got)
	}
	// Rewrite with a different size: picked up without a restart.
	if err := os.WriteFile(path, []byte("Prometheus. Groom Lake, Tonopah Test Range."), 0o600); err != nil {
		t.Fatal(err)
	}
	if got := o.initialPrompt("Athena"); !strings.Contains(got, "Tonopah") || strings.Contains(got, "Creech") {
		t.Fatalf("not reloaded: %q", got)
	}
	// Stale file (adapter stopped long ago) is ignored.
	old := time.Now().Add(-2 * dynamicPromptMaxAge)
	if err := os.Chtimes(path, old, old); err != nil {
		t.Fatal(err)
	}
	if got := o.initialPrompt("Athena"); got != "Athena, picture." {
		t.Fatalf("stale dynamic prompt used: %q", got)
	}
}

func TestDynamicPromptBounded(t *testing.T) {
	t.Parallel()
	path := filepath.Join(t.TempDir(), "dyn.txt")
	if err := os.WriteFile(path, []byte(strings.Repeat("Creech, ", 400)), 0o600); err != nil {
		t.Fatal(err)
	}
	d := &dynamicPrompt{path: path}
	if got := d.load(); len(got) > dynamicPromptMaxChars || got == "" {
		t.Fatalf("len %d", len(got))
	}
}
