package bus

import (
	"testing"
	"time"

	"github.com/veerendra-saraswathi/S1.a/gateways/internal/s2"
)

// BenchmarkLine is the CAN gateway's whole path per candump line: parse,
// reassemble, CRC, decode, aggregate, with a flush every simulated second.
func BenchmarkLine(b *testing.B) {
	golden := goldenLines(b)
	p := New(Config{TenantID: tenant, ObjectType: "Subsystem", Interval: 100 * time.Millisecond})
	discard := func(s2.Outbound) {}
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		p.Line(golden[i%len(golden)], discard)
		if i%len(golden) == len(golden)-1 {
			p.Flush(discard)
		}
	}
}
