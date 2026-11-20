// Package candump reads CAN frames in the log format of Linux can-utils
// (`candump -L`), one frame per line:
//
//	(1795600000.001000) can0 1001550A#D204000055EFBEC3
//
// The timestamp is when the capturing host's kernel received the frame, in
// seconds since the Unix epoch. An 8-digit id is a 29-bit extended frame and a
// 3-digit id an 11-bit standard one. Remote frames (`#R`) and CAN FD frames
// (`##`) are reported as unsupported.
package candump

import (
	"encoding/hex"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/veerendra-saraswathi/S1.a/gateways/can-gateway/internal/dronecan"
)

// ErrUnsupported marks a well-formed line this reader does not handle.
var ErrUnsupported = errors.New("candump: unsupported frame")

// Parse reads one log line. It returns the frame and the interface it came from.
func Parse(line string) (dronecan.Frame, string, error) {
	fields := strings.Fields(line)
	if len(fields) < 3 {
		return dronecan.Frame{}, "", fmt.Errorf("candump: want (time) iface id#data, got %q", line)
	}
	ts, err := parseTime(fields[0])
	if err != nil {
		return dronecan.Frame{}, "", err
	}
	iface := fields[1]
	idText, dataText, ok := strings.Cut(fields[2], "#")
	if !ok {
		return dronecan.Frame{}, iface, fmt.Errorf("candump: no '#' in %q", fields[2])
	}
	if strings.HasPrefix(dataText, "#") || strings.HasPrefix(dataText, "R") {
		return dronecan.Frame{}, iface, ErrUnsupported
	}

	var f dronecan.Frame
	switch len(idText) {
	case 3:
	case 8:
		f.Extended = true
	default:
		return dronecan.Frame{}, iface, fmt.Errorf("candump: id %q is neither 3 nor 8 hex digits", idText)
	}
	id, err := strconv.ParseUint(idText, 16, 32)
	if err != nil || (f.Extended && id > 0x1FFFFFFF) || (!f.Extended && id > 0x7FF) {
		return dronecan.Frame{}, iface, fmt.Errorf("candump: bad id %q", idText)
	}
	data, err := hex.DecodeString(dataText)
	if err != nil || len(data) > 8 {
		return dronecan.Frame{}, iface, fmt.Errorf("candump: bad data %q", dataText)
	}
	f.ID, f.Data, f.Time = uint32(id), data, ts
	return f, iface, nil
}

// parseTime reads "(seconds.micros)" without going through a float, so no
// microsecond is lost.
func parseTime(field string) (time.Time, error) {
	inner, ok := strings.CutPrefix(field, "(")
	if inner, ok = strings.CutSuffix(inner, ")"); !ok {
		return time.Time{}, fmt.Errorf("candump: timestamp %q is not (seconds.micros)", field)
	}
	secText, fracText, _ := strings.Cut(inner, ".")
	sec, err := strconv.ParseInt(secText, 10, 64)
	if err != nil || len(fracText) > 9 {
		return time.Time{}, fmt.Errorf("candump: bad timestamp %q", field)
	}
	nanos := int64(0)
	if fracText != "" {
		n, err := strconv.ParseInt(fracText, 10, 64)
		if err != nil {
			return time.Time{}, fmt.Errorf("candump: bad timestamp %q", field)
		}
		for i := len(fracText); i < 9; i++ {
			n *= 10
		}
		nanos = n
	}
	return time.Unix(sec, nanos).UTC(), nil
}
