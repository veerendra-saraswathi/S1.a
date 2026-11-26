package input

import (
	"fmt"
	"syscall"
)

const (
	ioctlGet = syscall.TCGETS
	ioctlSet = syscall.TCSETS
	crtscts  = 0x80000000 // CRTSCTS, which package syscall does not name on linux
	cbaud    = 0x100f     // CBAUD
)

// Linux encodes the rate in the control flags, one code per standard rate.
var speeds = map[int]uint32{
	9600: syscall.B9600, 19200: syscall.B19200, 38400: syscall.B38400,
	57600: syscall.B57600, 115200: syscall.B115200, 230400: syscall.B230400,
	460800: syscall.B460800, 500000: syscall.B500000, 576000: syscall.B576000,
	921600: syscall.B921600, 1000000: syscall.B1000000, 1500000: syscall.B1500000,
	2000000: syscall.B2000000,
}

func speed(baud int) (uint32, error) {
	if s, ok := speeds[baud]; ok {
		return s, nil
	}
	return 0, fmt.Errorf("baud rate %d is not a standard Linux rate (9600 to 2000000)", baud)
}

func setSpeed(t *syscall.Termios, spd uint32) {
	t.Cflag = t.Cflag&^cbaud | spd
	t.Ispeed = spd
	t.Ospeed = spd
}
