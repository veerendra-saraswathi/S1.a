// Package mavlink is a small, dependency-free MAVLink v1/v2 frame parser and a
// decoder for the handful of common-dialect messages the gateway forwards.
//
// It is deliberately not a full MAVLink implementation: it reads, never writes,
// and it only knows the messages listed in messages.go. Frames for any other
// message id are skipped by length and counted in Stats.Unknown.
package mavlink

const (
	magicV1 = 0xFE
	magicV2 = 0xFD

	headerLenV1  = 6
	headerLenV2  = 10
	crcLen       = 2
	signatureLen = 13

	incompatSigned = 0x01
)

// Frame is one checksum-verified MAVLink message.
type Frame struct {
	Version int // 1 or 2
	Seq     uint8
	SysID   uint8
	CompID  uint8
	MsgID   uint32
	// Payload is zero-extended to the message's full base length, which undoes
	// MAVLink 2's trailing-zero truncation. It may be longer when the sender
	// included extension fields.
	Payload []byte
}

// Stats counts what the parser saw. All counters only grow.
type Stats struct {
	Frames  uint64 // verified frames returned to the caller
	BadCRC  uint64 // a known message id whose checksum did not match
	Unknown uint64 // well-framed messages with an id this package does not decode
	Dropped uint64 // v2 frames with incompatibility flags this parser does not understand
	Garbage uint64 // bytes skipped while looking for a start-of-frame marker
}

// Parser turns a byte stream into frames. It keeps an incomplete trailing frame
// between calls, so it works for serial and TCP links. For UDP, where every
// datagram holds whole frames, call Reset before each datagram.
// A Parser is not safe for concurrent use.
type Parser struct {
	buf   []byte
	Stats Stats
}

// Reset discards any buffered partial frame.
func (p *Parser) Reset() { p.buf = p.buf[:0] }

// Write feeds bytes to the parser and returns every complete frame found.
func (p *Parser) Write(data []byte) []Frame {
	p.buf = append(p.buf, data...)
	var frames []Frame
	i := 0
	for i < len(p.buf) {
		b := p.buf[i:]
		if b[0] != magicV1 && b[0] != magicV2 {
			p.Stats.Garbage++
			i++
			continue
		}
		if len(b) < 2 {
			break
		}
		payloadLen := int(b[1])
		headerLen, total := headerLenV1, headerLenV1+payloadLen+crcLen
		if b[0] == magicV2 {
			headerLen, total = headerLenV2, headerLenV2+payloadLen+crcLen
			if len(b) < 3 {
				break
			}
			if b[2]&incompatSigned != 0 {
				total += signatureLen
			}
		}
		if len(b) < total {
			break // wait for the rest of this frame
		}

		f := Frame{Version: 1, Seq: b[2], SysID: b[3], CompID: b[4], MsgID: uint32(b[5])}
		if b[0] == magicV2 {
			f = Frame{
				Version: 2, Seq: b[4], SysID: b[5], CompID: b[6],
				MsgID: uint32(b[7]) | uint32(b[8])<<8 | uint32(b[9])<<16,
			}
		}

		def, known := definitions[f.MsgID]
		if !known {
			// The checksum cannot be verified without the message's CRC_EXTRA,
			// so trust the length byte and move past the frame.
			p.Stats.Unknown++
			i += total
			continue
		}

		crc := crcInit
		for _, c := range b[1 : headerLen+payloadLen] {
			crc = crcAccumulate(c, crc)
		}
		crc = crcAccumulate(def.crcExtra, crc)
		got := uint16(b[headerLen+payloadLen]) | uint16(b[headerLen+payloadLen+1])<<8
		if crc != got {
			// Probably a false start-of-frame inside other data: resync one byte on.
			p.Stats.BadCRC++
			i++
			continue
		}
		i += total

		if b[0] == magicV2 && b[2]&^incompatSigned != 0 {
			// The spec requires discarding frames with unknown incompat flags.
			p.Stats.Dropped++
			continue
		}

		size := payloadLen
		if size < def.length {
			size = def.length
		}
		f.Payload = make([]byte, size)
		copy(f.Payload, b[headerLen:headerLen+payloadLen])
		frames = append(frames, f)
		p.Stats.Frames++
	}
	p.buf = append(p.buf[:0], p.buf[i:]...)
	return frames
}

const crcInit uint16 = 0xFFFF

// crcAccumulate is one step of the X.25 / CRC-16-MCRF4XX checksum MAVLink uses.
func crcAccumulate(b byte, crc uint16) uint16 {
	tmp := b ^ byte(crc&0xFF)
	tmp ^= tmp << 4
	return (crc >> 8) ^ (uint16(tmp) << 8) ^ (uint16(tmp) << 3) ^ (uint16(tmp) >> 4)
}
