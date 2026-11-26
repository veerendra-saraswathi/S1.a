package input

import (
	"fmt"
	"syscall"
)

const (
	ioctlGet = syscall.TIOCGETA
	ioctlSet = syscall.TIOCSETA
	crtscts  = 0x00030000 // CCTS_OFLOW | CRTS_IFLOW, which package syscall does not name on darwin
)

// macOS takes the rate itself as the speed, so any rate the driver supports works.
func speed(baud int) (uint64, error) {
	if baud < 50 || baud > 4_000_000 {
		return 0, fmt.Errorf("baud rate %d is out of range", baud)
	}
	return uint64(baud), nil
}

func setSpeed(t *syscall.Termios, spd uint64) {
	t.Ispeed = spd
	t.Ospeed = spd
}
