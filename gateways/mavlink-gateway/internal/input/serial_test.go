//go:build darwin || linux

package input

import (
	"bytes"
	"io"
	"os"
	"testing"
	"time"
)

// A pseudo-terminal stands in for a serial device: what is written to the
// controlling side arrives on the device side through the same line discipline a
// USB or UART port has, so a byte it would translate or swallow shows up here.
func TestSerialIsRawAndReadOnly(t *testing.T) {
	ctl, device := openPTY(t)
	defer ctl.Close()

	link, err := openSerial(device, 57600)
	if err != nil {
		t.Fatal(err)
	}
	defer link.Close()
	if _, ok := link.(*os.File); !ok {
		t.Fatalf("link is %T", link)
	}
	if _, err := link.(*os.File).Write([]byte{0}); err == nil {
		t.Fatal("the device was opened writable")
	}

	// Every byte value, twice: CR, NL, XON and XOFF, Ctrl-C, DEL and 0xff included.
	want := make([]byte, 512)
	for i := range want {
		want[i] = byte(i)
	}
	if _, err := ctl.Write(want); err != nil {
		t.Fatal(err)
	}
	got := make([]byte, len(want))
	read := make(chan error, 1)
	go func() { _, err := io.ReadFull(link, got); read <- err }()
	select {
	case err := <-read:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("bytes were held back; the device is not in raw mode")
	}
	if !bytes.Equal(got, want) {
		for i := range want {
			if got[i] != want[i] {
				t.Fatalf("byte %d: got %#02x, want %#02x", i, got[i], want[i])
			}
		}
	}

	// Nothing echoed back towards the vehicle.
	ctl.SetReadDeadline(time.Now().Add(200 * time.Millisecond))
	if n, _ := ctl.Read(make([]byte, 16)); n > 0 {
		t.Fatalf("%d bytes echoed back to the sender", n)
	}
}

func TestSerialCloseUnblocksRead(t *testing.T) {
	ctl, device := openPTY(t)
	defer ctl.Close()
	link, err := openSerial(device, DefaultBaud)
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { _, err := link.Read(make([]byte, 64)); done <- err }()
	time.Sleep(50 * time.Millisecond)
	link.Close()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Close did not unblock Read")
	}
}

func TestSerialRefusesAFileThatIsNotATerminal(t *testing.T) {
	path := t.TempDir() + "/not-a-tty"
	if err := os.WriteFile(path, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := openSerial(path, DefaultBaud); err == nil {
		t.Fatal("opened a regular file as a serial device")
	}
}
