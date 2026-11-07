package transcript

import (
	"errors"
	"golang.org/x/sys/windows"
	"os"
	"time"
)

func privateFile(path string) (*os.File, error) {
	if info, err := os.Lstat(path); err == nil && !info.Mode().IsRegular() {
		return nil, errors.New("transcript_file_invalid")
	}
	return os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
}
func lockFile(f *os.File) error {
	deadline := time.Now().Add(time.Second)
	for {
		err := windows.LockFileEx(windows.Handle(f.Fd()), windows.LOCKFILE_EXCLUSIVE_LOCK|windows.LOCKFILE_FAIL_IMMEDIATELY, 0, 1, 0, &windows.Overlapped{})
		if err == nil {
			return nil
		}
		if time.Now().After(deadline) {
			return errors.New("transcript_lock_failed")
		}
		time.Sleep(10 * time.Millisecond)
	}
}
func unlockFile(f *os.File) {
	_ = windows.UnlockFileEx(windows.Handle(f.Fd()), 0, 1, 0, &windows.Overlapped{})
}
