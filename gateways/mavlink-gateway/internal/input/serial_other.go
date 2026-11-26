//go:build !darwin && !linux

package input

import (
	"errors"
	"fmt"
)

func speed(baud int) (int, error) {
	if baud <= 0 {
		return 0, fmt.Errorf("baud rate %d is out of range", baud)
	}
	return baud, nil
}

func openSerial(string, int) (Link, error) {
	return nil, errors.New("serial input is supported on macOS and Linux only")
}
