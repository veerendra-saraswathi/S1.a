//go:build darwin || linux

package input

import (
	"fmt"
	"os"
	"syscall"
	"unsafe"
)

// openSerial opens a serial device read-only and puts it in raw mode: 8 data bits,
// no parity, no flow control, no echo, and no byte translated or swallowed, which
// MAVLink's binary frames need. It does what cfmakeraw does, without cgo.
func openSerial(path string, baud int) (Link, error) {
	spd, err := speed(baud)
	if err != nil {
		return nil, err
	}
	// Non-blocking, so the open does not wait for carrier detect, and so the runtime
	// can poll the file and Close can interrupt a Read.
	fd, err := syscall.Open(path, syscall.O_RDONLY|syscall.O_NOCTTY|syscall.O_NONBLOCK|syscall.O_CLOEXEC, 0)
	if err != nil {
		return nil, &os.PathError{Op: "open", Path: path, Err: err}
	}
	var t syscall.Termios
	if err := ioctl(fd, ioctlGet, &t); err != nil {
		syscall.Close(fd)
		return nil, fmt.Errorf("%s is not a terminal device: %w", path, err)
	}
	t.Iflag &^= syscall.IGNBRK | syscall.BRKINT | syscall.PARMRK | syscall.ISTRIP |
		syscall.INLCR | syscall.IGNCR | syscall.ICRNL | syscall.IXON | syscall.IXOFF | syscall.IXANY
	t.Oflag &^= syscall.OPOST
	t.Lflag &^= syscall.ECHO | syscall.ECHONL | syscall.ICANON | syscall.ISIG | syscall.IEXTEN
	t.Cflag &^= syscall.CSIZE | syscall.PARENB | syscall.CSTOPB | crtscts
	t.Cflag |= syscall.CS8 | syscall.CREAD | syscall.CLOCAL
	t.Cc[syscall.VMIN] = 1
	t.Cc[syscall.VTIME] = 0
	setSpeed(&t, spd)
	if err := ioctl(fd, ioctlSet, &t); err != nil {
		syscall.Close(fd)
		return nil, fmt.Errorf("%s: setting raw mode at %d baud: %w", path, baud, err)
	}
	return os.NewFile(uintptr(fd), path), nil
}

func ioctl(fd int, req uintptr, t *syscall.Termios) error {
	if _, _, errno := syscall.Syscall(syscall.SYS_IOCTL, uintptr(fd), req, uintptr(unsafe.Pointer(t))); errno != 0 {
		return errno
	}
	return nil
}
