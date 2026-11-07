//go:build !windows

package transcript

import (
	"errors"
	"golang.org/x/sys/unix"
	"os"
	"time"
)

func privateFile(path string) (*os.File, error) {
	fd, err := unix.Open(path, unix.O_WRONLY|unix.O_APPEND|unix.O_CREAT|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0600)
	if err != nil {
		return nil, errors.New("transcript_open_failed")
	}
	f := os.NewFile(uintptr(fd), path)
	info, err := f.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0077 != 0 {
		f.Close()
		return nil, errors.New("transcript_file_not_private")
	}
	return f, nil
}
func lockFile(f *os.File) error {
	deadline := time.Now().Add(time.Second)
	for {
		err := unix.Flock(int(f.Fd()), unix.LOCK_EX|unix.LOCK_NB)
		if err == nil {
			return nil
		}
		if err != unix.EWOULDBLOCK || time.Now().After(deadline) {
			return errors.New("transcript_lock_failed")
		}
		time.Sleep(10 * time.Millisecond)
	}
}
func unlockFile(f *os.File) { _ = unix.Flock(int(f.Fd()), unix.LOCK_UN) }
