package application

import (
	"os"
	"strings"
)

// firstNonEmptyEnv returns the first environment variable that is set and
// non-empty.
//
// Credentials are read from the environment rather than accepted as flags: a
// flag value is visible in `ps` output and lands in shell history, and a config
// file can reach a repository. A mode-0600 systemd EnvironmentFile is the
// narrowest of the three, so that is the supported path.
//
// Nothing in this package logs the returned value.
func firstNonEmptyEnv(names ...string) string {
	for _, name := range names {
		if v := strings.TrimSpace(os.Getenv(name)); v != "" {
			return v
		}
	}
	return ""
}
