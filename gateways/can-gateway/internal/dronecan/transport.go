// Package dronecan reads DroneCAN (UAVCAN v0) message transfers from CAN frames.
//
// A DroneCAN message travels in 29-bit extended CAN frames:
//
//	CAN ID   bits 28-24 priority, 23-8 data type id, 7 service flag (0 = message),
//	         6-0 source node id (0 = anonymous)
//	data     payload bytes, then one tail byte:
//	         bit 7 start of transfer, bit 6 end of transfer, bit 5 toggle,
//	         bits 4-0 transfer id
//
// A payload of up to 7 bytes fits one frame. A longer one is split across
// frames whose toggle bit alternates from 0; the first frame opens with a
// CRC-16-CCITT of the data type's 64-bit signature followed by the payload, so
// a transfer can only be checked for a type whose signature is known.
//
// The reader only listens. Services, anonymous transfers and CAN FD are not read.
package dronecan

import (
	"encoding/binary"
	"time"
)

// Frame is one received CAN frame.
type Frame struct {
	ID       uint32 // 29-bit identifier
	Extended bool
	Data     []byte
	Time     time.Time // when it was captured
}

// Transfer is one complete, checked message transfer.
type Transfer struct {
	DataTypeID uint16
	SourceNode uint8
	Priority   uint8
	TransferID uint8
	Payload    []byte
	Time       time.Time // capture time of its last frame
}

// Stats counts what the reassembler has seen. Every frame lands in exactly one
// of the frame counters.
type Stats struct {
	Transfers  uint64 // complete transfers returned
	Ignored    uint64 // frames that are not message frames: standard ids, services, anonymous
	Undecoded  uint64 // message frames of a type this reader does not know
	Malformed  uint64 // empty frames, bad toggles, out-of-place starts
	BadCRC     uint64 // multi-frame transfers whose CRC did not match
	TimedOut   uint64 // multi-frame transfers abandoned part-way
	FrameCount uint64
}

// TransferTimeout is how long a multi-frame transfer may take between frames.
const TransferTimeout = 2 * time.Second

type key struct {
	dataType uint16
	source   uint8
}

type partial struct {
	transferID uint8
	toggle     bool
	crc        uint16
	payload    []byte
	last       time.Time
}

// Reassembler turns frames into transfers. It is not safe for concurrent use.
type Reassembler struct {
	// Known reports a data type's signature, and whether the type is read at all.
	Known func(dataType uint16) (signature uint64, ok bool)
	Stats Stats

	open map[key]*partial
}

// Push accepts one frame and returns the transfer it completes, if any.
func (r *Reassembler) Push(f Frame) (Transfer, bool) {
	r.Stats.FrameCount++
	if !f.Extended || f.ID&0x80 != 0 || f.ID&0x7F == 0 {
		r.Stats.Ignored++
		return Transfer{}, false
	}
	dataType := uint16(f.ID >> 8)
	k := key{dataType: dataType, source: uint8(f.ID & 0x7F)}
	signature, known := r.Known(dataType)
	if !known {
		r.Stats.Undecoded++
		return Transfer{}, false
	}
	if len(f.Data) == 0 || len(f.Data) > 8 {
		r.Stats.Malformed++
		return Transfer{}, false
	}

	tail := f.Data[len(f.Data)-1]
	start, end, toggle, tid := tail&0x80 != 0, tail&0x40 != 0, tail&0x20 != 0, tail&0x1F
	body := f.Data[:len(f.Data)-1]
	out := Transfer{DataTypeID: dataType, SourceNode: k.source, Priority: uint8(f.ID >> 24 & 0x1F), TransferID: tid, Time: f.Time}

	if r.open == nil {
		r.open = make(map[key]*partial)
	}

	if start && end { // single frame
		if toggle {
			r.Stats.Malformed++
			return Transfer{}, false
		}
		delete(r.open, k) // a new transfer supersedes an unfinished one
		out.Payload = append([]byte(nil), body...)
		r.Stats.Transfers++
		return out, true
	}

	if start { // first of several
		if toggle || len(body) < 2 {
			r.Stats.Malformed++
			return Transfer{}, false
		}
		if _, ok := r.open[k]; ok {
			r.Stats.TimedOut++ // the previous one never finished
		}
		r.open[k] = &partial{
			transferID: tid,
			toggle:     true, // the next frame's toggle
			crc:        binary.LittleEndian.Uint16(body[:2]),
			payload:    append([]byte(nil), body[2:]...),
			last:       f.Time,
		}
		return Transfer{}, false
	}

	p, ok := r.open[k]
	if !ok || p.transferID != tid || p.toggle != toggle {
		r.Stats.Malformed++ // a continuation of nothing we hold, or out of order
		return Transfer{}, false
	}
	if f.Time.Sub(p.last) > TransferTimeout {
		delete(r.open, k)
		r.Stats.TimedOut++
		return Transfer{}, false
	}
	p.payload = append(p.payload, body...)
	p.toggle = !p.toggle
	p.last = f.Time
	if !end {
		return Transfer{}, false
	}
	delete(r.open, k)
	if transferCRC(signature, p.payload) != p.crc {
		r.Stats.BadCRC++
		return Transfer{}, false
	}
	out.Payload = p.payload
	r.Stats.Transfers++
	return out, true
}

// transferCRC is CRC-16-CCITT-FALSE (poly 0x1021, init 0xFFFF) over the data
// type signature, little-endian, then the payload.
func transferCRC(signature uint64, payload []byte) uint16 {
	crc := uint16(0xFFFF)
	add := func(b byte) {
		crc ^= uint16(b) << 8
		for i := 0; i < 8; i++ {
			if crc&0x8000 != 0 {
				crc = crc<<1 ^ 0x1021
			} else {
				crc <<= 1
			}
		}
	}
	for i := 0; i < 8; i++ {
		add(byte(signature >> (8 * i)))
	}
	for _, b := range payload {
		add(b)
	}
	return crc
}
