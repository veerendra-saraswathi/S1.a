package input

import (
	"bytes"
	"os"
	"syscall"
	"testing"
	"unsafe"
)

// openPTY returns the controlling side of a new pseudo-terminal and the path of
// its device side, as posix_openpt, grantpt, unlockpt and ptsname would.
func openPTY(t *testing.T) (*os.File, string) {
	t.Helper()
	fd, err := syscall.Open("/dev/ptmx", syscall.O_RDWR|syscall.O_NOCTTY|syscall.O_NONBLOCK|syscall.O_CLOEXEC, 0)
	if err != nil {
		t.Skipf("no pseudo-terminals: %v", err)
	}
	for _, req := range []uintptr{syscall.TIOCPTYGRANT, syscall.TIOCPTYUNLK} {
		if _, _, errno := syscall.Syscall(syscall.SYS_IOCTL, uintptr(fd), req, 0); errno != 0 {
			syscall.Close(fd)
			t.Fatalf("ioctl %#x: %v", req, errno)
		}
	}
	var name [128]byte
	if _, _, errno := syscall.Syscall(syscall.SYS_IOCTL, uintptr(fd), syscall.TIOCPTYGNAME, uintptr(unsafe.Pointer(&name[0]))); errno != 0 {
		syscall.Close(fd)
		t.Fatalf("TIOCPTYGNAME: %v", errno)
	}
	return os.NewFile(uintptr(fd), "ptmx"), string(name[:bytes.IndexByte(name[:], 0)])
}
