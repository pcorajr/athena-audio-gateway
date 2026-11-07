package transcript

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestPrivateConcurrentRotation(t *testing.T) {
	root := t.TempDir()
	w := &Writer{Root: root}
	now := time.Now().Local()
	var wg sync.WaitGroup
	for i := 0; i < 40; i++ {
		wg.Go(func() {
			if err := w.Append(Record{Time: now, Kind: "inbound", Text: "synthetic\ntext", Outcome: "recognized"}); err != nil {
				t.Error(err)
			}
		})
	}
	wg.Wait()
	if err := w.Append(Record{Time: now.AddDate(0, 0, 1), Kind: "inbound", Outcome: "stt_failed"}); err != nil {
		t.Fatal(err)
	}
	paths, _ := filepath.Glob(filepath.Join(root, "*.jsonl"))
	if len(paths) != 2 {
		t.Fatal(paths)
	}
	raw, err := os.ReadFile(filepath.Join(root, "transcript-"+now.Format("20060102")+".jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(string(raw)), "\n")
	if len(lines) != 40 {
		t.Fatal(len(lines))
	}
	for _, line := range lines {
		var record Record
		if json.Unmarshal([]byte(line), &record) != nil || record.Text != "synthetic\ntext" {
			t.Fatal("broken JSON record")
		}
	}
	raw, _ = os.ReadFile(filepath.Join(root, "transcript-"+now.Format("20060102")+".txt"))
	if strings.Count(string(raw), "\n") != 40 {
		t.Fatal("multiline text")
	}
	info, _ := os.Stat(paths[0])
	if info.Mode().Perm() != 0600 {
		t.Fatal(info.Mode())
	}
}

func TestAppendRejectsPublicAndSymlink(t *testing.T) {
	root := t.TempDir()
	public := filepath.Join(root, "public")
	if err := os.Mkdir(public, 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(public, 0755); err != nil {
		t.Fatal(err)
	}
	if (&Writer{Root: public}).Append(Record{}) == nil {
		t.Fatal("public directory allowed")
	}
	link := filepath.Join(root, "link")
	if err := os.Symlink(public, link); err != nil {
		t.Fatal(err)
	}
	if (&Writer{Root: link}).Append(Record{}) == nil {
		t.Fatal("symlink allowed")
	}
}

func TestPythonGoConcurrentAppend(t *testing.T) {
	python, repo := os.Getenv("SRSMVP_PYTHON"), os.Getenv("SRSMVP_TEXT_ADAPTER_ROOT")
	if python == "" || repo == "" {
		t.Skip("explicit composition interpreter/root required")
	}
	root := t.TempDir()
	cmd := exec.Command(python, "-B", "-m", "integrations.hermes_radio.transcript_fixture", root, "100")
	cmd.Dir = repo
	cmd.Env = []string{"PATH=" + os.Getenv("PATH"), "PYTHONPATH=" + repo, "PYTHONDONTWRITEBYTECODE=1"}
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	w := &Writer{Root: root}
	for i := 0; i < 100; i++ {
		if err := w.Append(Record{Kind: "fixture", Text: "synthetic Go record"}); err != nil {
			t.Fatal(err)
		}
	}
	if err := cmd.Wait(); err != nil {
		t.Fatal(err)
	}
	paths, _ := filepath.Glob(filepath.Join(root, "*.jsonl"))
	count := 0
	for _, path := range paths {
		raw, _ := os.ReadFile(path)
		for _, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
			var value map[string]any
			if json.Unmarshal([]byte(line), &value) != nil {
				t.Fatal("interleaved record")
			}
			count++
		}
	}
	if count != 200 {
		t.Fatalf("records=%d", count)
	}
}
