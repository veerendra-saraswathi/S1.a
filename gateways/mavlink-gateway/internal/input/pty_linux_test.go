package input

import (
	"fmt"
	"os"
	"syscall"
	"testing"
	"unsafe"
)

// openPTY returns the controlling side of a new pseudo-terminal and the path of
// its device side, as posix_openpt, unlockpt and ptsname would.
func openPTY(t *testing.T) (*os.File, string) {
	t.Helper()
	fd, err := syscall.Open("/dev/ptmx", syscall.O_RDWR|syscall.O_NOCTTY|syscall.O_NONBLOCK|syscall.O_CLOEXEC, 0)
	if err != nil {
		t.Skipf("no pseudo-terminals: %v", err)
	}
	var unlock int32
	if _, _, errno := syscall.Syscall(syscall.SYS_IOCTL, uintptr(fd), syscall.TIOCSPTLCK, uintptr(unsafe.Pointer(&unlock))); errno != 0 {
		syscall.Close(fd)
		t.Fatalf("TIOCSPTLCK: %v", errno)
	}
	var n uint32
	if _, _, errno := syscall.Syscall(syscall.SYS_IOCTL, uintptr(fd), syscall.TIOCGPTN, uintptr(unsafe.Pointer(&n))); errno != 0 {
		syscall.Close(fd)
		t.Fatalf("TIOCGPTN: %v", errno)
	}
	return os.NewFile(uintptr(fd), "ptmx"), fmt.Sprintf("/dev/pts/%d", n)
}
