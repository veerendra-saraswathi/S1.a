package clock

import (
	"testing"
	"time"
)

var t0 = time.Date(2026, 11, 20, 10, 0, 0, 0, time.UTC)

func within(t *testing.T, name string, got, want time.Time, tol time.Duration) {
	t.Helper()
	if d := got.Sub(want); d < -tol || d > tol {
		t.Errorf("%s = %s, want %s ±%s (off by %s)", name, got.Format(time.RFC3339Nano), want.Format(time.RFC3339Nano), tol, d)
	}
}

func TestNoTimedMessageMeansGatewayTime(t *testing.T) {
	var c Clock
	if _, source, ok := c.At(1000, t0); ok || source != Gateway {
		t.Fatalf("At on an empty clock = %v %v, want Gateway, not ok", source, ok)
	}
}

func TestGNSSAnchorGivesVehicleTime(t *testing.T) {
	// The vehicle's clock runs 250 ms ahead of the gateway's; the link takes 4 ms.
	const skew, link = 250 * time.Millisecond, 4 * time.Millisecond
	boot := t0.Add(-30 * time.Second) // true UTC at boot
	var c Clock
	for i := 0; i < 5; i++ {
		bootMs := uint32(30_000 + 1000*i)
		vehicleUTC := boot.Add(time.Duration(bootMs)*time.Millisecond + skew)
		arrival := boot.Add(time.Duration(bootMs)*time.Millisecond + link)
		c.SystemTime(uint64(vehicleUTC.UnixMicro()), bootMs, arrival)
	}
	now := t0.Add(5 * time.Second)

	utc, source, ok := c.At(34_500, now)
	if !ok || source != GNSS {
		t.Fatalf("source = %v ok=%v, want gnss", source, ok)
	}
	// On the vehicle's own clock: true sample time plus its skew.
	within(t, "sample time", utc, boot.Add(34_500*time.Millisecond+skew), time.Millisecond)

	offset, ok := c.Offset(now)
	if !ok {
		t.Fatal("no offset with a fresh SYSTEM_TIME")
	}
	if want := skew - link; offset < want-time.Millisecond || offset > want+time.Millisecond {
		t.Errorf("offset = %s, want %s (skew minus link delay)", offset, want)
	}
}

func TestStaleGNSSFallsBackToTheEstimate(t *testing.T) {
	var c Clock
	c.SystemTime(uint64(t0.UnixMicro()), 10_000, t0)
	later := t0.Add(StaleAfter + time.Second)
	c.Observe(16_000, later)
	if _, source, _ := c.At(16_000, later); source != Estimated {
		t.Errorf("source after %s without SYSTEM_TIME = %v, want estimated", StaleAfter, source)
	}
	if _, ok := c.Offset(later); ok {
		t.Error("an offset was reported from a stale anchor")
	}
}

func TestZeroOrUnsetUTCIsNotAnAnchor(t *testing.T) {
	var c Clock
	c.SystemTime(0, 5_000, t0)                                                                                // no GNSS fix yet
	c.SystemTime(uint64(time.Date(1980, 1, 6, 0, 0, 0, 0, time.UTC).UnixMicro()), 6_000, t0.Add(time.Second)) // GPS epoch, never set
	if _, source, ok := c.At(6_000, t0.Add(time.Second)); !ok || source != Estimated {
		t.Errorf("source = %v ok=%v, want estimated: the boot times still count", source, ok)
	}
}

func TestEstimateTakesTheFastestArrival(t *testing.T) {
	// Samples every 100 ms; the link takes 5 ms plus up to 60 ms of jitter.
	boot := t0
	jitter := []time.Duration{40, 5, 60, 0, 22, 13, 51, 8, 30, 2}
	var c Clock
	var last time.Time
	for i := 0; i < 50; i++ {
		bootMs := uint32(100 * i)
		last = boot.Add(time.Duration(bootMs)*time.Millisecond + 5*time.Millisecond + jitter[i%len(jitter)]*time.Millisecond)
		c.Observe(bootMs, last)
	}
	utc, source, ok := c.At(4_900, last)
	if !ok || source != Estimated {
		t.Fatalf("source = %v ok=%v", source, ok)
	}
	// The minimum link delay is absorbed: the sample lands 5 ms late, not 60.
	within(t, "sample time", utc, boot.Add(4_900*time.Millisecond+5*time.Millisecond), time.Millisecond)

	// Spacing between samples is exact, whatever the jitter.
	a, _, _ := c.At(1_000, last)
	b, _, _ := c.At(1_100, last)
	if b.Sub(a) != 100*time.Millisecond {
		t.Errorf("spacing = %s, want 100ms", b.Sub(a))
	}
}

func TestEstimateFollowsDrift(t *testing.T) {
	// The autopilot's crystal runs 100 ppm slow: 1 ms behind per 10 s.
	var c Clock
	var arrival time.Time
	for i := 0; i <= 600; i++ { // a minute at 10 Hz
		trueElapsed := time.Duration(i) * 100 * time.Millisecond
		bootMs := uint32((trueElapsed - trueElapsed/10_000).Milliseconds())
		arrival = t0.Add(trueElapsed)
		c.Observe(bootMs, arrival)
	}
	utc, _, _ := c.At(c.lastBoot, arrival)
	// A minimum over all time would be 6 ms out by now; the window keeps it to 1-2 ms.
	within(t, "latest sample", utc, arrival, 2*time.Millisecond)
}

func TestRebootStartsOver(t *testing.T) {
	var c Clock
	c.SystemTime(uint64(t0.Add(time.Hour).UnixMicro()), 600_000, t0) // an hour ahead: a bad RTC
	if rebooted := c.Observe(1_200, t0.Add(time.Second)); !rebooted {
		t.Fatal("time_boot_ms falling from 600 s to 1.2 s was not taken as a reboot")
	}
	utc, source, _ := c.At(1_200, t0.Add(time.Second))
	if source != Estimated {
		t.Errorf("source after reboot = %v, want estimated: the old anchor belonged to the old boot", source)
	}
	within(t, "first sample after reboot", utc, t0.Add(time.Second), time.Millisecond)
}

func TestAReorderedDatagramIsNotAReboot(t *testing.T) {
	var c Clock
	c.Observe(10_000, t0)
	if c.Observe(9_800, t0.Add(10*time.Millisecond)) {
		t.Error("a frame 200 ms older than the last one was taken as a reboot")
	}
}
